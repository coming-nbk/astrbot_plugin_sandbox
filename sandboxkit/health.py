# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""插件自身的健康闸门。

插件和 AstrBot 同进程，所以「插件状态不好」等于「宿主状态不好」。这里放三道：

1. 熔断：短时间连续内部异常就开门，冷却期内一律拒绝新任务，避免半死不活地拖着宿主
2. 自检：进程 RSS / 线程数超过阈值时拒绝新任务（阈值默认 0 = 关闭）
3. 拉闸：base_dir 下出现 DISABLED 文件就全部拒绝，给管理员一个不用改配置的开关

判定逻辑纯内存 + 只读 /proc，不 import astrbot，方便测。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    reason: str
    failures: int
    circuit_open: bool
    rss_mb: float
    threads: int
    disabled_file: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _read_rss_mb() -> float:
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * 4096 / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _read_threads() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


class HealthGuard:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        failure_window: float = 60.0,
        cooldown: float = 300.0,
        rss_limit_mb: int = 0,
        thread_limit: int = 0,
        kill_switch: Path | str | None = None,
        clock: Callable[[], float] = time.monotonic,
        rss_reader: Callable[[], float] = _read_rss_mb,
        thread_reader: Callable[[], int] = _read_threads,
    ):
        self.failure_threshold = max(1, int(failure_threshold))
        self.failure_window = max(1.0, float(failure_window))
        self.cooldown = max(1.0, float(cooldown))
        self.rss_limit_mb = max(0, int(rss_limit_mb))
        self.thread_limit = max(0, int(thread_limit))
        self.kill_switch = Path(kill_switch) if kill_switch else None
        self._clock = clock
        self._rss = rss_reader
        self._threads = thread_reader
        self._failures: deque[float] = deque()
        self._open_until = 0.0
        self._last_reason = ""

    # ---------- 判定 ----------

    def _disabled(self) -> bool:
        if self.kill_switch is None:
            return False
        try:
            return self.kill_switch.exists()
        except OSError:
            return False

    def allow(self) -> tuple[bool, str]:
        now = self._clock()
        if self._disabled():
            return False, (
                "沙盒已被管理员停用（数据目录里有 DISABLED 文件，删掉它即可恢复）"
            )
        if now < self._open_until:
            left = int(self._open_until - now)
            return False, f"沙盒因内部错误过多暂停使用，{left} 秒后自动恢复"

        if self.rss_limit_mb:
            rss = self._rss()
            if rss > self.rss_limit_mb:
                return False, (
                    f"进程内存 {rss:.0f}MB 超过上限 {self.rss_limit_mb}MB，"
                    "已暂停新任务"
                )
        if self.thread_limit:
            threads = self._threads()
            if threads > self.thread_limit:
                return False, (
                    f"进程线程数 {threads} 超过上限 {self.thread_limit}，"
                    "已暂停新任务"
                )
        return True, ""

    # ---------- 记录 ----------

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self.failure_window:
            self._failures.popleft()

    def record_failure(self, reason: str = "") -> bool:
        """记一次内部错误。返回是否因此熔断。"""
        now = self._clock()
        self._prune(now)
        self._failures.append(now)
        self._last_reason = reason[:200]
        if len(self._failures) >= self.failure_threshold:
            self._open_until = now + self.cooldown
            self._failures.clear()
            return True
        return False

    def record_success(self) -> None:
        # 成功不清零：偶发失败仍然计数，避免「一成功就洗白」
        self._prune(self._clock())

    def reset(self) -> None:
        self._failures.clear()
        self._open_until = 0.0
        self._last_reason = ""

    # ---------- 状态 ----------

    def status(self) -> HealthStatus:
        now = self._clock()
        self._prune(now)
        open_now = now < self._open_until
        return HealthStatus(
            ok=not open_now and not self._disabled(),
            reason=(
                f"冷却中，剩余 {int(self._open_until - now)} 秒"
                if open_now
                else ("DISABLED" if self._disabled() else "正常")
            ),
            failures=len(self._failures),
            circuit_open=open_now,
            rss_mb=round(self._rss(), 1),
            threads=self._threads(),
            disabled_file=self._disabled(),
        )
