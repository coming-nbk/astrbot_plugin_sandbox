# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.api.event 的桩：事件对象 + filter 装饰器。"""

from __future__ import annotations

from typing import Any, Callable


class MessageChain:
    """最小实现：装一串组件，够测试判断内容用。"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(text)
        return self


class _Result:
    def __init__(self, kind: str, payload: Any):
        self.kind = kind
        self.payload = payload

    @property
    def text(self) -> str:
        return str(self.payload)

    def __repr__(self) -> str:
        return f"<Result {self.kind}: {self.payload!r}>"


class AstrMessageEvent:
    def __init__(self, message: str = "", sender: str = "u1", group: str = ""):
        self.message_str = message
        self._sender = sender
        self._group = group

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str:
        return self._group

    def plain_result(self, text: str) -> _Result:
        return _Result("plain", text)

    def image_result(self, url: str) -> _Result:
        return _Result("image", url)

    def chain_result(self, chain) -> _Result:
        return _Result("chain", chain)


class _Filter:
    def __init__(self):
        self.commands: list[str] = []
        self.tools: list[str] = []

    def command(self, name: str, *args, **kwargs):
        def _wrap(func: Callable):
            self.commands.append(name)
            func.__command__ = name
            return func

        return _wrap

    def llm_tool(self, name: str = "", *args, **kwargs):
        def _wrap(func: Callable):
            self.tools.append(name or func.__name__)
            func.__tool__ = name or func.__name__
            return func

        return _wrap

    def permission_type(self, *args, **kwargs):
        def _wrap(func: Callable):
            return func

        return _wrap


filter = _Filter()
