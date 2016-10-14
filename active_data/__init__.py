# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from pyLibrary.debugs.logs import Log
from pyLibrary.dot import wrap
from pyLibrary.times.dates import Date

request_log_queue = None


def record_request(request, query_, data, error):
    try:
        if request_log_queue == None:
            return

        if data and len(data)>10000:
            data = data[:10000]

        log = wrap({
            "timestamp": Date.now(),
            "http_user_agent": request.headers.get("user_agent"),
            "http_accept_encoding": request.headers.get("accept_encoding"),
            "path": request.headers.environ["werkzeug.request"].full_path,
            "content_length": request.headers.get("content_length"),
            "remote_addr": request.remote_addr,
            "query": query_,
            "data": data,
            "error": error
        })
        log["from"] = request.headers.get("from")
        request_log_queue.add({"value": log})
    except Exception, e:
        Log.warning("Can not record", cause=e)


def cors_wrapper(func):
    def _default(obj, key, value):
        v = obj.get(key)
        if v == None:
            obj[key] = value

    def output(*args, **kwargs):
        response = func(*args, **kwargs)
        headers = response.headers
        _default(headers, "Access-Control-Allow-Origin", "*")
        _default(headers, "Content-Type", "application/json")
        return response

    output.provide_automatic_options = False
    output.__name__ = func.__name__
    return output

