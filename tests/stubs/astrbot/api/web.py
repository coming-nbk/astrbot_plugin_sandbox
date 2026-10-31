# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.api.web 的桩（AstrBot >= 4.29 的插件 Web 层）。"""

from __future__ import annotations


class Response:
    def __init__(self, payload, status: int = 200):
        self.body = payload
        self.status = status
        self.status_code = status
        self.headers: dict[str, str] = {}


class _Query(dict):
    def get(self, key, default=None, type=None):  # noqa: A002 - 对齐官方签名
        value = super().get(key, default)
        if type is not None and value is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class _Request:
    def __init__(self):
        self.method = "GET"
        self.query = _Query()
        self.headers: dict[str, str] = {}
        self.remote_addr = "127.0.0.1"
        self._json = None

    async def json(self, default=None):
        return default if self._json is None else self._json

    @property
    def username(self) -> str:
        return "tester"


request = _Request()


def json_response(payload, status: int = 200) -> Response:
    return Response(payload, status)


def error_response(message: str, status: int = 400) -> Response:
    return Response({"status": "error", "message": message}, status)
