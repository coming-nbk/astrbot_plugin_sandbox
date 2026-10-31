# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""路径安全。

两条边界，必须同时守住：
1. 宿主路径 —— 只允许导入白名单目录内的普通文件，且不匹配拒绝名单。
2. 沙盒路径 —— 任何相对路径解析后都必须还在该用户的沙盒目录里。

所有判断都用 realpath，符号链接指到外面一样会被拦下。
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable, Sequence


class PathRejected(ValueError):
    """路径被安全策略拒绝。消息可以直接回给用户。"""


def _clean(raw: str) -> str:
    text = str(raw or "").strip().strip("\x00")
    if not text:
        raise PathRejected("路径不能为空")
    if "\x00" in text:
        raise PathRejected("路径包含非法字符")
    return text


def _within(child: str, parent: str) -> bool:
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _denied(real_path: str, roots: Sequence[str], globs: Iterable[str]) -> str | None:
    name = os.path.basename(real_path)
    relatives = []
    for root in roots:
        if _within(real_path, root):
            relatives.append(os.path.relpath(real_path, root))
    candidates = [name, *relatives]
    for pattern in globs:
        pattern = pattern.strip()
        if not pattern:
            continue
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(
                candidate.replace(os.sep, "/"), pattern
            ):
                return pattern
    return None


def resolve_host_file(
    raw: str,
    *,
    roots: Sequence[str],
    deny_globs: Iterable[str] = (),
    allow_dirs: bool = False,
) -> Path:
    """把用户给的宿主路径解析成一个可导入的真实路径。

    拒绝：相对路径、不存在、白名单外、命中拒绝名单、非普通文件/目录。
    """
    text = _clean(raw)
    expanded = os.path.expanduser(os.path.expandvars(text))
    if not os.path.isabs(expanded):
        raise PathRejected("请给出绝对路径，例如 /opt/astrbot/data/config/xxx.json")

    real_roots = []
    for root in roots:
        if not root:
            continue
        real_roots.append(os.path.realpath(os.path.expanduser(str(root))))
    if not real_roots:
        raise PathRejected("管理员没有配置 import_roots，宿主导入功能不可用")

    real = os.path.realpath(expanded)
    if not any(_within(real, root) for root in real_roots):
        raise PathRejected(
            "该路径不在允许导入的目录内。允许的前缀："
            + "、".join(sorted(set(real_roots)))
        )

    hit = _denied(real, real_roots, deny_globs)
    if hit:
        raise PathRejected(f"该文件匹配拒绝规则「{hit}」，不允许导入")

    if not os.path.exists(real):
        raise PathRejected("路径不存在")
    if os.path.isdir(real):
        if not allow_dirs:
            raise PathRejected("不支持导入目录（可在配置里打开 import_allow_dirs）")
        return Path(real)
    if not os.path.isfile(real):
        raise PathRejected("只允许导入普通文件")
    return Path(real)


def resolve_sandbox_path(raw: str, root: Path | str) -> Path:
    """把沙盒内相对路径解析成宿主上的真实路径，越界即拒绝。"""
    text = _clean(raw)
    if os.path.isabs(text):
        raise PathRejected("沙盒内请使用相对路径，例如 tmp/a.py")
    if text in (".", "./"):
        return Path(os.path.realpath(root))

    root_real = os.path.realpath(str(root))
    joined = os.path.join(root_real, text)
    real = os.path.realpath(joined)
    if not _within(real, root_real):
        raise PathRejected("路径越界：不允许访问沙盒目录之外的内容")
    return Path(real)


def sandbox_relative(path: Path | str, root: Path | str) -> str:
    """把宿主路径转成沙盒内的相对路径（用于回显）。"""
    root_real = os.path.realpath(str(root))
    real = os.path.realpath(str(path))
    if not _within(real, root_real):
        raise PathRejected("路径越界")
    rel = os.path.relpath(real, root_real)
    return "." if rel == "." else rel.replace(os.sep, "/")


def human_bytes(size: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(0, int(size)))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            # 整数不显示小数点：256MB 而不是 256.0MB
            if abs(value - round(value)) < 0.05:
                return f"{int(round(value))}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"
