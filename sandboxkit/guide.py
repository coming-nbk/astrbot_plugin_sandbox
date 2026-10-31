# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""沙盒使用说明：按当前配置和用户权限动态生成。

不写死文案 —— 配额、超时、联网、导入这些每个人可能都不一样，
写死就会出现「说明里说 30 秒，实际给的是 5 秒」这种事故。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .i18n import t
from .paths import human_bytes

# 命令表：只有用户确实能用的才会出现在说明里
COMMANDS = (
    ("run", "/sbx run <命令>", "在沙盒里跑一条 shell 命令", "allow_exec"),
    ("py", "/sbx py <代码>", "直接跑 Python，不用先写文件", "allow_py"),
    ("ls", "/sbx ls [目录]", "看目录里有什么", ""),
    ("cat", "/sbx cat <文件>", "看一个文本文件", ""),
    ("usage", "/sbx usage", "看自己用了多少空间", ""),
    ("import", "/sbx import <宿主路径>", "把宿主机上的文件拷进来", "allow_import"),
    ("export", "/sbx export <文件>", "把沙盒里的文件拿走", "allow_export"),
    ("reset", "/sbx reset", "清空自己的沙盒", "allow_reset"),
)


def _yes_no(value: bool) -> str:
    return "允许" if value else "禁止"


def build_guide(
    *,
    lang: str = "zh",
    user_id: str,
    is_admin: bool,
    quota_bytes: int,
    timeout_seconds: float,
    memory_mb: int,
    max_runs_per_minute: int,
    allow_network: bool,
    allow_import: bool,
    backend: str,
    preset: str = "",
    version: str = "",
    caps: dict | None = None,
    has_python: bool = True,
) -> str:
    """拼出这个用户专属的使用说明。"""
    caps = caps or {}
    lines: list[str] = []
    add = lines.append

    add(t("guide.title", lang, user=user_id))
    add("")

    add(t("guide.can_use", lang))
    for _key, syntax, _desc, cap in COMMANDS:
        if cap == "allow_import":
            if not allow_import:
                continue
        elif cap and not caps.get(cap, True):
            continue
        add(f"  {syntax}")
        add("      " + t(f"guide.cmd_{_key}", lang))
    add("  /sbx guide")
    add("      " + t("guide.cmd_guide", lang))
    if is_admin:
        add("  /sbx install <包名>")
        add("      " + t("guide.cmd_install", lang))
        add("  /sbx bootstrap")
        add("      " + t("guide.cmd_bootstrap", lang))

    add("")
    add(t("guide.limits", lang))
    add(t("guide.quota", lang, value=human_bytes(quota_bytes)))
    add(t("guide.timeout", lang, seconds=int(timeout_seconds)))
    add(t("guide.memory", lang, mb=memory_mb))
    if max_runs_per_minute > 0:
        add(t("guide.rate", lang, n=max_runs_per_minute))
    else:
        add(t("guide.rate_off", lang))
    add(t("guide.network", lang, value=t("common.yes" if allow_network else "common.no", lang)))
    add(t("guide.import", lang, value=t("common.yes" if allow_import else "common.no", lang)))

    add("")
    add(t("guide.whats_inside", lang))
    system = (preset + " " + version).strip() or t("common.unknown", lang)
    add(t("guide.system", lang, preset=system, backend=backend))
    if not has_python:
        add(t("guide.python_missing", lang))
    elif not caps.get("allow_py", True):
        add(t("guide.python_denied", lang))
    else:
        add(t("guide.python_ok", lang))
    add(t("guide.cwd", lang))

    add("")
    add(t("guide.notes", lang))
    add(t("guide.note_vm", lang))
    add(t("guide.note_net_on" if allow_network else "guide.note_net_off", lang))
    add(t("guide.note_output", lang))
    add(t("guide.note_quota", lang))
    if backend == "local":
        add(t("guide.note_local", lang))

    return "\n".join(lines)


class GuideTracker:
    """记一下谁已经看过说明，避免每次都说一遍。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def seen(self, user_id: str) -> bool:
        return user_id in self._read()

    def mark(self, user_id: str) -> bool:
        """记下已读。返回「这是第一次」的布尔值。"""
        data = self._read()
        if user_id in data:
            return False
        data[user_id] = int(time.time())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
        return True
