# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""独立执行进程。

插件和 AstrBot 同进程，所以插件里任何一段代码崩了都可能带走宿主。这个进程专门
负责「起子进程、看门狗、收输出」这段最容易出事的逻辑，父进程只跟它说话。

协议：stdin 一行一个 JSON 请求，stdout 一行一个 JSON 响应。
- stdout 只放协议，日志一律走 stderr
- 自己是一个会话首进程（父进程 spawn 时 start_new_session），
  所以父进程 killpg 就能把这里和它派生的所有命令一起收掉
- 一次只处理一个请求，串行；并发交给父进程的多个 worker
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping

from .limits import Limits, run

PROTOCOL_VERSION = 1


def _chroot_preexec(spec: Mapping[str, Any]):
    rootfs = str(spec.get("rootfs") or "")
    uid = int(spec.get("uid") or 0)
    gid = int(spec.get("gid") or 0)

    workdir = str(spec.get("cwd") or "/sandbox")

    def _enter() -> None:  # pragma: no cover - 子进程里跑
        os.chroot(rootfs)
        os.chdir(workdir)
        if uid > 0:
            try:
                os.setgroups([])
            except OSError:
                pass
            os.setgid(gid)
            os.setuid(uid)

    return _enter


def _preexec(spec: Mapping[str, Any] | None):
    if not spec:
        return None
    if spec.get("kind") == "chroot":
        return _chroot_preexec(spec)
    return None


def handle(request: Mapping[str, Any]) -> dict:
    limits = Limits(**request.get("limits", {}))
    result = run(
        request.get("argv", []),
        cwd=request.get("cwd") or None,
        limits=limits,
        env=request.get("env") or None,
        preexec=_preexec(request.get("preexec")),
    )
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration": result.duration,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "memory_killed": result.memory_killed,
        "cpu_killed": result.cpu_killed,
        "error": result.error,
        "warning": result.warning,
    }


def serve(stdin, stdout) -> None:
    for raw in iter(stdin.readline, ""):
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"error": "请求不是合法 JSON"}) + "\n")
            stdout.flush()
            continue
        try:
            response = handle(request)
        except Exception as exc:  # noqa: BLE001 - worker 不能因为一个请求就死
            response = {"error": f"worker 内部错误：{exc}"}
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()


def main() -> int:
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
