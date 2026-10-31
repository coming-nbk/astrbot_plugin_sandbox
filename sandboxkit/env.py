# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""探测插件跑在什么环境里。

AstrBot 大多数部署是一个 Docker 容器。这件事直接决定哪些后端可用：

- bwrap 需要非特权用户命名空间，容器里经常被 seccomp / apparmor 挡掉
- chroot 需要 root + CAP_SYS_CHROOT，容器默认是给 root 的，所以通常能用
- docker 后端需要容器里有 docker CLI，并且宿主的 socket 被挂进来
- proot 用户态，什么都不需要，但慢

探测只做只读判断，不改任何东西。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")


@dataclass(frozen=True)
class Runtime:
    in_container: bool
    container_kind: str
    is_root: bool
    has_bwrap: bool
    has_proot: bool
    has_docker_cli: bool
    docker_socket: str
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["notes"] = list(self.notes)
        return data


def _detect_container() -> tuple[bool, str]:
    if Path("/.dockerenv").exists():
        return True, "docker"
    if Path("/run/.containerenv").exists():
        return True, "podman"
    try:
        cgroup = Path("/proc/1/cgroup").read_text("utf-8", errors="replace")
    except OSError:
        cgroup = ""
    lowered = cgroup.lower()
    for kind in ("docker", "kubepods", "containerd", "lxc", "podman"):
        if kind in lowered:
            return True, kind
    return False, "none"


def _find_socket() -> str:
    for path in DOCKER_SOCKETS:
        if Path(path).exists():
            return path
    return ""


def detect() -> Runtime:
    in_container, kind = _detect_container()
    socket = _find_socket()
    notes: list[str] = []

    if in_container and kind == "docker":
        notes.append("检测到插件运行在容器内")
    if in_container and not socket:
        notes.append("容器内没有 docker socket，docker 后端不可用")
    if in_container and not Path("/proc/sys/kernel/unprivileged_userns_clone").exists():
        notes.append("无法确认非特权用户命名空间是否可用，bwrap 可能失败")

    return Runtime(
        in_container=in_container,
        container_kind=kind,
        is_root=os.geteuid() == 0 if hasattr(os, "geteuid") else False,
        has_bwrap=bool(shutil.which("bwrap")),
        has_proot=bool(shutil.which("proot")),
        has_docker_cli=bool(shutil.which("docker")),
        docker_socket=socket,
        notes=tuple(notes),
    )


def advice(runtime: Runtime) -> list[str]:
    """按探测结果给出建议，写进日志和 WebUI 概览。"""
    tips: list[str] = []
    if runtime.in_container:
        if runtime.has_bwrap:
            tips.append("容器内有 bwrap：优先用它，失败再退回 chroot")
        elif runtime.is_root:
            tips.append("容器内是 root 且没有 bwrap：chroot 通常可用，但隔离弱于 bwrap")
        if runtime.docker_socket and runtime.has_docker_cli:
            tips.append(
                "docker socket 已挂载且有 CLI：可用 docker 后端，"
                "建议开 docker_reuse 复用容器，否则每条命令都要新建容器"
            )
        elif runtime.docker_socket and not runtime.has_docker_cli:
            tips.append("挂载了 docker socket 但容器内没有 docker CLI，docker 后端不可用")
        if not runtime.is_root and not runtime.has_bwrap and not runtime.has_proot:
            tips.append("非 root 且无 bwrap/proot：只剩 local，命令会被拒绝执行")
    return tips
