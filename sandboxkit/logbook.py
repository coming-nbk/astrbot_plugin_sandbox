# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""独立日志：按天切文件，按天数保留，定时清理。

和 audit.jsonl 分工不同：
- audit.jsonl 是给「谁干了什么」留证据，只记结构化元数据，按大小轮转
- 这里的日志是给人看的运行记录（后端选择、rootfs 准备、执行结果、异常），
  按天分文件，超过保留天数就删

写入失败绝不影响主流程；清理失败只记一条 warning，不抛。
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable

LOG_NAME = re.compile(r"^sandbox-(\d{4}-\d{2}-\d{2})\.log$")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _safe_line(text: str) -> str:
    # 一行一条，避免多行输出把日志结构打散
    return " ".join(str(text).split())


class LogBook:
    def __init__(
        self,
        directory: Path | str,
        *,
        enabled: bool = True,
        retention_days: int = 7,
        max_files: int = 30,
        max_bytes: int = 5 * 1024 * 1024,
        clock=None,
    ):
        self.dir = Path(directory)
        self.enabled = enabled
        self.retention_days = max(1, int(retention_days))
        self.max_files = max(1, int(max_files))
        self.max_bytes = max(0, int(max_bytes))
        self._clock = clock or time.time
        self._lock = threading.Lock()

    # ---------- 写入 ----------

    def path_for(self, day: str | None = None) -> Path:
        return self.dir / f"sandbox-{day or _today()}.log"

    def write(self, level: str, message: str, **fields) -> None:
        if not self.enabled:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [stamp, level.upper().ljust(5), _safe_line(message)]
        for key, value in fields.items():
            parts.append(f"{key}={_safe_line(value)}")
        line = " ".join(parts)
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.path_for()
                if self.max_bytes and path.exists() and path.stat().st_size >= self.max_bytes:
                    self._rotate_today(path)
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                return

    def info(self, message: str, **fields) -> None:
        self.write("info", message, **fields)

    def warn(self, message: str, **fields) -> None:
        self.write("warn", message, **fields)

    def error(self, message: str, **fields) -> None:
        self.write("error", message, **fields)

    def _rotate_today(self, path: Path) -> None:
        suffix = time.strftime("%H%M%S")
        target = path.with_name(path.name.replace(".log", f".{suffix}.log"))
        try:
            os.replace(path, target)
        except OSError:
            return

    # ---------- 清理 ----------

    def files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        rows = []
        for entry in self.dir.iterdir():
            if entry.is_file() and entry.name.startswith("sandbox-"):
                rows.append(entry)
        return sorted(rows, key=lambda item: item.name)

    def cleanup(self, *, now: float | None = None) -> list[str]:
        """删掉过期文件，再按数量兜底。返回被删的文件名。"""
        if not self.enabled:
            return []
        current = now if now is not None else self._clock()
        cutoff = current - self.retention_days * 86400
        removed: list[str] = []
        with self._lock:
            rows = self.files()
            keep: list[Path] = []
            for path in rows:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                day = self._day_of(path)
                expired = day is not None and day < time.strftime(
                    "%Y-%m-%d", time.localtime(cutoff)
                )
                if expired or mtime < cutoff:
                    if self._unlink(path):
                        removed.append(path.name)
                    continue
                keep.append(path)
            for path in keep[: max(0, len(keep) - self.max_files)]:
                if self._unlink(path):
                    removed.append(path.name)
        return removed

    @staticmethod
    def _day_of(path: Path) -> str | None:
        match = LOG_NAME.match(path.name)
        return match.group(1) if match else None

    @staticmethod
    def _unlink(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def total_bytes(self) -> int:
        total = 0
        for path in self.files():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def tail(self, lines: int = 50) -> list[str]:
        """最近一个日志文件的最后几行，给 WebUI 用。"""
        rows = self.files()
        if not rows:
            return []
        try:
            with open(rows[-1], "r", encoding="utf-8", errors="replace") as handle:
                return [line.rstrip("\n") for line in handle.readlines()[-lines:]]
        except OSError:
            return []


def cleanup_many(books: Iterable[LogBook]) -> dict[str, list[str]]:
    return {str(book.dir): book.cleanup() for book in books}
