#!/usr/bin/env python3
# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""并行跑测试。

单进程跑 600 多个用例要 70 多秒，瓶颈是那些真的去 fork 子进程、
等超时、解压 rootfs 的用例。按模块分给多个进程，墙钟时间基本能砍一半以上。

用法：
    python3 scripts/test.py            # 全部，按模块并行
    python3 scripts/test.py --fast     # 只跑改页面/配置时会碰到的几个模块
    python3 scripts/test.py -j 8       # 指定并行度
    python3 scripts/test.py webapi     # 只跑名字里带 webapi 的模块
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path as _Path
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 改前端、改配置格式时最先要跑的几个，加起来不到 2 秒
FAST = [
    "test_pages",
    "test_config",
    "test_parse",
    "test_guard",
    "test_i18n",
]

CACHE = ROOT / ".testcache"


def source_digest() -> str:
    """源码指纹：没变就不用重跑。"""
    import hashlib

    digest = hashlib.sha256()
    skip = {"__pycache__", ".git", "tests", "plugin_data", ".testcache"}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(part in skip for part in path.parts) or rel.endswith(".zip"):
            continue
        digest.update(rel.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_ok() -> bool:
    try:
        return CACHE.read_text().strip() == source_digest()
    except OSError:
        return False


def cache_save() -> None:
    try:
        CACHE.write_text(source_digest())
    except OSError:
        pass


def modules() -> list[str]:
    found = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        found.append(path.stem)
    return found


# 改了哪块 → 该跑哪些模块。只用来快速自检，发布前还是要跑全量。
CHANGE_MAP = [
    ("pages/", ["test_pages", "test_preview_js"]),
    ("main.py", ["test_plugin_smoke", "test_pages"]),
    ("metadata.yaml", ["test_metadata", "test_license"]),
    ("_conf_schema.json", ["test_config", "test_pluginconf"]),
    ("README", ["test_license"]),
    ("sandboxkit/webapi.py", ["test_webapi"]),
    ("sandboxkit/webfs.py", ["test_webfs"]),
    ("sandboxkit/config.py", ["test_config", "test_pluginconf"]),
    ("sandboxkit/i18n.py", ["test_i18n"]),
    ("sandboxkit/parse.py", ["test_parse"]),
    ("sandboxkit/guard.py", ["test_guard"]),
    ("sandboxkit/limits.py", ["test_limits"]),
    ("sandboxkit/service.py", ["test_service"]),
    ("sandboxkit/bootstrap.py", ["test_bootstrap"]),
    ("sandboxkit/worker", ["test_worker"]),
    ("sandboxkit/backends.py", ["test_docker"]),
    ("sandboxkit/setup.py", ["test_setup"]),
    ("sandboxkit/health.py", ["test_health"]),
    ("sandboxkit/audit", ["test_security"]),
    ("sandboxkit/store.py", ["test_store"]),
    ("sandboxkit/session.py", ["test_session"]),
    ("sandboxkit/paths.py", ["test_paths"]),
    ("sandboxkit/env.py", ["test_env"]),
    ("sandboxkit/guide.py", ["test_guide"]),
    ("sandboxkit/cve_store.py", ["test_cve"]),
    ("sandboxkit/metrics.py", ["test_metrics"]),
    ("sandboxkit/logbook.py", ["test_logbook"]),
    ("sandboxkit/profiles.py", ["test_profiles"]),
    ("sandboxkit/pluginconf.py", ["test_pluginconf"]),
    ("install.py", ["test_install"]),
    ("scripts/", ["test_install"]),
]


def changed_modules(minutes: int) -> list[str]:
    """按最近改过的文件推断要跑哪些模块。"""
    cutoff = time.time() - minutes * 60
    picked: list[str] = []
    skip = {"__pycache__", ".git", "plugin_data"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(p in skip for p in path.parts):
            continue
        if path.suffix in {".pyc", ".zip"}:
            continue
        if path.stat().st_mtime < cutoff:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for prefix, names in CHANGE_MAP:
            if rel.startswith(prefix) or prefix in rel:
                picked.extend(names)
    return sorted(set(picked))


def run_inline(names: list[str]) -> int:
    """一个进程跑完，省掉每个模块 ~0.8s 的解释器启动。"""
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *[f"tests.{n}" for n in names]],
        cwd=ROOT, capture_output=True, text=True,
    )
    cost = time.time() - started
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    print("\n".join(tail))
    print(f"用时 {cost:.1f}s（{len(names)} 个模块，单进程）")
    return proc.returncode


def run_one(name: str) -> tuple[str, int, str, float]:
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", f"tests.{name}", "-v"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return name, proc.returncode, (proc.stdout + proc.stderr), time.time() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="并行跑测试")
    parser.add_argument("--fast", action="store_true", help="只跑快速模块，单进程")
    parser.add_argument("--changed", type=int, nargs="?", const=10, default=None,
                        metavar="分钟", help="只跑最近改过的文件相关的模块（默认 10 分钟）")
    parser.add_argument("-j", "--jobs", type=int, default=min(8, (os.cpu_count() or 4)))
    parser.add_argument("--full", action="store_true", help="忽略缓存，强制重跑")
    parser.add_argument("pattern", nargs="*", help="只跑名字匹配这些片段的模块")
    args = parser.parse_args()

    if not args.pattern and not args.full and cache_ok():
        print("源码没变，上次全绿，跳过（要强制重跑加 --full）")
        return 0

    names = modules()
    if args.changed is not None:
        names = changed_modules(args.changed)
        if not names:
            print(f"最近 {args.changed} 分钟没有改动，跳过测试")
            return 0
        print("改动相关：" + " ".join(names))
        code = run_inline(names)
        if code == 0:
            cache_save()
        return code
    if args.fast:
        names = [n for n in names if n in FAST]
        code = run_inline(names)
        if code == 0:
            cache_save()
        return code
    if args.pattern:
        names = [n for n in names if any(p in n for p in args.pattern)]
    if not names:
        print("没有匹配的测试模块")
        return 1

    started = time.time()
    failures = []
    total = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for name, code, output, cost in pool.map(run_one, names):
            total += 1
            mark = "ok" if code == 0 else "FAIL"
            print(f"  {mark:4} {name:28} {cost:5.1f}s")
            if code != 0:
                failures.append((name, output))

    elapsed = time.time() - started
    print(f"\n{total} 个模块，用时 {elapsed:.1f}s")

    if not failures:
        cache_save()

    if failures:
        print("\n失败详情：")
        for name, output in failures:
            print(f"\n===== {name} =====")
            print(output[-3000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
