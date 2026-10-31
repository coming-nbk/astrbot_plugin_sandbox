# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""限流与并发闸门。

沙盒里的命令是用户能直接触发的重操作：一个 `while true` 循环就能把 CPU 占满，
连着发十条就是十个进程组。所以两道闸：

- RateLimiter：每个用户每分钟最多 N 次，滑动窗口，超了直接拒绝并告诉还要等几秒
- ConcurrencyGuard：全局同时在跑的命令数有上限，满了就拒绝，不排队

两者都是纯内存、线程安全，不依赖 astrbot，方便测。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_minute = max(0, int(per_minute))
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """返回 (是否放行, 需要等待的秒数)。per_minute<=0 表示不限。"""
        if self.per_minute <= 0:
            return True, 0.0

        now = self._clock()
        window = 60.0
        with self._lock:
            bucket = self._hits.setdefault(str(key), deque())
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                retry = window - (now - bucket[0])
                return False, max(1.0, retry)
            bucket.append(now)
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(str(key), None)

    def pending(self, key: str) -> int:
        with self._lock:
            return len(self._hits.get(str(key), ()))


class ConcurrencyGuard:
    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self) -> bool:
        if self.limit <= 0:
            with self._lock:
                self._active += 1
            return True
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
