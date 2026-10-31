# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""宿主机资源读数：内存、负载、磁盘。

只读 /proc 和 statvfs，不依赖 psutil（沙盒镜像里不一定装得上）。
任何一项读不到就返回 0，前端会显示成「不可用」而不是报错。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _meminfo() -> tuple[float, float]:
    """返回 (已用 MB, 总 MB)。"""
    total = available = 0
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0, 0.0
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        if key == "MemTotal":
            total = value
        elif key == "MemAvailable":
            available = value
    if total <= 0:
        return 0.0, 0.0
    used = max(total - available, 0)
    return round(used / 1024, 1), round(total / 1024, 1)


def _load1() -> float | None:
    """1 分钟负载。读不到返回 None —— 不能拿 0 冒充「没有负载」。"""
    try:
        return round(float(os.getloadavg()[0]), 2)
    except (OSError, AttributeError):
        pass
    # 有些容器里 getloadavg 不可用，但 /proc/loadavg 还能读
    try:
        first = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        return round(float(first[0]), 2)
    except (OSError, ValueError, IndexError):
        return None


def snapshot(path: str | os.PathLike[str] | None = None) -> dict:
    """一眼能看懂的宿主资源快照。"""
    used_mb, total_mb = _meminfo()
    cpu = os.cpu_count() or 1
    load1 = _load1()

    disk_total = disk_free = 0
    try:
        usage = shutil.disk_usage(str(path) if path else "/")
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        pass

    return {
        "cpu_count": cpu,
        "load1": load1,
        "load_ratio": round(min((load1 or 0.0) / cpu, 1.0), 3),
        "mem_used_mb": used_mb,
        "mem_total_mb": total_mb,
        "mem_ratio": round(used_mb / total_mb, 3) if total_mb else 0.0,
        "disk_total_gb": round(disk_total / 1024**3, 1),
        "disk_free_gb": round(disk_free / 1024**3, 1),
        "disk_ratio": round((disk_total - disk_free) / disk_total, 3) if disk_total else 0.0,
    }
