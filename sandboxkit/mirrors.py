# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""爬镜像站的目录索引，拿「真实存在」的 rootfs 链接。

为什么不再写死文件名：Alpine 的目录是 v3.20 而文件名是 3.20.3；Ubuntu 的文件名
带小版本号（24.04.1）；Debian 干脆没有官方 tar。硬编码每次都要赌对，赌错就是 404。

这里的做法：
1. 抓 releases 根目录，列出所有版本目录
2. 进最新几个版本目录，按文件名规则挑最新的那个文件
3. 全部失败返回空列表，不抛异常 —— 上层还有静态地址兜底

所有网络失败都静默降级，因为「探测」本身不该让插件起不来。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from typing import Iterable

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) astrbot-plugin-sandbox/1.4"
TIMEOUT = 6.0  # 单个请求
BUDGET = 25.0  # 一轮解析的总预算，超了就带着已有的结果返回

ALPINE_BASES = (
    "https://mirrors.tuna.tsinghua.edu.cn/alpine",
    "https://dl-cdn.alpinelinux.org/alpine",
)
UBUNTU_BASES = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases",
    "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases",
)

ARCH_MAP = {
    "alpine": {"x86_64": "x86_64", "aarch64": "aarch64", "armv7": "armv7", "x86": "x86"},
    "ubuntu": {"x86_64": "amd64", "aarch64": "arm64", "armv7": "armhf"},
    "debian": {"x86_64": "amd64", "aarch64": "arm64v8", "armv7": "arm32v7"},
}


def _hrefs(url: str, deadline: float | None = None) -> list[str]:
    """抓一个目录页里的链接。失败或超预算返回空列表。"""
    if deadline is not None and time.monotonic() > deadline:
        return []
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(512 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return re.findall(r'href="([^"]+)"', body)


def _version_key(name: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", name)
    return tuple(int(item) for item in numbers[:4]) or (0,)


def _newest(names: Iterable[str], pattern: re.Pattern) -> str | None:
    hits = [name for name in names if pattern.search(name)]
    return max(hits, key=_version_key) if hits else None


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def alpine_urls(arch: str, version: str = "", deadline: float | None = None) -> list[str]:
    """Alpine minirootfs：latest-stable 优先，再按版本目录从新到旧。"""
    alpine_arch = ARCH_MAP["alpine"].get(arch, arch)
    pattern = re.compile(
        rf"alpine-minirootfs-[\d.]+-{re.escape(alpine_arch)}\.tar\.gz$"
    )
    urls: list[str] = []

    for base in ALPINE_BASES:
        index = f"{base}/latest-stable/releases/{alpine_arch}/"
        newest = _newest(_hrefs(index, deadline), pattern)
        if newest:
            urls.append(index + newest)

    if version:
        parts = str(version).split(".")
        directory = "v" + ".".join(parts[:2])
        for base in ALPINE_BASES:
            index = f"{base}/{directory}/releases/{alpine_arch}/"
            newest = _newest(_hrefs(index, deadline), pattern)
            if newest:
                urls.append(index + newest)

    for base in ALPINE_BASES:
        root = _hrefs(base + "/", deadline)
        directories = sorted(
            (name for name in root if re.fullmatch(r"v\d+\.\d+/", name)),
            key=_version_key,
            reverse=True,
        )
        for directory in directories[:2]:
            index = f"{base}/{directory}releases/{alpine_arch}/"
            newest = _newest(_hrefs(index, deadline), pattern)
            if newest:
                urls.append(index + newest)

    return _dedupe(urls)


def ubuntu_urls(arch: str, deadline: float | None = None) -> list[str]:
    """Ubuntu base：先列版本目录，再进 release/ 挑文件。"""
    ubuntu_arch = ARCH_MAP["ubuntu"].get(arch, arch)
    pattern = re.compile(
        rf"ubuntu-base-[\d.]+-base-{re.escape(ubuntu_arch)}\.tar\.gz$"
    )
    urls: list[str] = []

    for base in UBUNTU_BASES:
        root = _hrefs(base + "/", deadline)
        # 优先稳定的老 LTS：26.04 把 coreutils 换成了 Rust 版，
        # 在没有 /proc 的 chroot 里会直接 panic，24.04 / 22.04 没这个问题。
        PREFERRED = ("24.04", "22.04")

        def _lts_first(name: str):
            for rank, prefix in enumerate(PREFERRED):
                if name.startswith(prefix):
                    return (100 - rank, _version_key(name))
            parts = name.split(".")
            year = int(parts[0]) if parts and parts[0].isdigit() else 0
            is_lts = len(parts) > 1 and parts[1] == "04" and year % 2 == 0
            return (1 if is_lts else 0, _version_key(name))

        versions = sorted(
            (
                name.strip("/")
                for name in root
                if re.fullmatch(r"\d{2}\.\d{2}(\.\d+)?/", name)
            ),
            key=_lts_first,
            reverse=True,
        )
        for version in versions[:3]:
            index = f"{base}/{version}/release/"
            newest = _newest(_hrefs(index, deadline), pattern)
            if newest:
                urls.append(index + newest)

    return _dedupe(urls)


def debian_urls(arch: str) -> list[str]:
    """Debian 没有官方 rootfs tar，只能试第三方产物；失败无所谓。"""
    branch = ARCH_MAP["debian"].get(arch)
    if not branch:
        return []
    base = (
        "https://github.com/debuerreotype/docker-debian-artifacts/raw/"
        f"dist-{branch}"
    )
    return [f"{base}/bookworm/slim/rootfs.tar.xz", f"{base}/bookworm/rootfs.tar.xz"]


def resolve_all(
    arch: str, version: str = "", budget: float = BUDGET
) -> list[tuple[str, str]]:
    """返回 [(preset, url)]，顺序就是尝试顺序。

    有总预算：镜像站一个都连不上时，最多卡 budget 秒就带着已有结果返回，
    不会让插件启动一直挂着。
    """
    deadline = time.monotonic() + max(3.0, budget)
    items: list[tuple[str, str]] = []
    items.extend(("alpine", url) for url in alpine_urls(arch, version, deadline))
    items.extend(("ubuntu", url) for url in ubuntu_urls(arch, deadline))
    items.extend(("debian", url) for url in debian_urls(arch))
    return items


def probe(url: str, *, timeout: float = 10.0) -> dict:
    """探测一个地址到底通不通：先 HEAD，不支持再退回 Range GET。"""
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": USER_AGENT}
        )
        if method == "GET":
            request.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                return {
                    "url": url,
                    "ok": True,
                    "status": getattr(response, "status", 200),
                    "bytes": int(length) if length and length.isdigit() else 0,
                }
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405):
                continue
            return {"url": url, "ok": False, "status": exc.code, "error": str(exc)}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"url": url, "ok": False, "status": 0, "error": str(exc)}
    return {"url": url, "ok": False, "status": 0, "error": "HEAD/GET 都失败"}
