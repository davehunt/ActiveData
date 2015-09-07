# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http:# mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from copy import copy
from pyLibrary import convert

from pyLibrary.env import elasticsearch
from pyLibrary.env.elasticsearch import ES_NUMERIC_TYPES
from pyLibrary.meta import use_settings
from pyLibrary.queries import qb
from pyLibrary.queries.containers import Container
from pyLibrary.queries.domains import NumericDomain, SimpleSetDomain, DefaultDomain, UniqueDomain
from pyLibrary.queries.query import Query
from pyLibrary.debugs.logs import Log
from pyLibrary.dot.dicts import Dict
from pyLibrary.dot import coalesce, set_default, Null, literal_field
from pyLibrary.dot import wrap
from pyLibrary.strings import expand_template
from pyLibrary.thread.threads import Queue, Thread, Lock, Signal
from pyLibrary.times.dates import Date
from pyLibrary.times.durations import Duration


singlton = None


class FromESMetadata(Container):
    """
    QUERY THE METADATA
    """
    singleton = None

    def __new__(cls, *args, **kwargs):
        global singlton
        if singlton:
            Log.error()
            return singlton
        else:
            singlton = object.__new__(cls)
            return singlton

    @use_settings
    def __init__(self, host, index, alias=None, name=None, port=9200, settings=None):
        from pyLibrary.queries.containers.lists import ListContainer

        Container.__init__(self, None, schema=self)
        self.settings = settings
        self.default_name = coalesce(name, alias, index)
        self.default_es = elasticsearch.Index(settings=settings)
        self.tables_schema = wrap({c.name: c for c in self.get_columns(table="meta.tables")})
        self.tables = ListContainer([], self.tables_schema)
        self.columns_schema = wrap({c.name: c for c in self.get_columns(table="meta.columns")})
        self.columns = ListContainer([], self.columns_schema)
        self.todo = Queue("refresh metadata")
        self.worker = Thread.run("refresh metadata", self.monitor)
        self.locker = Lock("")
        self.done_first_pass = Signal()
        return

    @property
    def query_path(self):
        return None

    @property
    def url(self):
        return self.default_es.path + "/" + self.default_name.replace(".", "/")

    def get_table(self, table_name):
        with self.locker:
            return self.tables.query({"where": {"eq": {"name": table_name}}})

    def _get_columns(self):
        # TODO: HANDLE MORE THEN ONE ES, MAP TABLE SHORT_NAME TO ES INSTANCE
        with self.locker:
            all_columns = []
            alias_done = set()
            metadata = self.default_es.cluster.get_metadata()
            for index, meta in qb.sort(metadata.indices.items(), {"value": 0, "sort": -1}):
                for _, properties in meta.mappings.items():
                    columns = elasticsearch.parse_properties(index, None, properties.properties)
                    for c in columns:
                        c.table = index
                        c.domain = DefaultDomain()

                        existing_columns = filter(lambda r: r.table == index and r.abs_name == c.abs_name, self.columns.data)
                        if not existing_columns:
                            self.columns.add(c)
                            self.todo.add(c)
                        else:
                            set_default(existing_columns[0], c)
                            self.todo.add(c)

                    for alias in meta.aliases:
                        # ONLY THE LATEST ALIAS IS CHOSEN TO GET COLUMNS
                        if alias in alias_done:
                            continue
                        alias_done.add(alias)

                        for c in columns:
                            cc = copy(c)
                            cc.table = alias
                            all_columns.append(cc)

                        existing_columns = filter(lambda r: r.table == alias and r.abs_name == c.abs_name, self.columns.data)
                        if not existing_columns:
                            self.columns.add(c)
                            self.todo.add(c)
                        else:
                            set_default(existing_columns[0], c)
                            self.todo.add(c)

    def query(self, _query):
        return self.columns.query(Query(set_default(
            {
                "from": self.columns,
                "sort": ["table", "name"]
            },
            _query.as_dict()
        )))

    def get_columns(self, table):
        """
        RETURN METADATA COLUMNS
        """
        if table == "meta.columns":
            return metadata_columns()
        elif table == "meta.tables":
            return metadata_tables()

        with self.locker:
            columns = qb.sort(filter(lambda r: r.table == table, self.columns.data), "name")
            if not columns:
                self.done_first_pass.wait_for_go()
                columns = qb.sort(filter(lambda r: r.table == table, self.columns.data), "name")
                if not columns:
                    Log.error("no columns for {{table}}", table=table)
            return columns

    def _update_cardinality(self, c):
        """
        QUERY ES TO FIND CARDINALITY AND PARTITIONS FOR A SIMPLE COLUMN
        """
        if c.type in ["object", "nested"]:
            Log.error("not supported")

        result = self.default_es.search({
            "aggs": {c.name: _counting_query(c)},
            "size": 0
        })
        r = result.aggregations.values()[0]
        cardinaility = coalesce(r.value, r._nested.value)

        query = Dict(size=0)
        if c.type in ["object", "nested"]:
            Log.note("{{field}} has {{num}} parts", field=c.name, num=c.cardinality)
            with self.locker:
                self.columns.update({
                    "set": {
                        "cardinality": cardinaility,
                        "partitions": None,
                        "last_updated": Date.now()
                    },
                    "clear": ["partitions", "domain"],
                    "where": {"eq": {"table": c.table, "name": c.name}}
                })
            return
        elif c.cardinality > 1000:
            Log.note("{{field}} has {{num}} parts", field=c.name, num=c.cardinality)
            with self.locker:
                self.columns.update({
                    "set": {
                        "cardinality": cardinaility,
                        "partitions": None,
                        "last_updated": Date.now(),
                        "domain": UniqueDomain()
                    },
                    "clear": ["partitions"],
                    "where": {"eq": {"table": c.table, "name": c.name}}
                })
            return
        elif c.type in ES_NUMERIC_TYPES and c.cardinality > 30:
            Log.note("{{field}} has {{num}} parts", field=c.name, num=c.cardinality)
            with self.locker:
                self.columns.update({
                    "set": {
                        "cardinality": cardinaility,
                        "partitions": None,
                        "last_updated": Date.now(),
                        "domain": NumericDomain()
                    },
                    "clear": ["partitions"],
                    "where": {"eq": {"table": c.table, "name": c.name}}
                })
            return
        elif c.nested_path:
            query.aggs[literal_field(c.name)] = {
                "nested": {"path": c.nested_path[0]},
                "aggs": {"_nested": {"terms": {"field": c.name, "size": 0}}}
            }
        else:
            query.aggs[literal_field(c.name)] = {"terms": {"field": c.name, "size": 0}}

        result = self.default_es.search(query)

        aggs = result.aggregations.values()[0]
        if aggs._nested:
            parts = qb.sort(aggs._nested.buckets.key)
        else:
            parts = qb.sort(aggs.buckets.key)

        Log.note("{{field}} has {{parts}}", field=c.name, parts=parts)
        with self.locker:
            self.columns.update({
                "set": {
                    "cardinality": cardinaility,
                    "partitions": parts,
                    "domain": SimpleSetDomain(partitions=parts),
                    "last_updated": Date.now()
                },
                "where": {"eq": {"table": c.table, "name": c.name}}
            })

    def monitor(self, please_stop):
        with self.locker:
            Log.note("initial metadata pull")
            self._get_columns()
            self.done_first_pass.go()
        while not please_stop:
            if not self.todo:
                Log.note("look for more metatdata to update")
                with self.locker:
                    old_columns = filter(lambda c: c.last_updated == None or c.last_updated >= Date.now()-Duration("2hour"), self.columns)
                    self.todo.extend(old_columns)

            column = self.todo.pop(Duration.MINUTE*10)
            if column:
                if column.type in ["object", "nested"]:
                    continue

                self._update_cardinality(column)
                Log.note("updated {{column.name}}", column=column)
            else:
                Thread.sleep(Duration.MINUTE)



def _counting_query(c):
    if c.nested_path:
        return {
            "nested": {
                "path": c.nested_path[0] # FIRST ONE IS LONGEST
            },
            "aggs": {
                "_nested": {"cardinality": {
                    "field": c.name,
                    "precision_threshold": 10 if c.type in ES_NUMERIC_TYPES else 100
                }}
            }
        }
    else:
        return {"cardinality": {
            "field": c.name
        }}


def metadata_columns():
    return wrap(
        [
            Column(
                table="meta.columns",
                name=c,
                abs_name=c,
                type="string",
                nested_path=Null,
            )
            for c in [
                "name",
                "type",
                "nested_path",
                "relative",
                "abs_name",
                "table"
            ]
        ] + [
            Column(
                table="meta.columns",
                name=c,
                abs_name=c,
                type="object",
                nested_path=Null,
            )
            for c in [
                "domain",
                "partitions"
            ]
        ] + [
            Column(
                table="meta.columns",
                name=c,
                abs_name=c,
                type="long",
                nested_path=Null,
            )
            for c in [
                "count",
                "cardinality"
            ]
        ] + [
            Column(
                table="meta.columns",
                name="etl.timestamp",
                abs_name="etl.timestamp",
                type="long",
                nested_path=Null,
            )
        ]
    )

def metadata_tables():
    return wrap(
        [
            Column(
                table="meta.tables",
                name=c,
                abs_name=c,
                type="string",
                nested_path=Null
            )
            for c in [
                "name",
                "url",
                "query_path"
            ]
        ]
    )





def DataClass(name, columns):
    """
    Each column has {"name", "required", "nulls"} properties
    """

    columns = wrap([{"name": c, "required": True, "nulls": False} if isinstance(c, basestring) else c for c in columns])
    slots = columns.name
    required = wrap(filter(lambda c: c.required and not c.nulls, columns)).name
    nulls = wrap(filter(lambda c: c.nulls, columns)).name

    code = expand_template("""
from __future__ import unicode_literals
from collections import Mapping

class {{name}}(Mapping):
    __slots__ = {{slots}}

    def __init__(self, **kwargs):
        for s in {{slots}}:
            setattr(self, s, kwargs.get(s, Null))

        missed = {{required}}-set(kwargs.keys())
        if missed:
            Log.error("Expecting properties {"+"{missed}}", missed=missed)

        illegal = set(kwargs.keys())-set({{slots}})
        if illegal:
            Log.error("{"+"{names}} are not a valid properties", names=illegal)

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        setattr(self, item, value)
        return self

    def __getattr__(self, item):
        Log.error("{"+"{item|quote}} not valid attribute", item=item)

    def items(self):
        return ((k, getattr(self, k)) for k in {{slots}})

    def __copy__(self):
        return Column(**{{dict}})

    def __iter__(self):
        return {{slots}}.__iter__()

    def __len__(self):
        return {{len_slots}}

temp = {{name}}
""",
        {
            "name": name,
            "slots": "(" + (", ".join(convert.value2quote(s) for s in slots)) + ")",
            "required": "{" + (", ".join(convert.value2quote(s) for s in required)) + "}",
            "nulls": "{" + (", ".join(convert.value2quote(s) for s in nulls)) + "}",
            "len_slots": len(slots),
            "dict": "{" + (", ".join(convert.value2quote(s) + ": self." + s for s in slots)) + "}"
        }
    )

    return _exec(code)


def _exec(code):
    temp = None
    exec(code)
    return temp


class Table(DataClass("Table", [
    "name",
    "url",
    "query_path"
])):
    @property
    def columns(self):
        return FromESMetadata.singlton.get_columns(table=self.name)


Column = DataClass(
    "Column",
    [
        "name",
        "abs_name",
        "table",
        "type",
        {"name": "nested_path", "nulls": True},
        {"name": "domain", "nulls": True},
        {"name": "relative", "nulls": True},
        {"name": "count", "nulls": True},
        {"name": "cardinality", "nulls": True},
        {"name": "partitions", "nulls": True},
        {"name": "last_updated", "nulls": True}
    ]
)



