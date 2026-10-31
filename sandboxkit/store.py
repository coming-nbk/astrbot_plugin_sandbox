# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""三张表：单用户权限、命令信任、场景规则。

都用同一个 JSON 存储骨架：读失败不炸（坏文件改名 .broken），写走临时文件 + os.replace。
全局开关和默认值仍交给 AstrBot 的配置表单，这里只存表单表达不了的结构化规则。
"""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, TypeVar

from .config import Settings, _as_bool, _as_int, _as_str, _as_str_tuple

MAX_QUOTA = 64 * 1024 * 1024 * 1024
MAX_TIMEOUT = 3600


class StoreError(RuntimeError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        handle = os.fdopen(fd, "w", encoding="utf-8")
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_name, path)
    except OSError as exc:
        if handle is not None:
            handle.close()
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StoreError(f"写入 {path.name} 失败：{exc}") from exc


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        broken = path.with_suffix(path.suffix + ".broken")
        try:
            os.replace(path, broken)
        except OSError:
            pass
        return None


# ---------- 数据结构 ----------


@dataclass
class UserPerm:
    user_id: str
    enabled: bool = True
    note: str = ""
    quota_bytes: int = 0  # 0 = 跟随全局
    timeout_seconds: int = 0  # 0 = 跟随全局
    allow_network: bool | None = None
    allow_import: bool | None = None
    allow_exec: bool = True
    allow_py: bool = True
    allow_export: bool = True
    allow_reset: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrustRule:
    """被信任的命令：跳过看门狗，可选放宽超时。"""

    pattern: str
    note: str = ""
    bypass_memory: bool = True
    bypass_cpu: bool = True
    timeout_seconds: int = 0  # 0 = 跟随全局
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScopeRule:
    """按场景放行/拒绝：scope=all|private|group，target 为群号或 *。"""

    scope: str = "group"
    target: str = "*"
    allow: bool = True
    users: tuple[str, ...] = ()
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.target}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["users"] = list(self.users)
        data["key"] = self.key
        return data


def normalize_perm(payload: Mapping[str, Any]) -> UserPerm:
    user_id = _as_str(payload.get("user_id"), "")
    if not user_id:
        raise StoreError("user_id 不能为空")
    network = payload.get("allow_network")
    imports = payload.get("allow_import")
    return UserPerm(
        user_id=user_id,
        enabled=_as_bool(payload.get("enabled"), True),
        note=_as_str(payload.get("note"), "")[:80],
        quota_bytes=_as_int(payload.get("quota_bytes"), 0, minimum=0, maximum=MAX_QUOTA),
        timeout_seconds=_as_int(
            payload.get("timeout_seconds"), 0, minimum=0, maximum=MAX_TIMEOUT
        ),
        allow_network=None if network is None or network == "" else _as_bool(network, True),
        allow_import=None if imports is None or imports == "" else _as_bool(imports, True),
        allow_exec=_as_bool(payload.get("allow_exec"), True),
        allow_py=_as_bool(payload.get("allow_py"), True),
        allow_export=_as_bool(payload.get("allow_export"), True),
        allow_reset=_as_bool(payload.get("allow_reset"), True),
    )


def normalize_trust(payload: Mapping[str, Any]) -> TrustRule:
    pattern = _as_str(payload.get("pattern"), "")
    if not pattern:
        raise StoreError("pattern 不能为空")
    if len(pattern) > 200:
        raise StoreError("pattern 太长")
    return TrustRule(
        pattern=pattern,
        note=_as_str(payload.get("note"), "")[:80],
        bypass_memory=_as_bool(payload.get("bypass_memory"), True),
        bypass_cpu=_as_bool(payload.get("bypass_cpu"), True),
        timeout_seconds=_as_int(
            payload.get("timeout_seconds"), 0, minimum=0, maximum=MAX_TIMEOUT
        ),
        enabled=_as_bool(payload.get("enabled"), True),
    )


def normalize_scope(payload: Mapping[str, Any]) -> ScopeRule:
    scope = _as_str(payload.get("scope"), "group").lower()
    if scope not in ("all", "private", "group"):
        raise StoreError("scope 只能是 all / private / group")
    raw_target = payload.get("target")
    target = "*" if raw_target is None else str(raw_target).strip()
    if scope == "group" and not target:
        raise StoreError("group 规则必须填群号或 *")
    return ScopeRule(
        scope=scope,
        target=target,
        allow=_as_bool(payload.get("allow"), True),
        users=_as_str_tuple(payload.get("users")),
        note=_as_str(payload.get("note"), "")[:80],
    )


# ---------- 通用存储 ----------


T = TypeVar("T")


class _ListStore(Generic[T]):
    section = "items"

    def __init__(self, path: Path | str, normalizer: Callable[[Mapping], T]):
        self.path = Path(path)
        self._normalize = normalizer
        self._lock = threading.Lock()
        self._items: dict[str, T] = {}
        self.load()

    def _key(self, item: T) -> str:
        raise NotImplementedError  # quickcheck:ignore - 抽象基类

    def load(self) -> None:
        data = _read_json(self.path)
        rows: list[Any] = []
        if isinstance(data, dict):
            rows = data.get(self.section) or []
        elif isinstance(data, list):
            rows = data
        items: dict[str, T] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = self._normalize(row)
            except StoreError:
                continue
            items[self._key(item)] = item
        with self._lock:
            self._items = items

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                self.section: [item.to_dict() for item in self._items.values()],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        _atomic_write(self.path, text)

    def all(self) -> list[T]:
        with self._lock:
            return list(self._items.values())

    def get(self, key: str) -> T | None:
        with self._lock:
            return self._items.get(str(key))

    def upsert(self, payload: Mapping[str, Any]) -> T:
        item = self._normalize(payload)
        with self._lock:
            self._items[self._key(item)] = item
        self.save()
        return item

    def delete(self, key: str) -> bool:
        with self._lock:
            removed = self._items.pop(str(key), None) is not None
        if removed:
            self.save()
        return removed


class PermissionStore(_ListStore[UserPerm]):
    section = "users"

    def __init__(self, path: Path | str):
        super().__init__(path, normalize_perm)

    def _key(self, item: UserPerm) -> str:
        return item.user_id

    def all(self) -> list[UserPerm]:
        return sorted(super().all(), key=lambda perm: perm.user_id)


class TrustStore(_ListStore[TrustRule]):
    section = "rules"

    def __init__(self, path: Path | str):
        super().__init__(path, normalize_trust)

    def _key(self, item: TrustRule) -> str:
        return item.pattern


class ScopeStore(_ListStore[ScopeRule]):
    section = "rules"

    def __init__(self, path: Path | str):
        super().__init__(path, normalize_scope)

    def _key(self, item: ScopeRule) -> str:
        return item.key


# ---------- 匹配 ----------


def match_trust(rules: list[TrustRule], command: str) -> TrustRule | None:
    """按顺序找第一条命中的信任规则。命令为空时不匹配。"""
    text = (command or "").strip()
    if not text:
        return None
    for rule in rules:
        if not rule.enabled:
            continue
        if fnmatch.fnmatchcase(text, rule.pattern) or fnmatch.fnmatchcase(
            text.splitlines()[0] if text else "", rule.pattern
        ):
            return rule
    return None


def match_scope(
    rules: list[ScopeRule],
    group_id: str | None,
    is_private: bool,
) -> ScopeRule | None:
    """具体优先：指定群号 > group:* > private > all。"""
    gid = str(group_id or "").strip()
    buckets: dict[str, list[ScopeRule]] = {"exact": [], "group": [], "private": [], "all": []}
    for rule in rules:
        if rule.scope == "group" and gid and rule.target == gid:
            buckets["exact"].append(rule)
        elif rule.scope == "group" and rule.target == "*":
            buckets["group"].append(rule)
        elif rule.scope == "private" and is_private:
            buckets["private"].append(rule)
        elif rule.scope == "all":
            buckets["all"].append(rule)
    for name in ("exact", "group", "private", "all"):
        if buckets[name]:
            return buckets[name][0]
    return None


def apply_user_perm(settings: Settings, perm: UserPerm | None) -> Settings:
    """把单用户权限叠到全局设置上。0 / None 表示跟随全局。"""
    if perm is None:
        return settings
    changes: dict[str, Any] = {}
    if perm.quota_bytes > 0:
        changes["quota_bytes"] = min(perm.quota_bytes, MAX_QUOTA)
    if perm.timeout_seconds > 0:
        changes["timeout_seconds"] = min(perm.timeout_seconds, MAX_TIMEOUT)
    if perm.allow_network is not None:
        changes["allow_network"] = perm.allow_network
    if perm.allow_import is not None:
        changes["allow_import"] = perm.allow_import
    return replace(settings, **changes) if changes else settings
