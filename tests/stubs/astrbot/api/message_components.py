# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.api.message_components 的桩。"""

from __future__ import annotations


class File:
    def __init__(self, file: str = "", name: str = ""):
        self.file = file
        self.name = name

    def __repr__(self) -> str:
        return f"<File {self.name} {self.file}>"


class Image:
    def __init__(self, file: str = ""):
        self.file = file


class Plain:
    def __init__(self, text: str = ""):
        self.text = text
