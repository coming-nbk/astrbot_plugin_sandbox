# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""跟执行进程说话的那一头。

要点：
- worker 起不来、超时、半路死掉，都只返回一个带 error 的 ExecResult，
  绝不让异常冒到 AstrBot 里
- 超时或崩溃后 worker 会被整组杀掉并标记，下次调用自动重开
- 读响应带截止时间，避免「命令跑完了但 worker 不说话」把线程挂死
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .limits import ExecResult, Limits, kill_process_group

WORKER_SCRIPT = Path(__file__).with_name("worker.py")
GRACE_SECONDS = 5.0


class WorkerClient:
    def __init__(
        self,
        *,
        python: str | None = None,
        grace: float = GRACE_SECONDS,
        package_root: Path | None = None,
        on_crash: Callable[[str], None] | None = None,
    ):
        self.python = python or sys.executable
        self.grace = max(0.5, float(grace))
        self.package_root = Path(package_root) if package_root else WORKER_SCRIPT.parents[1]
        self._proc: subprocess.Popen | None = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._stderr_file = None
        self.on_crash = on_crash
        self.spawns = 0
        self.crashes = 0
        self.consecutive_crashes = 0
        self.last_error = ""

    # ---------- 统计 ----------

    def stats(self) -> dict:
        return {
            "spawns": self.spawns,
            "crashes": self.crashes,
            "consecutive_crashes": self.consecutive_crashes,
            "last_error": self.last_error[:200],
            "alive": self._proc is not None and self._proc.poll() is None,
        }

    def _note_crash(self, message: str) -> None:
        self.crashes += 1
        self.consecutive_crashes += 1
        self.last_error = message
        if self.on_crash is not None:
            try:
                self.on_crash(message)
            except Exception as exc:  # noqa: BLE001 - 回调不能反过来把调用方搞崩
                self.last_error = f"{message}（崩溃回调失败：{exc}）"

    # ---------- 进程管理 ----------

    def _spawn(self) -> subprocess.Popen:
        self._stderr_file = tempfile.TemporaryFile()
        env = {
            "PYTHONPATH": str(self.package_root),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(  # noqa: S603 - 参数固定，用户输入走管道
            [self.python, "-u", "-m", "sandboxkit.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            env=env,
            cwd=str(self.package_root),
            start_new_session=True,
            close_fds=True,
            bufsize=0,
        )
        self._buffer = b""
        self.spawns += 1
        return proc

    def _ensure(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._close_proc()
        self._proc = self._spawn()
        return self._proc

    def _close_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            if proc.poll() is None:
                kill_process_group(proc.pid)
                proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdout,):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass
            self._stderr_file = None

    def _stderr_tail(self) -> str:
        if self._stderr_file is None:
            return ""
        try:
            self._stderr_file.seek(0)
            data = self._stderr_file.read()[-2000:]
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    def close(self) -> None:
        with self._lock:
            self._close_proc()

    def warmup(self) -> bool:
        """提前把进程拉起来，第一条用户命令就不用等 Python 启动。"""
        try:
            result = self.run(
                ["/bin/sh", "-c", "true"],
                cwd=None,
                limits=Limits(timeout=15, memory_mb=0),
            )
            return result.ok
        except Exception:  # noqa: BLE001 - 预热失败不影响功能
            return False

    # ---------- 读一行 ----------

    def _read_line(self, proc: subprocess.Popen, deadline: float) -> bytes:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("worker 没有在期限内返回")
            if proc.stdout is None:
                raise EOFError("worker 没有 stdout")
            ready, _, _ = select.select([proc.stdout.fileno()], [], [], remaining)
            if not ready:
                raise TimeoutError("worker 没有在期限内返回")
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError("worker 已退出")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    # ---------- 执行 ----------

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        limits: Limits,
        env: Mapping[str, str] | None = None,
        preexec_spec: Mapping[str, Any] | None = None,
    ) -> ExecResult:
        payload = {
            "argv": [str(item) for item in argv],
            "cwd": str(cwd) if cwd else None,
            "env": dict(env or {}),
            "limits": asdict(limits),
            "preexec": dict(preexec_spec or {}),
        }
        started = time.monotonic()
        with self._lock:
            try:
                proc = self._ensure()
            except (OSError, ValueError) as exc:
                self.last_error = f"执行进程启动失败：{exc}"
                return self._error(argv, started, limits, self.last_error)

            try:
                assert proc.stdin is not None
                proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.last_error = f"执行进程不可写：{exc}"
                self._note_crash(self.last_error)
                self._close_proc()
                return self._error(argv, started, limits, self.last_error)

            deadline = started + float(limits.timeout) + self.grace
            try:
                line = self._read_line(proc, deadline)
            except TimeoutError:
                self._note_crash("执行进程超时未返回")
                self._close_proc()
                return ExecResult(
                    argv=payload["argv"],
                    exit_code=-1,
                    stdout="",
                    stderr="执行进程超时未返回，已终止",
                    duration=time.monotonic() - started,
                    timed_out=True,
                    timeout=limits.timeout,
                )
            except EOFError:
                tail = self._stderr_tail()
                self.last_error = "执行进程异常退出" + (f"：{tail[:300]}" if tail else "")
                self._note_crash(self.last_error)
                self._close_proc()
                return self._error(argv, started, limits, self.last_error)

            try:
                data = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self.last_error = "执行进程返回了无法解析的内容"
                self._note_crash(self.last_error)
                self._close_proc()
                return self._error(argv, started, limits, self.last_error)

        self.consecutive_crashes = 0
        return ExecResult(
            argv=payload["argv"],
            exit_code=int(data.get("exit_code", -1)),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            duration=float(data.get("duration", time.monotonic() - started)),
            timed_out=bool(data.get("timed_out")),
            truncated=bool(data.get("truncated")),
            memory_killed=bool(data.get("memory_killed")),
            cpu_killed=bool(data.get("cpu_killed")),
            error=str(data.get("error", "")),
            warning=str(data.get("warning", "")),
            timeout=limits.timeout,
        )

    @staticmethod
    def _error(
        argv: Sequence[str], started: float, limits: Limits, message: str
    ) -> ExecResult:
        return ExecResult(
            argv=[str(item) for item in argv],
            exit_code=-1,
            stdout="",
            stderr="",
            duration=time.monotonic() - started,
            error=message,
            timeout=limits.timeout,
        )
