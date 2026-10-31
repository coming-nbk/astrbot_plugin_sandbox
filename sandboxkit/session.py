# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""会话目录：一个用户一个沙盒。

沙盒内的路径一律是相对路径，落盘时解析到 <sessions>/<key>/ 下面。
导入宿主文件 = 复制进 imports/，导出 = 从沙盒里取文件发回聊天。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .paths import PathRejected, human_bytes, resolve_sandbox_path, sandbox_relative

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]+")


class SandboxError(RuntimeError):
    """可以直接回给用户的错误。"""


@dataclass(frozen=True)
class Usage:
    files: int
    bytes: int

    def to_text(self, quota: int) -> str:
        percent = (self.bytes / quota * 100) if quota > 0 else 0
        return (
            f"{self.files} 个文件，{human_bytes(self.bytes)} / "
            f"{human_bytes(quota)}（{percent:.1f}%）"
        )


def tree_size(path: Path) -> int:
    """文件或目录的总字节数（不跟随符号链接）。"""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            try:
                stat = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.st_mode & 0o170000 == 0o100000:
                total += stat.st_size
    return total


def tree_stats(path: Path) -> tuple[int, int]:
    """(文件数, 总字节)。不跟随符号链接。"""
    files = 0
    total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            try:
                stat = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            files += 1
            if stat.st_mode & 0o170000 == 0o100000:
                total += stat.st_size
    return files, total


def _copy_capped(source: Path, target: Path, limit: int) -> int:
    """边拷边数：源文件在拷贝过程中变大也不会突破上限。"""
    written = 0
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if limit > 0 and written > limit:
                dst.close()
                try:
                    target.unlink()
                except OSError:
                    pass
                raise SandboxError(
                    f"文件超过单次导入上限 {human_bytes(limit)}"
                )
            dst.write(chunk)
    return written


def _ignore_factory(patterns: Sequence[str]):
    def _ignore(_directory: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns if pattern)
        ]

    return _ignore


def sanitize_key(raw: str) -> str:
    """把任意用户标识变成安全目录名，同时保留可读性。"""
    text = str(raw or "anonymous")
    slug = _SAFE_KEY.sub("_", text).strip("._")[:32] or "user"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


class Session:
    def __init__(self, root: Path | str, key: str):
        self.root = Path(root)
        self.raw_key = str(key)
        self.key = sanitize_key(key)
        self.dir = self.root / self.key
        self._ensured = False
        self._usage_cache: tuple[float, Usage] | None = None

    # ---------- 生命周期 ----------

    @classmethod
    def from_key(cls, root: Path | str, key: str) -> "Session":
        """用已经算好的目录名构造（避免二次 hash）。"""
        session = cls(root, key)
        session.key = str(key)
        session.dir = Path(root) / session.key
        return session

    def ensure(self) -> Path:
        """建目录并把权限收到 0700：沙盒内容只给属主和插件进程看。

        每个实例只做一次，之后直接返回 —— 路径解析会频繁调用它。
        """
        if self._ensured and self.dir.is_dir():
            return self.dir
        for name in ("", "imports", "tmp", "out"):
            path = self.dir / name if name else self.dir
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(path, 0o700)
            except OSError:
                continue
        self._ensured = True
        return self.dir

    def reset(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self._ensured = False
        self.invalidate()
        self.ensure()

    # ---------- 路径 ----------

    def resolve(self, raw: str) -> Path:
        self.ensure()
        try:
            return resolve_sandbox_path(raw, self.dir)
        except PathRejected as exc:
            raise SandboxError(str(exc)) from exc

    def relative(self, path: Path) -> str:
        return sandbox_relative(path, self.dir)

    # ---------- 读写 ----------

    def invalidate(self) -> None:
        self._usage_cache = None

    def usage_cached(self, ttl: float = 2.0) -> Usage:
        """目录大的时候，两次执行各扫一遍文件树很浪费，短缓存一下。"""
        now = time.monotonic()
        if self._usage_cache is not None and now - self._usage_cache[0] <= ttl:
            return self._usage_cache[1]
        usage = self.usage()
        self._usage_cache = (now, usage)
        return usage

    def usage(self) -> Usage:
        files = 0
        total = 0
        if not self.dir.exists():
            return Usage(0, 0)
        for current, _dirs, names in os.walk(self.dir):
            for name in names:
                path = os.path.join(current, name)
                try:
                    stat = os.lstat(path)
                except OSError:
                    continue
                files += 1
                if stat.st_mode & 0o170000 == 0o100000:  # 只统计普通文件
                    total += stat.st_size
        return Usage(files, total)

    def list_dir(self, raw: str = ".") -> list[tuple[str, int, bool]]:
        target = self.resolve(raw)
        if not target.exists():
            raise SandboxError("目录不存在")
        if not target.is_dir():
            raise SandboxError("这不是目录")
        rows = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError as exc:
            raise SandboxError(f"读取目录失败：{exc}") from exc
        for entry in entries:
            try:
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                size = 0
            rows.append((entry.name, size, entry.is_dir()))
        return rows

    def read_text(self, raw: str, max_bytes: int) -> str:
        target = self.resolve(raw)
        if not target.is_file():
            raise SandboxError("文件不存在或不是普通文件")
        size = target.stat().st_size
        with open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
        text = data[:max_bytes].decode("utf-8", "replace")
        if size > max_bytes:
            text += f"\n…（文件 {human_bytes(size)}，只显示前 {human_bytes(max_bytes)}）"
        return text

    def remove(self, raw: str) -> str:
        target = self.resolve(raw)
        if target == Path(os.path.realpath(self.dir)):
            raise SandboxError("不能删除沙盒根目录，请用 reset")
        if not target.exists():
            raise SandboxError("目标不存在")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return self.relative(target)

    # ---------- 导入导出 ----------

    def ensure_quota(self, quota: int, incoming: int = 0) -> Usage:
        usage = self.usage()
        if quota > 0 and usage.bytes + incoming > quota:
            raise SandboxError(
                f"超出配额：已用 {human_bytes(usage.bytes)}，"
                f"本次 {human_bytes(incoming)}，上限 {human_bytes(quota)}"
            )
        return usage

    def import_host_file(
        self,
        host_path: Path,
        *,
        name: str = "",
        max_bytes: int,
        quota: int,
        overwrite: bool = False,
        deny_globs: Sequence[str] = (),
        max_files: int = 2000,
    ) -> Path:
        """复制宿主文件（或目录）进 imports/。返回落盘位置。"""
        source = Path(host_path)
        incoming = tree_size(source)
        if max_bytes > 0 and incoming > max_bytes:
            raise SandboxError(
                f"内容 {human_bytes(incoming)} 超过单次导入上限 {human_bytes(max_bytes)}"
            )
        self.ensure_quota(quota, incoming)

        safe_name = _SAFE_KEY.sub("_", Path(name or source.name).name).strip("._")
        safe_name = safe_name or "imported.bin"
        target = self.dir / "imports" / safe_name
        if target.exists() and not overwrite:
            suffix = target.suffix if target.is_file() else ""
            stem = target.stem if target.is_file() else target.name
            target = self.dir / "imports" / f"{stem}-{int(time.time())}{suffix}"

        if source.is_dir():
            files, _size = tree_stats(source)
            if max_files > 0 and files > max_files:
                raise SandboxError(
                    f"目录里有 {files} 个文件，超过单次导入上限 {max_files}"
                )
            shutil.copytree(
                source,
                target,
                symlinks=True,
                ignore=_ignore_factory(deny_globs),
            )
        else:
            _copy_capped(source, target, max_bytes)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def prepare_script(self, code: str, suffix: str = ".py") -> str:
        """把代码写进 tmp/，返回沙盒内相对路径。"""
        self.ensure()
        name = f"tmp/script-{int(time.time() * 1000)}{suffix}"
        path = self.dir / name
        path.write_text(code, "utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return name
