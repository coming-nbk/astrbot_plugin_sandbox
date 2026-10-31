# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.api.star 的桩：Context / Star / register。"""

from __future__ import annotations

from typing import Any, Callable


class Context:
    def __init__(self):
        self.web_apis: list[dict] = []
        self.fail_on_register = False
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo, chain):
        self.sent.append((str(umo), chain))

    def register_web_api(self, route, handler, methods, desc=""):
        if self.fail_on_register:
            raise RuntimeError("模拟注册失败")
        self.web_apis.append(
            {"route": route, "handler": handler, "methods": list(methods), "desc": desc}
        )


class Star:
    def __init__(self, context: Context | None = None):
        self.context = context


def register(name: str, author: str = "", desc: str = "", version: str = ""):
    def _wrap(cls: Callable):
        cls.__plugin_meta__ = {
            "name": name,
            "author": author,
            "desc": desc,
            "version": version,
        }
        return cls

    return _wrap


def __getattr__(name: str) -> Any:  # 兼容其它可能被引用的符号
    raise AttributeError(name)
