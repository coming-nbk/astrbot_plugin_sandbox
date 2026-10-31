# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""quart 的桩：只实现插件用到的那点表面。"""

from __future__ import annotations


class _Request:
    def __init__(self):
        self.method = "GET"
        self.args: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.remote_addr = "127.0.0.1"
        self._json = None

    async def get_json(self, silent: bool = False):
        return self._json


request = _Request()


class Response:
    def __init__(self, body="", status: int = 200, mimetype: str = "text/plain"):
        self.body = body
        self.status_code = status
        self.mimetype = mimetype
        self.headers: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"<Response {self.status_code} {self.mimetype}>"


def jsonify(payload):
    return Response(payload, 200, "application/json")
