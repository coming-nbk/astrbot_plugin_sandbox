# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""本地 CVE 知识库：AI 搜到什么、判断什么，都可以记进来，后面按内核版本自动匹配。

设计上刻意做得很小：
- 一条记录 = id / title / severity / affects(正则) / detail / fix
- 存 JSON，读写都容错（文件坏了当空库，不影响体检）
- 只做「匹配」，不做「抓取」——抓取交给外部搜索插件或 feed
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

SEVERITIES = ("high", "medium", "low", "info")


class CveError(Exception):
    pass


_PKG_SPEC = re.compile(r"^([A-Za-z0-9+._-]+)\s*(<=|>=|==|=|<|>)\s*(\S+)$")


def _pkg_matches(spec: str, packages: dict[str, str]) -> bool:
    """pkg:openssl<3.0.13 这种条件，拿实际安装版本判断。"""
    from .pkginfo import satisfies

    match = _PKG_SPEC.match(spec.strip())
    if not match:
        return False
    name, op, version = match.group(1), match.group(2), match.group(3)
    installed = packages.get(name)
    if not installed:
        return False
    return satisfies(installed, op + version)


class CveStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ---------- 读写 ----------

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("items", [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ---------- 对外 ----------

    def all(self) -> list[dict]:
        return self._read()

    def upsert(
        self,
        *,
        cve_id: str,
        title: str = "",
        severity: str = "medium",
        affects: str = "",
        detail: str = "",
        fix: str = "",
        added_by: str = "",
    ) -> dict:
        cve_id = (cve_id or "").strip()
        if not cve_id or len(cve_id) > 64:
            raise CveError("CVE 编号不能为空，也不能超过 64 字符")
        severity = (severity or "medium").lower()
        if severity not in SEVERITIES:
            raise CveError(f"severity 只能是 {'/'.join(SEVERITIES)}")
        if affects:
            try:
                re.compile(affects)
            except re.error as exc:
                raise CveError(f"affects 不是合法正则：{exc}") from exc

        item = {
            "id": cve_id,
            "title": (title or cve_id)[:200],
            "severity": severity,
            "affects": affects[:200],
            "detail": detail[:500],
            "fix": fix[:300],
            "added_by": str(added_by)[:64],
            "at": int(time.time()),
        }

        items = self._read()
        for index, existing in enumerate(items):
            if existing.get("id") == cve_id:
                items[index] = item
                break
        else:
            items.append(item)
        self._write(items)
        return item

    def remove(self, cve_id: str) -> bool:
        items = self._read()
        kept = [item for item in items if item.get("id") != cve_id]
        if len(kept) == len(items):
            return False
        self._write(kept)
        return True

    def matching(
        self,
        *,
        kernel: str,
        backend: str,
        packages: dict[str, str] | None = None,
    ) -> list[dict]:
        r"""挑出和本机相关的条目。

        affects 支持两种写法：
        - 正则：匹配「内核版本 + 后端名」，例如 ^5\.15
        - 包版本：pkg:openssl<3.0.13 —— 拿 rootfs 里实际装的版本比，
          升级完之后条件不成立，条目自动消失
        留空视为「通用」，总是命中。
        """
        from .pkginfo import satisfies

        haystack = f"{kernel} {backend}"
        packages = packages or {}
        out: list[dict] = []
        for item in self._read():
            pattern = (item.get("affects") or "").strip()
            if pattern.startswith("pkg:"):
                if not _pkg_matches(pattern[4:], packages):
                    continue
            elif pattern:
                try:
                    if not re.search(pattern, haystack):
                        continue
                except re.error:
                    continue
            out.append(item)
        return out

    def render(self, items: list[dict] | None = None) -> str:
        items = self.all() if items is None else items
        if not items:
            return "知识库里还没有条目。"
        lines = [f"共 {len(items)} 条："]
        for item in items:
            lines.append(
                f"[{item.get('severity', '?')}] {item['id']}  {item.get('title', '')}"
            )
            if item.get("affects"):
                lines.append(f"    命中条件：{item['affects']}")
            if item.get("fix"):
                lines.append(f"    修法：{item['fix']}")
        return "\n".join(lines)
