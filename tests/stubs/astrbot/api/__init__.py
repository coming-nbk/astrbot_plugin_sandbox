# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.api 的桩。"""

from __future__ import annotations

from . import message_components  # noqa: F401


class _Logger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _log(self, level, message, *args, **kwargs):
        self.records.append((level, str(message)))

    def info(self, message, *args, **kwargs):
        self._log("info", message)

    def warning(self, message, *args, **kwargs):
        self._log("warning", message)

    def warn(self, message, *args, **kwargs):
        self._log("warning", message)

    def error(self, message, *args, **kwargs):
        self._log("error", message)

    def debug(self, message, *args, **kwargs):
        self._log("debug", message)


logger = _Logger()
