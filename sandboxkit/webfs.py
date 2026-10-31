# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebUI 用的文件视图：目录树、预览、保存、删除。

预览要区分文本和二进制：文本直接给字符串，图片给 data URL，其余只给元信息。
所有路径都走 Session.resolve，越界一律拒绝。
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

from .paths import human_bytes
from .session import SandboxError, Session, tree_size

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".html", ".htm",
    ".css", ".xml", ".csv", ".tsv", ".log", ".sql", ".c", ".h", ".cpp", ".hpp",
    ".rs", ".go", ".java", ".kt", ".php", ".rb", ".lua", ".env", ".gitignore",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


def _entry(session: Session, path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"name": path.name, "kind": "unknown", "size": 0, "mtime": 0}
    if path.is_dir():
        kind = "dir"
    elif path.suffix.lower() in IMAGE_SUFFIXES:
        kind = "image"
    elif path.suffix.lower() in TEXT_SUFFIXES:
        kind = "text"
    else:
        kind = "file"
    return {
        "name": path.name,
        "path": session.relative(path),
        "kind": kind,
        "size": stat.st_size,
        "size_text": human_bytes(stat.st_size),
        "mtime": int(stat.st_mtime),
    }


def list_tree(
    session: Session,
    raw_path: str = ".",
    *,
    depth: int = 2,
    max_entries: int = 400,
) -> dict:
    """返回一层目录 + 可选深度的子树。深度有上限，避免一次扫爆。"""
    target = session.resolve(raw_path or ".")
    if not target.is_dir():
        raise SandboxError("不是目录")

    depth = max(0, min(int(depth), 4))
    max_entries = max(10, min(int(max_entries), 2000))
    counter = {"n": 0}

    def walk(path: Path, level: int) -> list[dict]:
        rows: list[dict] = []
        try:
            children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            raise SandboxError(f"读取目录失败：{exc}") from exc
        for child in children:
            if counter["n"] >= max_entries:
                break
            counter["n"] += 1
            entry = _entry(session, child)
            if entry["kind"] == "dir" and level + 1 < depth:
                try:
                    entry["children"] = walk(child, level + 1)
                except SandboxError:
                    entry["children"] = []
            rows.append(entry)
        return rows

    return {
        "path": session.relative(target),
        "entries": walk(target, 0),
        "truncated": counter["n"] >= max_entries,
        "count": counter["n"],
    }


def preview(session: Session, raw_path: str, *, max_bytes: int = 256 * 1024) -> dict:
    target = session.resolve(raw_path)
    if not target.exists():
        raise SandboxError("文件不存在")
    if target.is_dir():
        info = _entry(session, target)
        info["kind"] = "dir"
        info["entries"] = list_tree(session, raw_path, depth=1)["entries"]
        return info

    info = _entry(session, target)
    suffix = target.suffix.lower()
    size = target.stat().st_size
    info["size_text"] = human_bytes(size)

    if suffix in IMAGE_SUFFIXES:
        if size > max_bytes:
            info["kind"] = "binary"
            info["message"] = f"图片 {human_bytes(size)} 超过预览上限"
            return info
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        info["kind"] = "image"
        info["data_url"] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        return info

    with open(target, "rb") as handle:
        chunk = handle.read(max_bytes + 1)
    truncated = len(chunk) > max_bytes or size > max_bytes
    chunk = chunk[:max_bytes]
    if b"\x00" in chunk[:4096]:
        info["kind"] = "binary"
        info["message"] = "二进制文件，不提供预览"
        # 给前端一段十六进制头，至少能看出这是什么
        info["head_b64"] = base64.b64encode(chunk[:256]).decode("ascii")
        info["head_bytes"] = min(len(chunk), 256)
        return info
    info["kind"] = "text"
    info["content"] = chunk.decode("utf-8", "replace")
    info["truncated"] = truncated
    return info


def save_text(
    session: Session,
    raw_path: str,
    content: str,
    *,
    max_bytes: int,
    quota: int,
) -> dict:
    target = session.resolve(raw_path)
    if target == Path(session.dir).resolve():
        raise SandboxError("不能写沙盒根目录")
    if target.is_dir():
        raise SandboxError("目标是目录")
    payload = content.encode("utf-8")
    if max_bytes > 0 and len(payload) > max_bytes:
        raise SandboxError(f"内容超过上限 {human_bytes(max_bytes)}")

    existing = target.stat().st_size if target.exists() else 0
    session.ensure_quota(quota, max(0, len(payload) - existing))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(content)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return {"path": session.relative(target), "size": len(payload)}


def delete_path(session: Session, raw_path: str) -> str:
    target = session.resolve(raw_path)
    if target == Path(session.dir).resolve():
        raise SandboxError("不能删除沙盒根目录")
    if not target.exists():
        raise SandboxError("目标不存在")
    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    return session.relative(target)


def session_summary(session: Session) -> dict:
    usage = session.usage()
    return {
        "key": session.key,
        "files": usage.files,
        "bytes": usage.bytes,
        "bytes_text": human_bytes(usage.bytes),
    }


def total_size(path: Path) -> int:
    return tree_size(path)
