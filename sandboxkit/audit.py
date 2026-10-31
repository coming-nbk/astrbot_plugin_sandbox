# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""审计日志：谁、在什么时候、对什么、做了什么、结果如何。

只写 JSONL，一行一条；超过上限就轮转一次（保留 .1）。
写入失败绝不影响主流程 —— 日志是旁路，不是关键路径。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path | str, *, max_bytes: int, enabled: bool = True):
        self.path = Path(path)
        self.max_bytes = max(0, int(max_bytes))
        self.enabled = enabled
        self._lock = threading.Lock()

    def write(self, action: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                # 0600：审计日志里有用户 ID 和命令，不给别人看
                descriptor = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                return

    def purge(self, retention_days: int, *, now: float | None = None) -> bool:
        """删掉超过保留期的轮转文件（只留 .1 之外的旧档）。"""
        days = max(1, int(retention_days))
        current = now if now is not None else time.time()
        cutoff = current - days * 86400
        removed = False
        for candidate in (
            self.path.with_suffix(self.path.suffix + ".1"),
            self.path,
        ):
            try:
                if candidate.exists() and candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
                    removed = True
            except OSError:
                continue
        return removed

    def _rotate_if_needed(self) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
            backup = self.path.with_suffix(self.path.suffix + ".1")
            os.replace(self.path, backup)
        except OSError:
            return
