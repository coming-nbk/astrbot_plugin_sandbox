# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""执行后端。

同一个「在沙盒里跑一条命令」的语义，五种实现：

  bwrap   把解压好的 minirootfs 挂成 /，用非特权用户命名空间隔离，推荐
  chroot  chroot 到 minirootfs，需要 root，之后降权到 nobody
  proot   proot 用户态模拟，无特权但慢
  docker  交给容器跑，隔离最彻底，需要 docker
  local   只换工作目录 + rlimit，不隔离文件系统（兜底）

后端只负责把「内层命令」包装成「外层命令」，不负责资源限制（limits.py 管）
也不负责超时（limits.py 管）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import Settings

IS_POSIX = os.name == "posix"


class BackendError(RuntimeError):
    """后端准备阶段失败（容器起不来、镜像不存在之类）。"""


def docker_cli(args: Sequence[str], timeout: float = 60.0) -> tuple[int, str, str]:
    """调一次 docker CLI。测试里可以替换掉这个函数。"""
    try:
        proc = subprocess.run(  # noqa: S603 - 参数由插件拼，不含用户输入
            ["docker", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)
    return (
        proc.returncode,
        (proc.stdout or b"").decode("utf-8", "replace"),
        (proc.stderr or b"").decode("utf-8", "replace"),
    )


@dataclass
class Prepared:
    argv: list[str]
    cwd: str | None
    preexec: Callable[[], None] | None = None
    env_extra: dict[str, str] = field(default_factory=dict)
    note: str = ""
    home: str = ""
    preexec_spec: dict | None = None


def rootfs_ready(rootfs: Path | str | None) -> bool:
    if not rootfs:
        return False
    root = Path(rootfs)
    for candidate in (root / "bin" / "sh", root / "usr" / "bin" / "sh"):
        try:
            # 绝对符号链接不能用 exists()，会跟到宿主根目录
            if candidate.is_symlink() or candidate.exists():
                return True
        except OSError:
            continue
    return False


class Backend:
    name = "base"

    def __init__(self, rootfs: Path | None = None):
        self.rootfs = rootfs

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        return False, "未实现"

    def prepare(
        self,
        inner: Sequence[str],
        *,
        session_dir: Path,
        settings: Settings,
        rootfs: Path | None,
    ) -> Prepared:
        raise NotImplementedError  # quickcheck:ignore - 抽象基类，子类必须实现

    def describe(self) -> str:
        return self.name


class LocalBackend(Backend):
    """兜底：没有隔离，只有工作目录和 rlimit。"""

    name = "local"

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        return True, "无需额外依赖，但不隔离文件系统"

    def prepare(self, inner, *, session_dir, settings, rootfs) -> Prepared:
        return Prepared(
            list(inner), str(session_dir), None, {}, "local", str(session_dir)
        )

    def describe(self) -> str:
        return "local（无文件系统隔离）"


class BwrapBackend(Backend):
    name = "bwrap"

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        if not IS_POSIX:
            return False, "仅支持 Linux"
        if not shutil.which("bwrap"):
            return False, "未安装 bubblewrap（apt install bubblewrap）"
        if not rootfs_ready(rootfs):
            return False, "minirootfs 未就绪"
        return True, "bubblewrap + minirootfs"

    def prepare(self, inner, *, session_dir, settings, rootfs) -> Prepared:
        root = str(rootfs)
        argv = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--ro-bind",
            root,
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--bind",
            str(session_dir),
            "/sandbox",
            "--chdir",
            "/sandbox",
            "--setenv",
            "HOME",
            "/sandbox",
            "--setenv",
            "TMPDIR",
            "/sandbox",
            "--cap-drop",
            "ALL",
        ]
        argv.append("--share-net" if settings.allow_network else "--unshare-net")
        argv.append("--")
        argv.extend(str(item) for item in inner)
        return Prepared(argv, None, None, {}, "bwrap", "/sandbox")

    def describe(self) -> str:
        return "bwrap（minirootfs 挂为 /，可禁网）"


class ChrootBackend(Backend):
    name = "chroot"

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        if not IS_POSIX:
            return False, "仅支持 Linux"
        if os.geteuid() != 0:
            return False, "需要 root 权限"
        if not rootfs_ready(rootfs):
            return False, "minirootfs 未就绪"
        return True, "chroot + minirootfs（root 降权到 nobody）"

    def prepare(self, inner, *, session_dir, settings, rootfs) -> Prepared:
        root = str(rootfs)
        # chroot 之后看到的是 rootfs 内部，宿主上的 session_dir 不在里面。
        # 所以 chroot 模式要求会话目录位于 <rootfs>/sandbox（main 里会这么配）。
        if not (Path(root) / "sandbox").is_dir():
            raise BackendError(
                f"chroot 后端需要 {root}/sandbox 存在（会话目录必须在 rootfs 内）"
            )
        # /sandbox 是所有会话的根，每个用户还有一层 <key>
        jail_home = f"/sandbox/{Path(session_dir).name}"
        if not (Path(root) / "sandbox" / Path(session_dir).name).is_dir():
            raise BackendError(f"chroot 里找不到会话目录 {jail_home}")
        uid = int(settings.chroot_uid)
        gid = int(settings.chroot_gid)

        def _enter() -> None:  # pragma: no cover - 只在子进程里跑
            os.chroot(root)
            os.chdir(jail_home)
            if uid > 0:
                try:
                    os.setgroups([])
                except OSError:
                    pass
                os.setgid(gid)
                os.setuid(uid)

        return Prepared(
            [str(item) for item in inner],
            None,
            _enter,
            {"HOME": jail_home},
            "chroot",
            jail_home,
            {
                "kind": "chroot",
                "rootfs": root,
                "cwd": jail_home,
                "uid": uid,
                "gid": gid,
            },
        )

    def describe(self) -> str:
        return "chroot（需 root；chroot 不是强隔离）"


class ProotBackend(Backend):
    name = "proot"

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        if not IS_POSIX:
            return False, "仅支持 Linux"
        if not shutil.which("proot"):
            return False, "未安装 proot"
        if not rootfs_ready(rootfs):
            return False, "minirootfs 未就绪"
        return True, "proot + minirootfs（用户态，较慢）"

    def prepare(self, inner, *, session_dir, settings, rootfs) -> Prepared:
        argv = [
            "proot",
            "-r",
            str(rootfs),
            "-b",
            f"{session_dir}:/sandbox",
            "-w",
            "/sandbox",
            "-0",
            "--kill-on-exit",
            "--link2symlink",
        ]
        argv.extend(str(item) for item in inner)
        return Prepared(argv, None, None, {"HOME": "/sandbox"}, "proot", "/sandbox")

    def describe(self) -> str:
        return "proot（用户态，无法禁网）"


class DockerBackend(Backend):
    """docker 后端。

    每条命令 `docker run --rm` 的代价是「建容器 + 起进程 + 删容器」，
    实测在 300ms~1s 量级，比命令本身还贵。所以默认开 docker_reuse：
    每个沙盒留一个常驻容器（sleep infinity），命令走 docker exec，
    首次建容器之后再执行就是 30~80ms 的量级。
    """

    name = "docker"

    def __init__(self, rootfs: Path | None = None, runner=None):
        super().__init__(rootfs)
        self._runner = runner or docker_cli
        self._known: set[str] = set()
        self._last_used: dict[str, float] = {}
        self._lock = threading.Lock()

    def available(self, rootfs: Path | None) -> tuple[bool, str]:
        if self._runner is docker_cli and not shutil.which("docker"):
            return False, "未安装 docker 客户端"
        socket = "/var/run/docker.sock"
        if (
            self._runner is docker_cli
            and not os.environ.get("DOCKER_HOST")
            and not os.path.exists(socket)
        ):
            return False, "没有 docker socket（容器内需要 -v /var/run/docker.sock）"
        return True, "docker 容器"

    def _container_name(self, session_dir: Path) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in "_.-" else "-" for ch in session_dir.name
        )
        return f"astrbot-sbx-{safe}"[:60]

    def _run_flags(self, settings: Settings, session_dir: Path) -> list[str]:
        cpus = str(min(settings.max_cores, 8)) if settings.max_cores else "1.0"
        flags = [
            "--network",
            "bridge" if settings.allow_network else "none",
            "--memory",
            f"{settings.memory_mb}m",
            "--cpus",
            cpus,
            "--pids-limit",
            str(settings.max_procs or 64),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "-v",
            f"{session_dir}:/sandbox",
            "-w",
            "/sandbox",
            "--env",
            "HOME=/sandbox",
            "--label",
            "astrbot.sandbox=1",
        ]
        if settings.docker_user:
            flags.extend(["--user", settings.docker_user])
        return flags

    def _ensure(self, name: str, session_dir: Path, settings: Settings) -> None:
        with self._lock:
            if name in self._known:
                self._last_used[name] = time.time()
                return

        code, out, _err = self._runner(
            ["inspect", "-f", "{{.State.Running}}", name]
        )
        running = code == 0 and out.strip() == "true"
        if not running:
            self._runner(["rm", "-f", name])
            code, _out, err = self._runner(
                [
                    "run",
                    "-d",
                    "--name",
                    name,
                    *self._run_flags(settings, session_dir),
                    settings.docker_image,
                    "sleep",
                    "infinity",
                ]
            )
            if code != 0:
                raise BackendError(
                    f"启动沙盒容器失败：{(err or '未知错误').strip()[:300]}"
                )

        with self._lock:
            self._known.add(name)
            self._last_used[name] = time.time()

    def prepare(self, inner, *, session_dir, settings, rootfs) -> Prepared:
        if not settings.docker_reuse:
            argv = [
                "docker",
                "run",
                "--rm",
                "-i",
                *self._run_flags(settings, session_dir),
                settings.docker_image,
                *[str(item) for item in inner],
            ]
            return Prepared(argv, None, None, {}, "docker", "/sandbox")

        name = self._container_name(session_dir)
        self._ensure(name, session_dir, settings)
        argv = [
            "docker",
            "exec",
            "-i",
            "-w",
            "/sandbox",
            "-e",
            "HOME=/sandbox",
            name,
            *[str(item) for item in inner],
        ]
        return Prepared(argv, None, None, {}, "docker", "/sandbox")

    def prune(self, idle_seconds: float = 1800.0) -> list[str]:
        """停掉闲置太久的容器，返回被删的容器名。"""
        now = time.time()
        removed: list[str] = []
        with self._lock:
            candidates = [
                (name, last)
                for name, last in self._last_used.items()
                if now - last >= idle_seconds
            ]
        for name, _last in candidates:
            code, _out, _err = self._runner(["rm", "-f", name])
            if code == 0:
                removed.append(name)
            with self._lock:
                self._known.discard(name)
                self._last_used.pop(name, None)
        return removed

    def cleanup_orphans(self) -> list[str]:
        """清掉上次进程崩溃留下的沙盒容器（按 label 找）。"""
        code, out, _err = self._runner(
            ["ps", "-aq", "--filter", "label=astrbot.sandbox=1"]
        )
        if code != 0:
            return []
        names = [line.strip() for line in out.splitlines() if line.strip()]
        for name in names:
            self._runner(["rm", "-f", name])
        return names

    def describe(self) -> str:
        return "docker（容器隔离，复用容器）"


BACKENDS: dict[str, type[Backend]] = {
    "local": LocalBackend,
    "bwrap": BwrapBackend,
    "chroot": ChrootBackend,
    "proot": ProotBackend,
    "docker": DockerBackend,
}

# auto 的挑选顺序：隔离强度 / 易用性折中
AUTO_ORDER = ("bwrap", "chroot", "proot", "docker", "local")


def select_backend(
    settings: Settings, rootfs: Path | None
) -> tuple[Backend, list[str]]:
    """返回实际使用的后端和需要告知用户的说明。"""
    notes: list[str] = []
    wanted = settings.backend if settings.backend in BACKENDS else "auto"

    if wanted != "auto":
        backend = BACKENDS[wanted](rootfs)
        ok, why = backend.available(rootfs)
        if ok:
            return backend, notes
        notes.append(f"配置的 backend={wanted} 不可用（{why}），已回落到 local")
        return LocalBackend(rootfs), notes

    for name in AUTO_ORDER:
        candidate = BACKENDS[name](rootfs)
        ok, _why = candidate.available(rootfs)
        if ok:
            if name == "local":
                notes.append("没有可用的隔离后端，已回落到 local（不隔离文件系统）")
            return candidate, notes
    return LocalBackend(rootfs), notes
