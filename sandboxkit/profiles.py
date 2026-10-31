# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""rootfs 套餐。

Alpine 只有 busybox，跑个 python 都得自己 apk add；Debian 全量又太大。
所以分三档，按「你打算拿沙盒干什么」来选，而不是按发行版选。

explicit 配置永远优先：设了 rootfs_preset / rootfs_image / bootstrap_packages
就以你写的为准，套餐只提供默认值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Profile:
    name: str
    preset: str
    image: str
    packages: tuple[str, ...]
    description: str
    approx_mb: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "preset": self.preset,
            "image": self.image,
            "packages": list(self.packages),
            "description": self.description,
            "approx_mb": self.approx_mb,
        }


COMMON = (
    "python3",
    "python3-pip",
    "ca-certificates",
    "curl",
    "wget",
    "git",
    "jq",
    "unzip",
    "zip",
    "less",
    "nano",
    "procps",
    "file",
)

PROFILES: dict[str, Profile] = {
    "minimal": Profile(
        name="minimal",
        preset="alpine",
        image="alpine:3.20",
        packages=("python3",),
        description="Alpine + python3，3MB 起步，只适合跑脚本",
        approx_mb=20,
    ),
    "standard": Profile(
        name="standard",
        preset="ubuntu",
        image="ubuntu:24.04",
        packages=COMMON,
        description="Ubuntu base + python/git/curl/jq 等常用工具，日常够用",
        approx_mb=200,
    ),
    "full": Profile(
        name="full",
        preset="ubuntu",
        image="ubuntu:24.04",
        packages=COMMON
        + (
            "build-essential",
            "python3-dev",
            "python3-venv",
            "nodejs",
            "npm",
            "sqlite3",
            "rsync",
            "dnsutils",
            "netcat-openbsd",
            "tree",
            "htop",
        ),
        description="Ubuntu + 编译链 / Node / SQLite / 网络工具，能应付大多数场景",
        approx_mb=750,
    ),
}

DEFAULT_PROFILE = "standard"


def get_profile(name: str) -> Profile:
    return PROFILES.get((name or "").strip().lower(), PROFILES[DEFAULT_PROFILE])


def resolve(
    profile_name: str = "",
    *,
    preset: str = "",
    image: str = "",
    packages: Sequence[str] = (),
) -> tuple[str, str, tuple[str, ...]]:
    """显式配置优先，没填的用套餐默认值。"""
    profile = get_profile(profile_name)
    return (
        preset or profile.preset,
        image or profile.image,
        tuple(packages) or profile.packages,
    )
