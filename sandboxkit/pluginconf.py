# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""直接读写 AstrBot 的插件配置文件。

页面里改设置，最终要落到 AstrBot 自己那份配置上（`data/config/<插件名>_config.json`），
否则重载插件就丢了。这里只做三件事：找文件、安全合并、原子写。

- 只允许改白名单里的键，避免把不认识的字段写进去把 AstrBot 弄懵
- 写之前留一份 `.bak`，写坏了还能手工恢复
- 写入走临时文件 + os.replace
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # bool / int / text / select / list
    default: Any = None
    options: tuple[str, ...] = ()
    hint: str = ""
    restart: bool = False
    scale: int = 1  # 界面单位 -> 存储单位（MB -> 字节用 1024*1024）
    group: str = "基础"  # 设置页分组

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "options": list(self.options),
            "hint": self.hint,
            "restart": self.restart,
            "scale": self.scale,
            "group": self.group,
        }


MB = 1024 * 1024

# 设置页分组：同组的字段排在一起，页面按这个顺序渲染
GROUP_ORDER = (
    "基础",
    "隔离与后端",
    "资源限制",
    "权限",
    "安全巡检",
    "AI 与通知",
    "高级",
)

_GROUP_OF = {
    "enabled": "基础",
    "language": "基础",
    "backend": "隔离与后端",
    "allow_local_backend": "隔离与后端",
    "rootfs_profile": "隔离与后端",
    "rootfs_preset": "隔离与后端",
    "rootfs_version": "隔离与后端",
    "rootfs_url": "隔离与后端",
    "rootfs_image": "隔离与后端",
    "rootfs_sha256": "隔离与后端",
    "auto_bootstrap": "隔离与后端",
    "bootstrap_packages": "隔离与后端",
    "chroot_uid": "隔离与后端",
    "chroot_gid": "隔离与后端",
    "allow_root_sandbox": "隔离与后端",
    "quota_bytes": "资源限制",
    "timeout_seconds": "资源限制",
    "memory_mb": "资源限制",
    "cpu_seconds": "资源限制",
    "max_procs": "资源限制",
    "max_cores": "资源限制",
    "max_output_bytes": "资源限制",
    "max_runs_per_minute": "资源限制",
    "max_concurrent_runs": "资源限制",
    "max_command_chars": "资源限制",
    "allow_all": "权限",
    "allowed_platforms": "权限",
    "allowed_users": "权限",
    "admin_users": "权限",
    "allow_exec": "权限",
    "allow_network": "权限",
    "allow_import": "权限",
    "import_roots": "权限",
    "export_as_file": "权限",
    "import_max_bytes": "权限",
    "deny_patterns": "权限",
    "web_enabled": "安全巡检",
    "web_auth": "安全巡检",
    "web_token": "安全巡检",
    "security_feed_url": "安全巡检",
    "security_feed_format": "安全巡检",
    "security_feed_ttl": "安全巡检",
    "security_notify": "AI 与通知",
    "security_scan_hours": "AI 与通知",
    "ai_summary_enabled": "AI 与通知",
    "ai_summary_source": "AI 与通知",
    "ai_summary_url": "AI 与通知",
    "ai_summary_key": "AI 与通知",
    "ai_summary_model": "AI 与通知",
    "sandbox_shell": "高级",
    "python_bin": "高级",
    "reply_max_chars": "高级",
    "audit_enabled": "高级",
    "audit_max_bytes": "高级",
    "log_enabled": "高级",
    "log_retention_days": "高级",
    "worker_isolation": "高级",
    "worker_fallback": "高级",
    "trust_rules": "高级",
    "security_search_path": "安全巡检",
    "security_search_engine": "安全巡检",
}

FIELDS: tuple[Field, ...] = (
    Field("enabled", "启用沙盒", "bool", True, hint="关掉后所有人都用不了"),
    Field(
        "language",
        "界面与消息语言",
        "text",
        "zh",
        hint="zh / en。影响机器人回复、安全报告和页面文案",
    ),
    Field(
        "backend",
        "隔离后端",
        "select",
        "auto",
        ("auto", "bwrap", "chroot", "proot", "docker", "local"),
        hint="auto 会依次尝试；chroot 需要 root",
        restart=True,
    ),
    Field(
        "chroot_uid",
        "沙盒内用户（0 = root）",
        "int",
        65534,
        hint="65534 = nobody（安全，apt 用不了）；0 = root（apt 直接可用，风险更高）",
        restart=True,
    ),
    Field("quota_bytes", "每用户配额（MB）", "int", 256, scale=MB),
    Field("timeout_seconds", "命令超时（秒）", "int", 30),
    Field("memory_mb", "内存上限（MB）", "int", 512),
    Field("max_runs_per_minute", "每分钟次数", "int", 20, hint="0 = 不限"),
    Field("allow_network", "允许联网", "bool", False),
    Field("allow_import", "允许导入宿主文件", "bool", True),
    Field(
        "allow_local_backend",
        "允许 local 后端执行",
        "bool",
        False,
        hint="没有隔离后端时，开着会把宿主 shell 交出去",
    ),
    Field("worker_isolation", "执行放进独立进程", "bool", True, restart=True),
    Field(
        "rootfs_profile",
        "rootfs 套餐",
        "select",
        "standard",
        ("minimal", "standard", "full"),
        hint="只影响下次下载 rootfs",
    ),
    Field(
        "web_auth",
        "页面鉴权",
        "select",
        "dashboard",
        ("dashboard", "token", "none"),
        hint="dashboard = 跟随控制台登录态",
    ),
    Field("admin_users", "管理员 ID", "list", (), hint="逗号分隔，能用 install 等特权操作"),
    Field("allowed_users", "允许使用的用户", "list", ()),
    Field(
        "export_as_file",
        "导出时直接发文件",
        "bool",
        True,
        hint="关掉后 /sbx export 只回宿主路径。平台发不了任意文件（例如部分官方机器人接口）时用这个",
    ),
    Field("import_roots", "允许导入的宿主目录", "list", ()),
    Field(
        "security_feed_url",
        "外部漏洞源地址",
        "text",
        "",
        hint="可选。填一个返回 JSON 数组的地址，/sbx audit 会按内核版本匹配；留空只用本地检查",
    ),
    Field(
        "security_search_path",
        "搜索插件路径",
        "text",
        "",
        hint="可选。指向 astrbot_plugin_search_engines 的目录，/sbx cve search 会借它抓链；留空自动在同级目录找",
    ),
    Field(
        "security_search_engine",
        "搜索用哪个引擎",
        "text",
        "bing",
        hint="bing / baidu / google，取决于搜索插件支持哪些",
    ),
    Field(
        "ai_summary_enabled",
        "用 AI 翻译并总结漏洞",
        "bool",
        False,
        hint="打开后，安全页每条漏洞可以点「翻译并总结」，用下面的接口把英文原文转成中文",
    ),
    Field(
        "ai_summary_source",
        "用哪个模型",
        "text",
        "current",
        hint="current=直接用 AstrBot 里当前选中的对话模型（推荐）；custom=填下面的接口地址和密钥",
    ),
    Field("ai_summary_url", "AI 接口地址", "text", "", hint="只有 custom 模式才用，OpenAI 兼容，例如 https://api.openai.com/v1"),
    Field("ai_summary_key", "AI 接口密钥", "text", "", hint="只存在本地配置文件里"),
    Field("ai_summary_model", "AI 模型名", "text", "gpt-4o-mini"),
    Field(
        "security_notify",
        "定时巡检并私聊管理员",
        "bool",
        True,
        hint="发现新的高风险条目时，给最后一次调用命令的管理员发私聊",
    ),
    Field(
        "security_scan_hours",
        "巡检间隔（小时）",
        "int",
        12,
        hint="最小 1 小时。只在发现新的高风险时才发消息",
    ),
    Field(
        "security_feed_format",
        "漏洞源格式",
        "text",
        "custom",
        hint="custom=自己写的 JSON 数组；nvd=NVD 2.0 接口；kev=CISA 已知被利用目录",
    ),
    Field(
        "security_feed_ttl",
        "漏洞源缓存（秒）",
        "int",
        86400,
        hint="默认一天拉一次，别设太小",
    ),
    Field(
        "max_command_chars",
        "单条命令长度上限",
        "int",
        8192,
        hint="超过就直接拒绝，防止被诱导堆一条几十 KB 的命令",
    ),
    Field(
        "deny_patterns",
        "禁止出现的命令片段",
        "list",
        (),
        hint="一行一条，按子串匹配（不区分大小写）。命中就拒绝执行，例如 rm -rf /",
    ),
    Field(
        "allow_root_sandbox",
        "允许沙盒内以 root 运行",
        "bool",
        False,
        hint="关着的时候，chroot_uid=0 会被直接拒绝执行 —— root 一旦逃逸就是整台机器",
    ),
)

# 按 key 打上分组，页面照着 GROUP_ORDER 渲染
FIELDS = tuple(
    replace(field, group=_GROUP_OF.get(field.key, "其它")) for field in FIELDS
)

BY_KEY = {field.key: field for field in FIELDS}


def config_candidates(data_dir: Path, plugin_name: str) -> list[Path]:
    base = Path(data_dir) / "config"
    return [
        base / f"{plugin_name}_config.json",
        base / f"{plugin_name}.json",
    ]


def find_config(data_dir: Path, plugin_name: str) -> Path:
    for candidate in config_candidates(data_dir, plugin_name):
        if candidate.is_file():
            return candidate
    # 还没有就返回首选路径，写的时候会创建
    return config_candidates(data_dir, plugin_name)[0]


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"配置文件读不出来：{exc}") from exc
    return data if isinstance(data, dict) else {}


def _coerce(field: Field, value: Any) -> Any:
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    if field.kind == "int":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field.label} 必须是数字") from exc
        if number < 0:
            raise ConfigError(f"{field.label} 不能是负数")
        return number * field.scale
    if field.kind == "select":
        text = str(value).strip()
        if field.options and text not in field.options:
            raise ConfigError(f"{field.label} 只能是 {'/'.join(field.options)}")
        return text
    if field.kind == "list":
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            items = [part.strip() for part in str(value).replace("，", ",").split(",")]
        return [item for item in items if item]
    return str(value).strip()


def validate(updates: Mapping[str, Any]) -> dict:
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        field = BY_KEY.get(key)
        if field is None:
            continue  # 白名单外的键直接忽略，不报错
        clean[key] = _coerce(field, value)
    if not clean:
        raise ConfigError("没有可保存的字段")
    return clean


def save_config(path: Path, updates: Mapping[str, Any], *, backup: bool = True) -> dict:
    """合并写入。返回写入后的完整配置。"""
    clean = validate(updates)
    current = read_config(path)
    merged = {**current, **clean}

    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.is_file():
        try:
            shutil.copyfile(
                path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
            )
        except OSError:
            pass

    payload = json.dumps(merged, ensure_ascii=False, indent=2)
    handle = None
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
        handle = os.fdopen(fd, "w", encoding="utf-8")
        handle.write(payload)
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
        raise ConfigError(f"写入配置失败：{exc}") from exc
    return merged


def sync_into_host(path: Path, host, *, keys: tuple[str, ...] | None = None) -> list[str]:
    """把配置文件里的值对齐到宿主（AstrBot）的配置对象。

    以文件为准：文件里有的键，只要和宿主内存里的值不同，就写回宿主并让它落盘。
    返回真正改动过的键名，便于日志。
    """
    if host is None or not path.is_file():
        return []
    try:
        raw = read_config(path)
    except ConfigError:
        return []
    if not raw:
        return []

    allowed = set(keys) if keys else None
    changed: list[str] = []
    for key, value in raw.items():
        if allowed is not None and key not in allowed:
            continue
        try:
            current = host.get(key) if hasattr(host, "get") else None
        except Exception:  # noqa: BLE001 - 宿主对象行为不确定
            current = None
        if _same_value(current, value):
            continue
        try:
            host[key] = value
            changed.append(key)
        except Exception:  # noqa: BLE001 - 单个键失败不影响其它
            continue

    if not changed:
        return []

    save = getattr(host, "save_config", None)
    if callable(save):
        try:
            save()
        except Exception:  # noqa: BLE001
            pass
    return changed


def _same_value(left, right) -> bool:
    """宽松比较：JSON 里 1 和 True 不等价，但 "1" 和 1 视为不同。"""
    if left is None and right is None:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return list(left) == list(right)
    return left == right


def snapshot(path: Path, settings, warnings: list | None = None) -> list[dict]:
    """界面用的字段列表：带当前值（配置里没写就用默认）。

    配置文件坏了不能让整个页面 500：回退到当前生效值，并把原因塞进 warnings。
    """
    try:
        raw = read_config(path)
    except ConfigError as exc:
        raw = {}
        if warnings is not None:
            warnings.append(f"{exc}（已按当前生效值显示）")

    rows = []
    for field in FIELDS:
        try:
            if field.key in raw:
                value = raw[field.key]
            else:
                value = getattr(settings, field.key, field.default)
            if field.scale > 1 and isinstance(value, (int, float)):
                value = int(value / field.scale)
            if field.kind == "list":
                value = list(value or [])
            elif isinstance(value, (set, frozenset, tuple)):
                value = list(value)
            elif isinstance(value, Path):
                value = str(value)
        except Exception as exc:  # noqa: BLE001 - 单个字段坏不能拖垮整页
            value = field.default
            if warnings is not None:
                warnings.append(f"{field.key} 读不出来：{exc}")
        row = field.to_dict()
        row["value"] = value
        rows.append(row)
    return rows
