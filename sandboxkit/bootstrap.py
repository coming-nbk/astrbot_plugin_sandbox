# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""拉取并解压 rootfs —— 沙盒的「根文件系统」。

支持三种预置，都是官方/半官方产物，一个 URL 就能下：

  debian  debuerreotype 打好的 bookworm slim rootfs.tar.xz（默认，apt + bash + coreutils）
  ubuntu  cdimage 的 ubuntu-base（apt + bash）
  alpine  Alpine minirootfs（3MB，只有 busybox，适合只跑脚本的场景）

流程：下载 -> 校验 sha256 -> 安全解压 -> 打标记。四步都可重入：
标记存在且 /bin/sh 在，就直接复用，不会重复下载。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import mirrors

USER_AGENT = "astrbot-plugin-sandbox/1.0"
ARCH_MAP = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv7l": "armv7",
    "armv8l": "armv7",
    "i686": "x86",
    "i386": "x86",
}


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preset:
    name: str
    default_version: str
    template: str
    arch_map: dict[str, str]
    description: str

    def url(self, version: str, arch: str) -> str:
        dir_version = ".".join(str(version).split(".")[:2])
        return self.template.format(
            version=version,
            dir_version=dir_version,
            arch=self.arch_map.get(arch, arch),
        )


PRESETS: dict[str, Preset] = {
    "debian": Preset(
        name="debian",
        default_version="bookworm",
        template=(
            "https://github.com/debuerreotype/docker-debian-artifacts/raw/"
            "dist-{arch}/{version}/slim/rootfs.tar.xz"
        ),
        arch_map={"x86_64": "amd64", "aarch64": "arm64v8", "armv7": "arm32v7", "x86": "i386"},
        description="Debian 12 slim：apt、bash、coreutils，约 30MB",
    ),
    "ubuntu": Preset(
        name="ubuntu",
        default_version="24.04",
        template=(
            "https://cdimage.ubuntu.com/ubuntu-base/releases/{version}/release/"
            "ubuntu-base-{version}-base-{arch}.tar.gz"
        ),
        arch_map={"x86_64": "amd64", "aarch64": "arm64", "armv7": "armhf"},
        description="Ubuntu base：apt、bash，约 30MB",
    ),
    "alpine": Preset(
        name="alpine",
        default_version="3.20.3",
        # 目录只到 major.minor，文件名才带完整版本：v3.20/releases/.../alpine-minirootfs-3.20.3-...
        template=(
            "https://dl-cdn.alpinelinux.org/alpine/v{dir_version}/releases/{arch}/"
            "alpine-minirootfs-{version}-{arch}.tar.gz"
        ),
        arch_map={"x86_64": "x86_64", "aarch64": "aarch64", "armv7": "armv7", "x86": "x86"},
        description="Alpine minirootfs：只有 busybox，约 3MB",
    ),
}

# 默认用 Alpine：3MB、下载快，国内镜像也全
DEFAULT_PRESET = "alpine"

# Alpine 的两个源：清华镜像优先（国内快），官方 CDN 兜底
ALPINE_MIRRORS = (
    "https://mirrors.tuna.tsinghua.edu.cn/alpine/v{version}/releases/{arch}/"
    "alpine-minirootfs-{version}-{arch}.tar.gz",
    "https://dl-cdn.alpinelinux.org/alpine/v{version}/releases/{arch}/"
    "alpine-minirootfs-{version}-{arch}.tar.gz",
)

# 一个源 404 不该让插件不可用，按顺序兜底
FALLBACK_PRESETS = (
    ("alpine", "3.20.3"),
    ("ubuntu", "24.04"),
    ("debian", "bookworm"),
)


@dataclass
class RootfsInfo:
    version: str
    arch: str
    url: str
    sha256: str
    installed_at: str
    packages: list[str]
    preset: str = DEFAULT_PRESET
    bytes: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def detect_arch() -> str:
    machine = platform.machine().lower()
    return ARCH_MAP.get(machine, machine)


def default_url(version: str, arch: str) -> str:
    """Alpine 的地址。保留这个名字是因为旧配置和测试都在用。"""
    return PRESETS["alpine"].url(version, PRESETS["alpine"].arch_map.get(arch, arch))


def resolve_rootfs(
    preset: str,
    version: str = "",
    arch: str | None = None,
) -> tuple[str, str]:
    """返回 (下载地址, 实际版本)。"""
    key = (preset or DEFAULT_PRESET).strip().lower()
    if key not in PRESETS:
        raise BootstrapError(
            f"未知 rootfs 预置「{preset}」，可选：{'、'.join(PRESETS)}"
        )
    spec = PRESETS[key]
    resolved_version = version.strip() or spec.default_version
    resolved_arch = arch or detect_arch()
    return spec.url(resolved_version, resolved_arch), resolved_version


def marker_path(rootfs: Path) -> Path:
    return rootfs.parent / f".{rootfs.name}.bootstrap.json"


def has_shell(rootfs: Path) -> bool:
    """判断 rootfs 里有没有可用的 shell。

    注意不能用 Path.exists()：Alpine 的 /bin/sh 是指向 /bin/busybox 的**绝对**
    符号链接，exists() 会跟到宿主根目录去，永远为假。
    """
    for candidate in (
        rootfs / "bin" / "sh",
        rootfs / "usr" / "bin" / "sh",
        rootfs / "bin" / "busybox",
    ):
        try:
            if candidate.is_symlink() or candidate.exists():
                return True
        except OSError:
            continue
    return False


def is_ready(rootfs: Path) -> bool:
    return rootfs.is_dir() and has_shell(rootfs)


def read_marker(rootfs: Path) -> RootfsInfo | None:
    path = marker_path(rootfs)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8-sig"))
        return RootfsInfo(**data)
    except (OSError, ValueError, TypeError):
        return None


def package_manager(rootfs: Path) -> str:
    """看 rootfs 里装的是哪家的包管理器。"""
    if (rootfs / "usr" / "bin" / "apt-get").exists() or (
        rootfs / "usr" / "bin" / "apt"
    ).exists():
        return "apt"
    if (rootfs / "sbin" / "apk").exists() or (rootfs / "usr" / "sbin" / "apk").exists():
        return "apk"
    return ""


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_source(url: str) -> str:
    """允许直接给本地 tar 包路径，方便内网/离线部署。"""
    text = str(url or "").strip()
    if not text or text.startswith(("http://", "https://", "file://")):
        return text
    candidate = Path(text).expanduser()
    if candidate.is_file():
        return candidate.resolve().as_uri()
    return text


def strip_suid(rootfs: Path, *, limit: int = 50000) -> int:
    """清掉 rootfs 里所有 setuid/setgid 位，返回清掉的个数。

    解压时已经清过一次，但 `apt install` 会把 su / passwd / mount / sudo
    这些带 setuid 的程序装回来。沙盒进程虽然是 nobody，一执行它们就回到
    euid 0 —— 而 uid 0 在 chroot 外就是真 root。所以每次装完包都要再扫一遍。
    """
    cleared = 0
    seen = 0
    for current, dirs, files in os.walk(str(rootfs)):
        for name in dirs + files:
            path = os.path.join(current, name)
            seen += 1
            if seen > limit:
                return cleared
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                try:
                    os.chmod(path, stat.S_IMODE(info.st_mode) & 0o777)
                except OSError:
                    continue
                cleared += 1
    return cleared


def download(url: str, dest: Path, *, timeout: float = 300.0) -> int:
    """流式下载到 dest，返回字节数。失败抛 BootstrapError。"""
    url = _normalize_source(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status not in (None, 200):
                raise BootstrapError(f"下载失败，HTTP {status}")
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    handle.write(chunk)
                    total += len(chunk)
    except urllib.error.URLError as exc:
        raise BootstrapError(f"下载失败：{exc}") from exc
    except OSError as exc:
        raise BootstrapError(f"写入失败：{exc}") from exc
    if total == 0:
        raise BootstrapError("下载得到空文件")
    return total


def _validate_member(member: tarfile.TarInfo, dest_real: str) -> None:
    name = member.name
    if not name or name.startswith("/") or name.startswith(".."):
        raise BootstrapError(f"压缩包内出现可疑路径：{name}")
    if any(part == ".." for part in Path(name).parts):
        raise BootstrapError(f"压缩包内出现可疑路径：{name}")

    target = os.path.realpath(os.path.join(dest_real, name))
    if not _within(target, dest_real):
        raise BootstrapError(f"压缩包成员越界：{name}")

    if member.issym():
        link = member.linkname
        if os.path.isabs(link):
            # 绝对链接在沙盒内部解析，落点仍是沙盒自己的根，允许
            return
        resolved = os.path.realpath(os.path.join(os.path.dirname(target), link))
        if not _within(resolved, dest_real):
            raise BootstrapError(f"符号链接越界：{name} -> {link}")
    elif member.islnk():
        link = member.linkname
        if os.path.isabs(link):
            raise BootstrapError(f"硬链接指向绝对路径：{name} -> {link}")
        # 硬链接的目标相对压缩包根目录，不是相对成员所在目录
        resolved = os.path.realpath(os.path.join(dest_real, link))
        if not _within(resolved, dest_real):
            raise BootstrapError(f"硬链接越界：{name} -> {link}")


def safe_extract(tar_path: Path, dest: Path, *, skip_special: bool = True) -> int:
    """解压到 dest（dest 必须已存在），返回解出的成员数。"""
    dest_real = os.path.realpath(str(dest))
    count = 0
    with tarfile.open(tar_path, "r:*") as tar:
        members = []
        for member in tar.getmembers():
            if member.isdev() or member.isfifo():
                if skip_special:
                    continue
                raise BootstrapError(f"压缩包内出现设备文件：{member.name}")
            _validate_member(member, dest_real)
            # 解压出来的 rootfs 不该带 setuid/setgid：沙盒内提权没有意义，
            # 却能让降权后的进程重新拿到 root-in-chroot
            member.mode &= 0o0777
            members.append(member)
        try:
            tar.extractall(dest_real, members=members, filter="fully_trusted")
        except TypeError:  # Python < 3.12
            tar.extractall(dest_real, members=members)
        count = len(members)
    return count


def bootstrap(
    rootfs: Path,
    *,
    preset: str = DEFAULT_PRESET,
    version: str = "",
    arch: str | None = None,
    url: str = "",
    expected_sha256: str = "",
    packages: Sequence[str] = (),
    force: bool = False,
    timeout: float = 300.0,
    log: Callable[[str], None] = print,
) -> RootfsInfo:
    """确保 rootfs 可用。已就绪且未 force 时直接返回记录。"""
    arch = arch or detect_arch()
    if url:
        target_url = _normalize_source(url)
        resolved_version = version or PRESETS.get(preset, PRESETS[DEFAULT_PRESET]).default_version
    else:
        target_url, resolved_version = resolve_rootfs(preset, version, arch)

    if target_url.startswith("http://") and not expected_sha256:
        raise BootstrapError(
            "拒绝明文 HTTP 源：中间人可以替换压缩包，装进去就是 root 代码。\n"
            "改用 https，或者配上 rootfs_sha256 再下。"
        )

    existing = read_marker(rootfs)
    if not force and is_ready(rootfs) and existing is not None:
        log(f"rootfs 已就绪：{existing.preset} {existing.version} {existing.arch}")
        return existing

    rootfs.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rootfs-dl-", dir=str(rootfs.parent)) as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "rootfs.tar"
        log(f"下载 rootfs：{target_url}")
        size = download(target_url, archive, timeout=timeout)

        digest = sha256_of(archive)
        if expected_sha256:
            if digest.lower() != expected_sha256.lower():
                raise BootstrapError(
                    f"sha256 不匹配：期望 {expected_sha256}，实际 {digest}"
                )
            log("sha256 校验通过")
        else:
            log(f"未配置 rootfs_sha256，仅记录实际值 {digest}")

        staging = tmp_dir / "rootfs"
        staging.mkdir()
        log("解压中")
        members = safe_extract(archive, staging)
        strip_suid(staging)
        if not has_shell(staging):
            raise BootstrapError("解压完成但找不到 /bin/sh，可能不是完整的 rootfs")

        # chroot 模式下会话目录就在 rootfs/sandbox 里，换 rootfs 前先挪出来
        kept = None
        sessions = rootfs / "sandbox"
        if sessions.is_dir():
            kept = tmp_dir / "sessions-keep"
            shutil.move(str(sessions), str(kept))
            log(f"保留会话目录（{len(list(kept.iterdir()))} 个用户）")

        if rootfs.exists():
            shutil.rmtree(rootfs)
        shutil.move(str(staging), str(rootfs))
        if kept is not None:
            target = rootfs / "sandbox"
            target.mkdir(parents=True, exist_ok=True)
            for child in kept.iterdir():
                shutil.move(str(child), str(target / child.name))
            log("会话目录已放回新 rootfs")
        log(f"解压完成，{members} 个成员")

        info = RootfsInfo(
            version=resolved_version,
            arch=arch,
            url=target_url,
            sha256=digest,
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            packages=[],
            preset=preset,
            bytes=size,
        )

        ensure_dev_nodes(rootfs, log=log)
        if packages:
            ensure_resolv_conf(rootfs)
            ok, message = install_packages(rootfs, packages, log=log)
            if ok:
                info.packages = [str(item) for item in packages]
            else:
                log(f"安装额外软件包失败：{message}")

        marker_path(rootfs).write_text(info.to_json(), "utf-8")
        return info


def _export_container(name: str, archive: Path, timeout: float) -> tuple[bool, str]:
    """把容器导出成 tar。单独拆出来是为了测试能替换掉。"""
    try:
        with open(archive, "wb") as handle:
            proc = subprocess.run(
                ["docker", "export", name],
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or b"").decode("utf-8", "replace")[-500:]
    return True, ""


def from_docker_image(
    image: str,
    rootfs: Path,
    *,
    packages: Sequence[str] = (),
    force: bool = False,
    timeout: float = 600.0,
    log: Callable[[str], None] = print,
    runner: Callable | None = None,
) -> RootfsInfo:
    """从本地已有的 docker 镜像里导出 rootfs。

    比下载靠谱：镜像通常已经在缓存里，不依赖任何外网地址；导出的是一张干净
    的基镜像，不含 AstrBot 的配置和密钥。
    """
    from .backends import docker_cli

    runner = runner or docker_cli
    name = f"astrbot-sbx-rootfs-{uuid.uuid4().hex[:8]}"

    if not force and is_ready(rootfs) and read_marker(rootfs) is not None:
        log("rootfs 已就绪，跳过导出")
        return read_marker(rootfs)  # type: ignore[return-value]

    code, _out, err = runner(["create", "--name", name, image])
    if code != 0:
        raise BootstrapError(f"docker create {image} 失败：{(err or '').strip()[:200]}")

    rootfs.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="rootfs-img-", dir=str(rootfs.parent)
        ) as tmp:
            tmp_dir = Path(tmp)
            archive = tmp_dir / "rootfs.tar"
            log(f"从镜像导出 rootfs：{image}")
            ok, message = _export_container(name, archive, timeout)
            if not ok:
                raise BootstrapError(f"docker export 失败：{message}")
            if archive.stat().st_size == 0:
                raise BootstrapError("docker export 得到空文件")

            staging = tmp_dir / "rootfs"
            staging.mkdir()
            members = safe_extract(archive, staging)
            if not has_shell(staging):
                raise BootstrapError("导出的镜像里没有 /bin/sh")

            kept = None
            sessions = rootfs / "sandbox"
            if sessions.is_dir():
                kept = tmp_dir / "sessions-keep"
                shutil.move(str(sessions), str(kept))
                log("保留会话目录")

            if rootfs.exists():
                shutil.rmtree(rootfs)
            shutil.move(str(staging), str(rootfs))
            if kept is not None:
                target = rootfs / "sandbox"
                target.mkdir(parents=True, exist_ok=True)
                for child in kept.iterdir():
                    shutil.move(str(child), str(target / child.name))

            info = RootfsInfo(
                version=image,
                arch=detect_arch(),
                url=f"docker:{image}",
                sha256=sha256_of(archive),
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                packages=[],
                preset="docker-image",
                bytes=archive.stat().st_size,
            )
            log(f"导出完成，{members} 个成员")
    finally:
        runner(["rm", "-f", name])

    ensure_dev_nodes(rootfs, log=log)
    if packages:
        ensure_resolv_conf(rootfs)
        ok, message = install_packages(rootfs, packages, log=log)
        if ok:
            info.packages = [str(item) for item in packages]
        else:
            log(f"安装额外软件包失败：{message}")

    marker_path(rootfs).write_text(info.to_json(), "utf-8")
    return info


def bootstrap_with_fallback(
    rootfs: Path,
    *,
    preset: str = DEFAULT_PRESET,
    version: str = "",
    arch: str | None = None,
    url: str = "",
    image: str = "",
    expected_sha256: str = "",
    packages: Sequence[str] = (),
    force: bool = False,
    timeout: float = 300.0,
    log: Callable[[str], None] = print,
) -> RootfsInfo:
    """按「本地镜像 -> 配置的源 -> 其它源」依次尝试，全失败才抛错。

    单个地址说没就没（GitHub 上的第三方产物尤其），只认一个源等于把可用性
    交给别人。国内优先清华镜像。
    """
    arch = arch or detect_arch()
    attempts: list[tuple[str, str, str]] = []

    def add(item_preset: str, item_version: str, item_url: str) -> None:
        if not item_url:
            return
        if all(item_url != existing[2] for existing in attempts):
            attempts.append((item_preset, item_version, item_url))

    if url:
        add(preset, version, url)
    else:
        # 先把这个发行版的所有可能都试完（静态 + 索引），再换下一个发行版。
        # 否则 Alpine 的静态地址一旦能用，就永远轮不到 Ubuntu 的索引地址。
        crawled = mirrors.resolve_all(arch, version)
        by_preset: dict[str, list[str]] = {}
        for item_preset, item_url in crawled:
            by_preset.setdefault(item_preset, []).append(item_url)

        def add_preset(item_preset: str, item_version: str) -> None:
            # Ubuntu 的文件名带小版本号，静态拼不准，直接走索引
            if item_preset != "ubuntu":
                try:
                    static_url, static_version = resolve_rootfs(
                        item_preset, item_version, arch
                    )
                except BootstrapError:
                    static_url, static_version = "", ""
                add(item_preset, static_version or item_version, static_url)
            for item_url in by_preset.get(item_preset, []):
                add(item_preset, item_version, item_url)

        add_preset(preset, version)
        for name, fallback_version in FALLBACK_PRESETS:
            if name == preset:
                continue
            add_preset(name, fallback_version)

    failures: list[str] = []
    if image:
        try:
            return from_docker_image(
                image,
                rootfs,
                packages=packages,
                force=force,
                timeout=timeout,
                log=log,
            )
        except BootstrapError as exc:
            failures.append(f"docker:{image}\n    {exc}")
            log(f"镜像 {image} 不可用：{exc}")

    def _try(item_preset: str, item_version: str, item_url: str):
        try:
            return bootstrap(
                rootfs,
                preset=item_preset,
                version=item_version,
                arch=arch,
                url=item_url,
                expected_sha256=expected_sha256 if item_preset == preset else "",
                packages=packages,
                force=force,
                timeout=timeout,
                log=log,
            )
        except BootstrapError as exc:
            failures.append(f"{item_url}\n    {exc}")
            log(f"{item_preset} 源不可用：{exc}")
            return None

    for attempt_preset, attempt_version, attempt_url in attempts:
        result = _try(attempt_preset, attempt_version, attempt_url)
        if result is not None:
            return result

    # 2) 静态地址全挂了才去爬索引：文件名会变，这里拿到的是当前真实存在的
    if not url:
        crawled = mirrors.resolve_all(arch, version)
        log(f"静态地址都不可用，开始爬镜像索引（{len(crawled)} 个候选）")
        for item_preset, item_url in crawled:
            result = _try(item_preset, "", item_url)
            if result is not None:
                return result

    raise BootstrapError(
        "所有 rootfs 源都失败了。可以填 rootfs_url（支持本地 tar 路径）：\n  "
        + "\n  ".join(failures)
    )


# chroot 之后 /dev 是空的，shell 连 `> /dev/null` 都做不了（降权后 /dev 不可写）
DEV_NODES = (
    ("null", 1, 3, 0o666),
    ("zero", 1, 5, 0o666),
    ("full", 1, 7, 0o666),
    ("random", 1, 8, 0o666),
    ("urandom", 1, 9, 0o666),
    ("tty", 5, 0, 0o666),
)


def ensure_dev_nodes(rootfs: Path, log: Callable[[str], None] = print) -> list[str]:
    """在 rootfs 里补上最基本的字符设备。需要 CAP_MKNOD（Docker 默认有）。"""
    dev = Path(rootfs) / "dev"
    created: list[str] = []
    try:
        dev.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"无法创建 {dev}：{exc}")
        return created

    for name, major, minor, mode in DEV_NODES:
        target = dev / name
        try:
            if target.exists() or target.is_symlink():
                continue
        except OSError:
            continue
        try:
            os.mknod(target, stat.S_IFCHR | mode, os.makedev(major, minor))
            os.chmod(target, mode)
            created.append(name)
        except (OSError, AttributeError) as exc:
            log(f"无法创建 /dev/{name}：{exc}")
            continue

    # /dev/fd 指向 /proc/self/fd，很多脚本依赖它
    for name in ("pts", "shm"):
        try:
            (dev / name).mkdir(exist_ok=True)
            os.chmod(dev / name, 0o1777)
        except OSError:
            continue

    for link, target_path in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                              ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2")):
        path = dev / link
        try:
            if path.exists() or path.is_symlink():
                continue
            path.symlink_to(target_path)
        except OSError:
            continue

    if created:
        log(f"已补 /dev 设备：{' '.join(created)}")
    return created


def _run_in_rootfs(
    rootfs: Path,
    inner: Sequence[str],
    *,
    timeout: float,
) -> tuple[bool, str]:
    """在 rootfs 里跑一条命令：优先 bwrap（有 /proc、/dev），否则 chroot。"""
    argv: list[str]
    kwargs: dict[str, object] = {"cwd": None}
    if shutil.which("bwrap"):
        argv = [
            "bwrap",
            "--die-with-parent",
            "--bind",
            str(rootfs),
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--share-net",
            "--",
            *inner,
        ]
    elif os.geteuid() == 0 and hasattr(os, "chroot"):
        argv = list(inner)

        def _enter() -> None:  # pragma: no cover - 只在子进程里跑
            os.chroot(str(rootfs))
            os.chdir("/")

        kwargs["preexec_fn"] = _enter
    else:
        return False, "需要 root 或 bwrap 才能在 rootfs 里安装软件包"

    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "DEBIAN_FRONTEND": "noninteractive",
        "LANG": "C.UTF-8",
    }
    try:
        proc = subprocess.run(  # noqa: S603 - 参数来自配置，非用户输入
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    stdout = (proc.stdout or b"")[-2000:].decode("utf-8", "replace")
    stderr = (proc.stderr or b"")[-2000:].decode("utf-8", "replace")
    if proc.returncode != 0:
        return False, stderr or stdout or f"exit={proc.returncode}"
    return True, stdout


# Debian 的包名和 Alpine 不完全一样；跨发行版回落时按这张表翻译
APK_ALIASES = {
    "python3-pip": "py3-pip",
    "python3-venv": "py3-virtualenv",
    "python3-dev": "python3-dev",
    "build-essential": "build-base",
    "sqlite3": "sqlite",
    "dnsutils": "bind-tools",
    "netcat-openbsd": "netcat-openbsd",
    "htop": "htop",
    "tree": "tree",
    "rsync": "rsync",
}


def _translate(names: Sequence[str], manager: str) -> list[str]:
    if manager == "apk":
        return [APK_ALIASES.get(name, name) for name in names]
    return list(names)


def upgrade_packages(
    rootfs: Path,
    *,
    log: Callable[[str], None] = print,
    timeout: float = 900.0,
) -> tuple[bool, str]:
    """把 rootfs 里的软件包升到最新 —— 这是沙盒内部唯一的「一键修复」。"""
    manager = package_manager(rootfs)
    if manager == "apt":
        log("apt-get update")
        ok, output = _run_in_rootfs(
            rootfs, ["/usr/bin/apt-get", "update"], timeout=timeout
        )
        if not ok:
            return False, f"apt-get update 失败：{output}"
        log("apt-get upgrade")
        ok, output = _run_in_rootfs(
            rootfs,
            ["/usr/bin/apt-get", "-y", "--no-install-recommends", "upgrade"],
            timeout=timeout,
        )
    elif manager == "apk":
        log("apk upgrade")
        ok, output = _run_in_rootfs(rootfs, ["/sbin/apk", "upgrade"], timeout=timeout)
    else:
        return False, "rootfs 里既没有 apk 也没有 apt-get"

    cleared = strip_suid(rootfs)
    if cleared:
        log(f"升级后清掉 {cleared} 个 setuid 位")
    return ok, output


def install_packages(
    rootfs: Path,
    packages: Iterable[str],
    *,
    log: Callable[[str], None] = print,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """在 rootfs 里装软件包，自动识别 apt / apk。

    先整批装（快）；失败就一个一个装 —— 某个包名在别的发行版里不存在时，
    不该把其余能装的包一起拖死。
    """
    requested = [str(item).strip() for item in packages if str(item).strip()]
    if not requested:
        return True, ""

    manager = package_manager(rootfs)
    if manager not in ("apk", "apt"):
        return False, "rootfs 里既没有 apk 也没有 apt-get，无法安装软件包"

    names = _translate(requested, manager)
    log(f"安装软件包（{manager}）：{' '.join(names)}")

    if manager == "apt":
        ok, output = _run_in_rootfs(
            rootfs, ["/usr/bin/apt-get", "update"], timeout=timeout
        )
        if not ok:
            return False, f"apt-get update 失败：{output}"

    def command(pkg_list: Sequence[str]) -> list[str]:
        if manager == "apk":
            return ["/sbin/apk", "add", "--no-cache", *pkg_list]
        return [
            "/usr/bin/apt-get",
            "-o",
            "Dpkg::Use-Pty=0",
            "install",
            "-y",
            "--no-install-recommends",
            *pkg_list,
        ]

    ok, output = _run_in_rootfs(rootfs, command(names), timeout=timeout)
    if ok:
        strip_suid(rootfs)
        return True, ""

    log("整批安装失败，改为逐个安装")
    installed: list[str] = []
    failed: list[str] = []
    for name in names:
        one_ok, one_output = _run_in_rootfs(
            rootfs, command([name]), timeout=min(timeout, 180.0)
        )
        if one_ok:
            installed.append(name)
        else:
            failed.append(f"{name}（{(one_output or '').strip()[:120]}）")

    if installed:
        log(f"已装 {len(installed)} 个，失败 {len(failed)} 个")
        return True, "部分失败：" + "；".join(failed)
    return False, "全部失败：" + "；".join(failed)


def ensure_resolv_conf(rootfs: Path) -> bool:
    """把宿主的 resolv.conf 拷进 rootfs，让 apt / 沙盒能解析域名。"""
    src = Path("/etc/resolv.conf")
    dst = rootfs / "etc" / "resolv.conf"
    if not src.is_file() or not (rootfs / "etc").is_dir():
        return False
    if dst.is_symlink():
        try:
            dst.unlink()
        except OSError:
            return False
    try:
        shutil.copyfile(src, dst)
        return True
    except OSError:
        return False
