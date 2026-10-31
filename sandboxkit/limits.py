# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""受限执行。

一条命令在沙盒里只有几种结局：跑完、超时、超内存、输出太多。每种都必须有界。

内存有两层：
- RLIMIT_AS 是硬上限，单进程分配不到就直接失败
- 看门狗按进程组统计 RSS，超过就整组 SIGKILL —— 用来兜住「fork 十个孩子各自吃一半」
  RLIMIT_AS 管不到的情况

CPU 核数用亲和性限制：进程只能被调度到前 N 个核上，不是「用满再降频」。

输出不走内存管道，而是落到临时文件再按上限读取 ——
`while true; do echo; done` 不会先把宿主内存吃满再被截断。
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

IS_POSIX = os.name == "posix"

try:  # 模块加载时导好，preexec 里绝不再 import —— 那里抢锁会死锁
    import resource
except ImportError:  # pragma: no cover - 非 POSIX
    resource = None  # type: ignore[assignment]

try:  # 同理：libc 也在加载时打开，子进程里只调 prctl
    import ctypes as _ctypes

    _LIBC = _ctypes.CDLL("libc.so.6", use_errno=True)
except Exception:  # pragma: no cover - 非 Linux / 无 libc
    _LIBC = None

# 传给子进程的环境变量白名单。绝不透传 AstrBot 的环境 —— 里面有 API Key。
SAFE_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TERM",
    "TZ",
)


@dataclass(frozen=True)
class Limits:
    timeout: float = 30.0
    memory_mb: int = 512
    cpu_seconds: int = 30
    max_procs: int = 0  # 0 = 不限制（RLIMIT_NPROC 按 uid 统计，共享主机上容易误伤）
    max_file_bytes: int = 0
    max_output_bytes: int = 32 * 1024
    max_cores: int = 0  # 0 = 不限制；>0 时只用前 N 个核
    memory_watchdog_mb: int = 0  # 0 = 关闭；>0 时按进程组 RSS 整组杀
    cpu_percent_limit: int = 95  # 单核百分比，持续超过就判死循环
    cpu_burst_seconds: float = 5.0  # 连续超限多少秒才杀，避免误杀启动抖动
    bypass_memory: bool = False  # 信任规则：跳过内存看门狗
    bypass_cpu: bool = False  # 信任规则：跳过 CPU 看门狗
    harden: bool = False  # 沙盒内是 root 时打开：no_new_privs + 丢危险 capability
    watchdog_interval: float = 0.2


@dataclass
class ExecResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False
    memory_killed: bool = False
    cpu_killed: bool = False
    error: str = ""
    warning: str = ""
    timeout: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def to_text(self, max_chars: int = 3000) -> str:
        if self.error:
            body = f"执行失败：{self.error}"
        else:
            parts = []
            if self.stdout:
                parts.append(self.stdout.rstrip("\n"))
            if self.stderr:
                parts.append("[stderr]\n" + self.stderr.rstrip("\n"))
            body = "\n".join(parts) if parts else "(无输出)"
        flags = []
        if self.timed_out:
            flags.append(f"超时（>{self.timeout:.0f}s）已终止")
        if self.memory_killed:
            flags.append("内存超限已终止")
        if self.cpu_killed:
            flags.append("疑似死循环已终止")
        if self.truncated:
            flags.append("输出已截断")
        flags.append(f"exit={self.exit_code} {self.duration:.2f}s")
        header = " · ".join(flags)
        text = f"{header}\n{body}"
        if self.warning:
            text = f"{text}\n\n注意：{self.warning}"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（已按 reply_max_chars 截断）"
        return text


def build_env(sandbox_home: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {}
    for key in SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env["HOME"] = sandbox_home
    env["TMPDIR"] = sandbox_home
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# root 模式下额外收紧：不给提权、丢掉危险 capability
_DANGEROUS_CAPS = (16, 21, 12, 19, 17, 22, 8)  # SYS_MODULE, SYS_ADMIN, NET_ADMIN, SYS_PTRACE, SYS_RAWIO, SYS_BOOT, SYS_CHROOT


def _prctl(option: int, value: int) -> bool:
    """只调用已经打开的 libc，不在子进程里做任何 import。"""
    if _LIBC is None:
        return False
    try:
        _LIBC.prctl(int(option), int(value), 0, 0, 0)
        return True
    except Exception:  # noqa: BLE001 - 老内核/非 Linux 就算了
        return False


PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38


def lock_child() -> None:
    """每个子进程都无条件执行：禁止 setuid 提权，禁止被同 uid 进程 ptrace。

    rootfs 里可能装着 su / passwd / mount 这类 setuid 程序，沙盒进程虽然是
    nobody，一执行它们就回到 euid 0 —— 而 uid 0 在 chroot 外就是真 root。
    NNP 让这些位彻底失效。

    另外所有沙盒共用同一个 uid，不锁 dumpable 的话，A 的进程能 ptrace B 的。
    """
    _prctl(PR_SET_NO_NEW_PRIVS, 1)
    _prctl(PR_SET_DUMPABLE, 0)


def drop_caps(extra_caps: Sequence[int] = ()) -> None:
    """root 模式下额外丢掉危险 capability。"""
    for cap in tuple(_DANGEROUS_CAPS) + tuple(extra_caps):
        _prctl(PR_CAPBSET_DROP, int(cap))


def harden_child(extra_caps: Sequence[int] = ()) -> None:
    """在子进程里加一层：no_new_privs + 丢危险 capability。

    这是给「沙盒内是 root」这种情况兜底的：即使 uid 是 0，也不能 setuid 提权、
    不能 mount、不能 chroot 逃逸。需要 libc 的 prctl，拿不到就静默跳过。
    """
    _prctl(PR_SET_NO_NEW_PRIVS, 1)
    for cap in tuple(_DANGEROUS_CAPS) + tuple(extra_caps):
        _prctl(PR_CAPBSET_DROP, int(cap))


def _apply_affinity(max_cores: int) -> None:
    if max_cores <= 0 or not hasattr(os, "sched_setaffinity"):
        return
    try:
        available = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return
    if not available:
        return
    try:
        os.sched_setaffinity(0, set(available[:max_cores]))
    except OSError:
        return


def _rlimit_setter(limits: Limits) -> Callable[[], None]:
    def _apply() -> None:
        if resource is None:
            return
        pairs = []
        if limits.cpu_seconds > 0:
            pairs.append((resource.RLIMIT_CPU, int(limits.cpu_seconds)))
        if limits.memory_mb > 0:
            pairs.append((resource.RLIMIT_AS, int(limits.memory_mb) * 1024 * 1024))
        if limits.max_procs > 0:
            pairs.append((resource.RLIMIT_NPROC, int(limits.max_procs)))
        if limits.max_file_bytes > 0:
            pairs.append((resource.RLIMIT_FSIZE, int(limits.max_file_bytes)))
        pairs.append((resource.RLIMIT_CORE, 0))
        for which, value in pairs:
            try:
                soft, hard = resource.getrlimit(which)
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                continue

    return _apply


def _make_preexec(
    limits: Limits,
    extra: Callable[[], None] | None,
) -> Callable[[], None] | None:
    if not IS_POSIX:
        return None
    apply_limits = _rlimit_setter(limits)
    max_cores = limits.max_cores

    def _setup() -> None:  # pragma: no cover - 只在子进程里跑
        os.umask(0o022)
        lock_child()
        if limits.harden:
            drop_caps()
        _apply_affinity(max_cores)
        if extra is not None:
            extra()
        apply_limits()

    return _setup


def _read_capped(handle, cap: int) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(cap + 1)
    truncated = len(data) > cap
    if truncated:
        data = data[:cap]
    return data.decode("utf-8", "replace"), truncated


def _page_size_kb() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") // 1024
    except (ValueError, OSError, AttributeError):
        return 4


def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100


def _read_stat_fields(pid: str) -> list[bytes] | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        # comm 里可能有空格和括号，所以从最后一个 ")" 之后开始切字段
        return raw[raw.rindex(b")") + 2 :].split()
    except ValueError:
        return None


def process_group_cpu_seconds(pgid: int) -> float:
    """进程组累计 CPU 时间（用户态 + 内核态），单位秒。"""
    if not IS_POSIX:
        return 0.0
    ticks = _clock_ticks()
    total = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0.0
    for name in entries:
        if not name.isdigit():
            continue
        fields = _read_stat_fields(name)
        if not fields or len(fields) < 22:
            continue
        try:
            if int(fields[2]) != pgid:
                continue
            total += int(fields[11]) + int(fields[12])
        except (ValueError, IndexError):
            continue
    return total / ticks if ticks else 0.0


def process_group_rss_kb(pgid: int) -> int:
    """按进程组统计 RSS。读 /proc，不依赖 psutil。"""
    if not IS_POSIX:
        return 0
    page_kb = _page_size_kb()
    total = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        try:
            # comm 里可能有空格和括号，所以从最后一个 ")" 之后开始切字段
            fields = raw[raw.rindex(b")") + 2 :].split()
            if int(fields[2]) != pgid:  # 第 5 个字段：pgrp
                continue
            total += int(fields[21]) * page_kb  # 第 24 个字段：rss（页）
        except (ValueError, IndexError):
            continue
    return total


class _ResourceWatchdog(threading.Thread):
    """一个线程同时看内存和 CPU。

    内存：按进程组 RSS 累加，超过就整组杀 —— RLIMIT_AS 管不到 fork 出来的孩子。
    CPU：按进程组 CPU 时间增量算占用率，连续超限若干秒才杀，用来抓死循环。
    """

    def __init__(
        self,
        pgid: int,
        *,
        memory_limit_kb: int,
        cpu_percent_limit: int,
        cpu_burst_seconds: float,
        interval: float,
        bypass_memory: bool = False,
        bypass_cpu: bool = False,
    ):
        super().__init__(daemon=True, name="sandbox-resource-watchdog")
        self.pgid = pgid
        self.memory_limit_kb = memory_limit_kb
        self.cpu_percent_limit = cpu_percent_limit
        self.cpu_burst_seconds = max(0.0, float(cpu_burst_seconds))
        self.interval = max(0.05, float(interval))
        self.bypass_memory = bypass_memory
        self.bypass_cpu = bypass_cpu
        self.triggered_memory = False
        self.triggered_cpu = False
        self.peak_kb = 0
        self.peak_percent = 0.0
        self._halt = threading.Event()

    def run(self) -> None:  # pragma: no cover - 线程里跑
        last_cpu = process_group_cpu_seconds(self.pgid)
        last_time = time.monotonic()
        burst = 0.0
        while not self._halt.wait(self.interval):
            now = time.monotonic()
            delta_time = now - last_time
            if delta_time <= 0:
                continue
            cpu = process_group_cpu_seconds(self.pgid)
            percent = (cpu - last_cpu) / delta_time * 100.0
            last_cpu, last_time = cpu, now
            self.peak_percent = max(self.peak_percent, percent)

            if not self.bypass_memory and self.memory_limit_kb > 0:
                rss = process_group_rss_kb(self.pgid)
                self.peak_kb = max(self.peak_kb, rss)
                if rss > self.memory_limit_kb:
                    self.triggered_memory = True
                    self._kill()
                    return

            if not self.bypass_cpu and self.cpu_percent_limit > 0:
                if percent >= self.cpu_percent_limit:
                    burst += self.interval
                    if burst >= self.cpu_burst_seconds:
                        self.triggered_cpu = True
                        self._kill()
                        return
                else:
                    # 衰减而不是清零：临界值附近会抖动，清零会让计数永远攒不起来
                    burst = max(0.0, burst - self.interval)

    def _kill(self) -> None:
        kill_process_group(self.pgid)

    def stop(self) -> None:
        self._halt.set()


_BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "dumb",
    "TMPDIR": "/tmp",
}


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    limits: Limits,
    env: Mapping[str, str] | None = None,
    preexec: Callable[[], None] | None = None,
) -> ExecResult:
    """跑一条命令，永远返回 ExecResult，不向上抛异常。"""
    argv_list = [str(item) for item in argv]
    started = time.monotonic()
    setup = _make_preexec(limits, preexec)

    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        try:
            proc = subprocess.Popen(
                argv_list,
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                env=dict(env) if env else dict(_BASE_ENV),
                start_new_session=IS_POSIX,
                preexec_fn=setup if IS_POSIX else None,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            return ExecResult(
                argv=argv_list,
                exit_code=-1,
                stdout="",
                stderr="",
                duration=time.monotonic() - started,
                error=f"无法启动进程：{exc}",
            )

        watchdog = None
        watch_memory = limits.memory_watchdog_mb > 0 and not limits.bypass_memory
        watch_cpu = limits.cpu_percent_limit > 0 and not limits.bypass_cpu
        if IS_POSIX and (watch_memory or watch_cpu):
            watchdog = _ResourceWatchdog(
                proc.pid,
                memory_limit_kb=(
                    limits.memory_watchdog_mb * 1024 if watch_memory else 0
                ),
                cpu_percent_limit=(
                    limits.cpu_percent_limit if watch_cpu else 0
                ),
                cpu_burst_seconds=limits.cpu_burst_seconds,
                interval=limits.watchdog_interval,
                bypass_memory=not watch_memory,
                bypass_cpu=not watch_cpu,
            )
            watchdog.start()

        timed_out = False
        try:
            proc.wait(timeout=limits.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            if watchdog is not None:
                watchdog.stop()
                watchdog.join(timeout=2)

        duration = time.monotonic() - started
        stdout, out_cut = _read_capped(out_file, limits.max_output_bytes)
        stderr, err_cut = _read_capped(err_file, limits.max_output_bytes)

    result = ExecResult(
        argv=argv_list,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration=duration,
        timed_out=timed_out,
        truncated=out_cut or err_cut,
        memory_killed=bool(watchdog and watchdog.triggered_memory),
        cpu_killed=bool(watchdog and watchdog.triggered_cpu),
        timeout=limits.timeout,
    )
    if result.memory_killed and not result.stderr:
        result.stderr = f"进程组 RSS 超过 {limits.memory_watchdog_mb}MB，已整组终止"
    if result.cpu_killed and not result.stderr:
        result.stderr = (
            f"CPU 占用连续 {limits.cpu_burst_seconds:.0f}s 超过 "
            f"{limits.cpu_percent_limit}%，判定为死循环，已整组终止"
        )
    return result


def kill_process_group(pid: int, *, own_pgid: int | None = None) -> bool:
    """只杀子进程自己的进程组。

    万一哪天 start_new_session 被改掉，getpgid(pid) 会等于 AstrBot 自己的组，
    killpg 就会把宿主一起带走 —— 所以这里先比对，相等就拒绝。
    """
    if not IS_POSIX:
        return False
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    mine = own_pgid if own_pgid is not None else os.getpgid(0)
    if pgid == mine:
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _kill_tree(proc: subprocess.Popen) -> None:
    if not IS_POSIX:
        proc.kill()
        return
    if not kill_process_group(proc.pid):
        try:
            proc.kill()
        except OSError:
            pass
