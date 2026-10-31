# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""读 rootfs 里装了哪些包、什么版本，用来判断「升级完到底修没修」。

不跑 chroot、不调 dpkg —— 直接解析数据库文件，快且没副作用：
- Debian/Ubuntu: /var/lib/dpkg/status
- Alpine: /lib/apk/db/installed
"""

from __future__ import annotations

import re
from pathlib import Path


def _parse_dpkg(text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    name = version = ""
    for line in text.splitlines():
        if line.startswith("Package:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif not line.strip():
            if name and version:
                packages[name] = version
            name = version = ""
    if name and version:
        packages[name] = version
    return packages


def _parse_apk(text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    name = version = ""
    for line in text.splitlines():
        if line.startswith("P:"):
            name = line[2:].strip()
        elif line.startswith("V:"):
            version = line[2:].strip()
        elif not line.strip():
            if name and version:
                packages[name] = version
            name = version = ""
    if name and version:
        packages[name] = version
    return packages


def installed_versions(rootfs: Path | str | None) -> dict[str, str]:
    if not rootfs:
        return {}
    root = Path(rootfs)
    for relative, parser in (
        ("var/lib/dpkg/status", _parse_dpkg),
        ("lib/apk/db/installed", _parse_apk),
    ):
        path = root / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data = parser(text)
        if data:
            return data
    return {}


def _segments(version: str) -> list:
    """把 1.2.3-4ubuntu1 拆成可比较的片段。"""
    version = version.split(":", 1)[-1]  # 去掉 epoch
    out: list = []
    for chunk in re.split(r"[.~+-]", version):
        if chunk.isdigit():
            out.append(int(chunk))
        elif chunk:
            out.append(chunk)
    return out


def compare(left: str, right: str) -> int:
    """返回 -1 / 0 / 1。够用即可：数字段按数值比，字母段按字典序。"""
    a, b = _segments(left), _segments(right)
    for index in range(max(len(a), len(b))):
        x = a[index] if index < len(a) else 0
        y = b[index] if index < len(b) else 0
        if isinstance(x, int) and isinstance(y, int):
            if x != y:
                return -1 if x < y else 1
        else:
            sx, sy = str(x), str(y)
            if sx != sy:
                return -1 if sx < sy else 1
    return 0


OPERATORS = ("<=", ">=", "<", ">", "=", "==")


def satisfies(version: str, condition: str) -> bool:
    """condition 形如 `<3.0.2` 或 `>=1.2`。判断 version 是否满足。"""
    condition = (condition or "").strip()
    if not condition:
        return False
    for op in OPERATORS:
        if condition.startswith(op):
            target = condition[len(op):].strip()
            if not target:
                return False
            result = compare(version, target)
            if op in ("=", "=="):
                return result == 0
            if op == "<":
                return result < 0
            if op == "<=":
                return result <= 0
            if op == ">":
                return result > 0
            return result >= 0
    return False
