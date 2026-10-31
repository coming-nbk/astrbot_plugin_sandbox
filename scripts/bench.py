#!/usr/bin/env python3
# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""量一量每种后端跑一条命令要多久。

在你自己的机器上跑，数字才有意义 —— 容器、内核、磁盘都会影响结果。

    python3 scripts/bench.py --backend local
    python3 scripts/bench.py --all --runs 20
    python3 scripts/bench.py --backend docker --rootfs /path/to/rootfs

输出：首次耗时（含建容器 / 解压缓存）和之后的中位数、p95。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.backends import BACKENDS  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.limits import Limits, build_env, run  # noqa: E402
from sandboxkit.session import Session  # noqa: E402
from sandboxkit.worker_client import WorkerClient  # noqa: E402

COMMAND = ["/bin/sh", "-c", "echo bench"]


def _settings(backend: str, reuse: bool) -> Settings:
    return Settings(
        backend=backend,
        docker_reuse=reuse,
        timeout_seconds=30,
        memory_mb=0,
        cpu_seconds=0,
        max_procs=0,
        quota_bytes=64 * 1024 * 1024,
        allow_local_backend=True,
    )


def bench_backend(
    name: str,
    rootfs: Path | None,
    runs: int,
    reuse: bool,
    use_worker: bool = False,
) -> None:
    backend = BACKENDS[name](rootfs)
    ok, why = backend.available(rootfs)
    if not ok:
        print(f"{name:8} 不可用：{why}")
        return

    settings = _settings(name, reuse)
    client = WorkerClient() if use_worker else None
    with tempfile.TemporaryDirectory(prefix="sandbox-bench-") as tmp:
        session = Session(Path(tmp), "bench")
        session.ensure()
        samples: list[float] = []
        for index in range(runs):
            prepared = backend.prepare(
                COMMAND,
                session_dir=session.dir,
                settings=settings,
                rootfs=rootfs,
            )
            env = build_env(prepared.home or str(session.dir))
            env.update(prepared.env_extra)
            limits = Limits(timeout=60, memory_mb=0, cpu_seconds=0)
            started = time.perf_counter()
            if client is not None:
                result = client.run(
                    prepared.argv,
                    cwd=prepared.cwd,
                    limits=limits,
                    env=env,
                    preexec_spec=prepared.preexec_spec,
                )
            else:
                result = run(
                    prepared.argv,
                    cwd=prepared.cwd,
                    limits=limits,
                    env=env,
                    preexec=prepared.preexec,
                )
            samples.append((time.perf_counter() - started) * 1000)
            if not result.ok:
                print(f"{name:8} 第 {index + 1} 次失败：{result.error or result.stderr}")
                return

    if client is not None:
        client.close()

    first = samples[0]
    rest = samples[1:] or samples
    print(
        f"{name:8} 首次 {first:7.1f}ms   中位数 {statistics.median(rest):6.1f}ms   "
        f"p95 {sorted(rest)[max(0, int(len(rest) * 0.95) - 1)]:6.1f}ms   "
        f"样本 {len(samples)}"
    )


def bench_quota(runs: int) -> None:
    """配额检查要扫目录，文件多了会变慢 —— 这里量一下量级。"""
    print("\n配额统计（Session.usage）:")
    with tempfile.TemporaryDirectory(prefix="sandbox-bench-") as tmp:
        session = Session(Path(tmp), "bench")
        session.ensure()
        for count in (0, 100, 1000, 5000):
            target = session.dir / f"d{count}"
            target.mkdir(exist_ok=True)
            for index in range(count - (0 if count == 0 else 0)):
                if index >= count:
                    break
                (target / f"f{index}").write_text("x")
            started = time.perf_counter()
            for _ in range(3):
                session.invalidate()
                session.usage()
            cost = (time.perf_counter() - started) / 3 * 1000
            print(f"  {count:5d} 个文件：{cost:7.2f}ms / 次")


def main() -> int:
    parser = argparse.ArgumentParser(description="沙盒后端延迟基准")
    parser.add_argument("--backend", default="local", choices=sorted(BACKENDS))
    parser.add_argument("--all", action="store_true", help="跑所有可用后端")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--rootfs", default="", help="minirootfs 路径")
    parser.add_argument("--no-reuse", action="store_true", help="docker 不复用容器")
    parser.add_argument("--quota", action="store_true", help="顺便量配额统计开销")
    parser.add_argument(
        "--worker",
        action="store_true",
        help="经独立 worker 进程执行（对比进程间往返开销）",
    )
    args = parser.parse_args()

    rootfs = Path(args.rootfs) if args.rootfs else None
    names = sorted(BACKENDS) if args.all else [args.backend]
    mode = "worker 进程" if args.worker else "同进程"
    print(f"每条命令：{' '.join(COMMAND)}   模式：{mode}\n")
    for name in names:
        bench_backend(
            name, rootfs, max(2, args.runs), not args.no_reuse, args.worker
        )
    if args.quota:
        bench_quota(args.runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
