# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""配置解析与访问控制。

设计要点：
- 所有取值都做类型收敛，WebUI 把数字存成字符串也不会炸。
- 非法值不抛异常，回落到默认值并记录 warning，插件仍然可用。
- 访问控制是纯函数，方便测试「谁能用沙盒」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .profiles import DEFAULT_PROFILE, PROFILES, resolve as resolve_profile
from typing import Any, Iterable, Mapping, Sequence

# local   仅目录隔离 + 资源限制，不隔离文件系统（最弱，但零依赖）
# bwrap   bubblewrap，非特权用户命名空间，推荐
# chroot  chroot 到解压出来的 minirootfs，需要 root
# proot   proot 用户态，无特权，慢
# docker  容器，需要 docker 守护进程
VALID_BACKENDS = ("auto", "local", "bwrap", "chroot", "proot", "docker")

# rootfs 预置；custom 表示只按 rootfs_url 下载
VALID_PRESETS = ("debian", "ubuntu", "alpine", "custom")

# 默认拒绝导入的宿主文件（按 basename 或完整路径做 fnmatch）
DEFAULT_DENY_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.kdbx",
    "shadow",
    "gshadow",
)

_TRUE = {"1", "true", "yes", "on", "y", "t", "是", "开", "启用", "打开"}
_FALSE = {"0", "false", "no", "off", "n", "f", "否", "关", "关闭", "禁用", ""}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        result = int(value)
    elif value is None or value == "":
        return default
    else:
        try:
            result = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    return result


def _as_list(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        return ()
    return tuple(item for item in items if item)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts: Iterable[str] = re.split(r"[,\n;，；]", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return tuple(part.strip() for part in parts if part.strip())


def _as_id_set(value: Any) -> frozenset[str]:
    return frozenset(_as_str_tuple(value))


@dataclass(frozen=True)
class Settings:
    """插件全部可调参数。字段名与 _conf_schema.json 的 key 一一对应。"""

    enabled: bool = True
    backend: str = "auto"
    sandbox_root: str = ""

    # 访问控制
    allow_all: bool = False
    allowed_users: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    admin_users: frozenset[str] = frozenset()

    # 宿主 -> 沙盒 导入
    allow_import: bool = True
    # 导出文件时：true=直接发文件，false=只回宿主路径（给发不了文件的平台）
    export_as_file: bool = True
    import_roots: tuple[str, ...] = ()
    import_deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS
    import_max_bytes: int = 8 * 1024 * 1024
    import_allow_dirs: bool = False
    import_max_files: int = 2000

    # 资源限制
    quota_bytes: int = 256 * 1024 * 1024
    quota_check_seconds: int = 30
    timeout_seconds: int = 30
    max_output_bytes: int = 32 * 1024
    memory_mb: int = 512
    cpu_seconds: int = 30
    max_procs: int = 0
    max_cores: int = 0
    memory_watchdog: bool = True
    cpu_percent_limit: int = 95
    cpu_burst_seconds: int = 5
    allow_network: bool = False
    allow_local_backend: bool = False
    worker_isolation: bool = True
    worker_fallback: bool = True
    max_runs_per_minute: int = 20
    max_concurrent_runs: int = 4

    # 后端细节
    rootfs_profile: str = DEFAULT_PROFILE
    rootfs_preset: str = "ubuntu"
    rootfs_url: str = ""
    rootfs_image: str = ""
    rootfs_version: str = ""
    rootfs_sha256: str = ""
    auto_bootstrap: bool = True
    bootstrap_packages: tuple[str, ...] = ()
    docker_image: str = "python:3.12-alpine"
    docker_user: str = ""
    docker_reuse: bool = True
    docker_idle_minutes: int = 30
    language: str = "zh"  # zh / en
    # 允许使用沙盒的平台（适配器名），留空表示全部
    allowed_platforms: tuple[str, ...] = ()
    sandbox_shell: str = "/bin/sh"
    # 命令层限制
    max_command_chars: int = 8192
    deny_patterns: tuple[str, ...] = ()
    allow_root_sandbox: bool = False
    # 安全体检：外部漏洞源（可选），格式见 audit_sec.fetch_feed
    security_feed_url: str = ""
    security_feed_ttl: int = 86400
    security_feed_format: str = "custom"
    # 定时巡检 + 私聊提醒管理员
    security_notify: bool = True
    security_scan_hours: int = 12
    # 用 AI 把漏洞原文翻成中文并总结（可选，默认关）
    ai_summary_enabled: bool = False
    ai_summary_source: str = "current"
    ai_summary_url: str = ""
    ai_summary_key: str = ""
    ai_summary_model: str = "gpt-4o-mini"
    # 借 astrbot_plugin_search_engines 的抓链能力查 CVE（可选）
    security_search_path: str = ""
    security_search_engine: str = "bing"
    python_bin: str = "python3"
    chroot_uid: int = 65534
    root_hardening: bool = True
    chroot_gid: int = 65534

    # WebUI
    web_enabled: bool = True
    web_writes_per_minute: int = 30
    web_auth: str = "dashboard"
    web_token: str = ""

    # 输出
    reply_max_chars: int = 3000
    audit_enabled: bool = True
    audit_max_bytes: int = 2 * 1024 * 1024
    audit_retention_days: int = 30

    log_enabled: bool = True
    log_retention_days: int = 7
    log_max_files: int = 30
    log_max_bytes: int = 5 * 1024 * 1024
    cleanup_interval_hours: int = 12
    plugin_max_rss_mb: int = 0
    plugin_max_threads: int = 0

    warnings: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        data = {}
        for key, value in self.__dict__.items():
            if isinstance(value, frozenset):
                data[key] = sorted(value)
            elif isinstance(value, tuple):
                data[key] = list(value)
            else:
                data[key] = value
        return data


def load_settings(
    raw: Mapping[str, Any] | None,
    *,
    fallback_import_roots: Sequence[str] = (),
) -> Settings:
    """把 WebUI 配置收敛成 Settings。永远返回可用对象。"""
    source: Mapping[str, Any] = raw or {}
    warnings: list[str] = []

    backend = _as_str(source.get("backend"), "auto").lower()
    if backend not in VALID_BACKENDS:
        warnings.append(f"未知 backend「{backend}」，已回落到 auto")
        backend = "auto"

    import_roots = _as_str_tuple(source.get("import_roots"))
    if not import_roots:
        import_roots = tuple(str(item) for item in fallback_import_roots if str(item))
        if import_roots:
            warnings.append("未配置 import_roots，默认只允许导入 AstrBot 数据目录")

    raw_deny = source.get("import_deny_globs")
    deny_globs = _as_str_tuple(raw_deny) if raw_deny is not None else DEFAULT_DENY_GLOBS

    settings = Settings(
        enabled=_as_bool(source.get("enabled"), True),
        backend=backend,
        sandbox_root=_as_str(source.get("sandbox_root"), ""),
        allow_all=_as_bool(source.get("allow_all"), False),
        allowed_users=_as_id_set(source.get("allowed_users")),
        allowed_groups=_as_id_set(source.get("allowed_groups")),
        admin_users=_as_id_set(source.get("admin_users")),
        allow_import=_as_bool(source.get("allow_import"), True),
        export_as_file=_as_bool(source.get("export_as_file"), True),
        import_roots=import_roots,
        import_deny_globs=deny_globs,
        import_max_bytes=_as_int(
            source.get("import_max_bytes"), 8 * 1024 * 1024, minimum=0
        ),
        import_allow_dirs=_as_bool(source.get("import_allow_dirs"), False),
        import_max_files=_as_int(
            source.get("import_max_files"), 2000, minimum=0, maximum=100000
        ),
        quota_bytes=_as_int(
            source.get("quota_bytes"), 256 * 1024 * 1024, minimum=1024 * 1024
        ),
        quota_check_seconds=_as_int(
            source.get("quota_check_seconds"), 30, minimum=0, maximum=3600
        ),
        timeout_seconds=_as_int(
            source.get("timeout_seconds"), 30, minimum=1, maximum=3600
        ),
        max_output_bytes=_as_int(
            source.get("max_output_bytes"), 32 * 1024, minimum=1024
        ),
        memory_mb=_as_int(source.get("memory_mb"), 512, minimum=64, maximum=65536),
        cpu_seconds=_as_int(source.get("cpu_seconds"), 30, minimum=1, maximum=3600),
        max_procs=_as_int(source.get("max_procs"), 0, minimum=0, maximum=4096),
        max_cores=_as_int(source.get("max_cores"), 0, minimum=0, maximum=256),
        memory_watchdog=_as_bool(source.get("memory_watchdog"), True),
        cpu_percent_limit=_as_int(
            source.get("cpu_percent_limit"), 95, minimum=0, maximum=400
        ),
        cpu_burst_seconds=_as_int(
            source.get("cpu_burst_seconds"), 5, minimum=0, maximum=600
        ),
        allow_network=_as_bool(source.get("allow_network"), False),
        allow_local_backend=_as_bool(source.get("allow_local_backend"), False),
        worker_isolation=_as_bool(source.get("worker_isolation"), True),
        worker_fallback=_as_bool(source.get("worker_fallback"), True),
        max_runs_per_minute=_as_int(
            source.get("max_runs_per_minute"), 20, minimum=0, maximum=600
        ),
        max_concurrent_runs=_as_int(
            source.get("max_concurrent_runs"), 4, minimum=1, maximum=64
        ),
        rootfs_profile=_as_str(source.get("rootfs_profile"), DEFAULT_PROFILE).lower(),
        rootfs_preset=_as_str(source.get("rootfs_preset"), "").lower(),
        rootfs_url=_as_str(source.get("rootfs_url"), ""),
        rootfs_image=_as_str(source.get("rootfs_image"), ""),
        rootfs_version=_as_str(source.get("rootfs_version"), ""),
        rootfs_sha256=_as_str(source.get("rootfs_sha256"), "").lower(),
        auto_bootstrap=_as_bool(source.get("auto_bootstrap"), True),
        bootstrap_packages=_as_str_tuple(source.get("bootstrap_packages")),
        docker_image=_as_str(source.get("docker_image"), "python:3.12-alpine"),
        docker_user=_as_str(source.get("docker_user"), ""),
        docker_reuse=_as_bool(source.get("docker_reuse"), True),
        docker_idle_minutes=_as_int(
            source.get("docker_idle_minutes"), 30, minimum=1, maximum=1440
        ),
        language=_as_str(source.get("language"), "zh").lower(),
        allowed_platforms=_as_list(source.get("allowed_platforms")),
        sandbox_shell=_as_str(source.get("sandbox_shell"), "/bin/sh"),
        max_command_chars=_as_int(source.get("max_command_chars"), 8192, minimum=64),
        deny_patterns=_as_list(source.get("deny_patterns")),
        allow_root_sandbox=_as_bool(source.get("allow_root_sandbox"), False),
        security_feed_url=_as_str(source.get("security_feed_url"), ""),
        security_feed_ttl=_as_int(source.get("security_feed_ttl"), 86400, minimum=60),
        security_feed_format=_as_str(source.get("security_feed_format"), "custom").lower(),
        security_notify=_as_bool(source.get("security_notify"), True),
        security_scan_hours=_as_int(source.get("security_scan_hours"), 12, minimum=1),
        ai_summary_enabled=_as_bool(source.get("ai_summary_enabled"), False),
        ai_summary_source=_as_str(source.get("ai_summary_source"), "current").lower(),
        ai_summary_url=_as_str(source.get("ai_summary_url"), ""),
        ai_summary_key=_as_str(source.get("ai_summary_key"), ""),
        ai_summary_model=_as_str(source.get("ai_summary_model"), "gpt-4o-mini"),
        security_search_path=_as_str(source.get("security_search_path"), ""),
        security_search_engine=_as_str(source.get("security_search_engine"), "bing"),
        python_bin=_as_str(source.get("python_bin"), "python3"),
        chroot_uid=_as_int(source.get("chroot_uid"), 65534, minimum=0),
        root_hardening=_as_bool(source.get("root_hardening"), True),
        chroot_gid=_as_int(source.get("chroot_gid"), 65534, minimum=0),
        web_enabled=_as_bool(source.get("web_enabled"), True),
        web_writes_per_minute=_as_int(
            source.get("web_writes_per_minute"), 30, minimum=0, maximum=600
        ),
        web_auth=_as_str(source.get("web_auth"), "dashboard").lower(),
        web_token=_as_str(source.get("web_token"), ""),
        reply_max_chars=_as_int(source.get("reply_max_chars"), 3000, minimum=200),
        audit_enabled=_as_bool(source.get("audit_enabled"), True),
        audit_max_bytes=_as_int(
            source.get("audit_max_bytes"), 2 * 1024 * 1024, minimum=64 * 1024
        ),
        audit_retention_days=_as_int(
            source.get("audit_retention_days"), 30, minimum=1, maximum=3650
        ),
        log_enabled=_as_bool(source.get("log_enabled"), True),
        log_retention_days=_as_int(
            source.get("log_retention_days"), 7, minimum=1, maximum=365
        ),
        log_max_files=_as_int(source.get("log_max_files"), 30, minimum=1, maximum=3650),
        log_max_bytes=_as_int(
            source.get("log_max_bytes"), 5 * 1024 * 1024, minimum=64 * 1024
        ),
        cleanup_interval_hours=_as_int(
            source.get("cleanup_interval_hours"), 12, minimum=1, maximum=168
        ),
        plugin_max_rss_mb=_as_int(
            source.get("plugin_max_rss_mb"), 0, minimum=0, maximum=65536
        ),
        plugin_max_threads=_as_int(
            source.get("plugin_max_threads"), 0, minimum=0, maximum=10000
        ),
        warnings=tuple(warnings),
    )

    if settings.rootfs_profile not in PROFILES:
        settings = replace(
            settings,
            rootfs_profile=DEFAULT_PROFILE,
            warnings=settings.warnings
            + (f"未知 rootfs_profile「{settings.rootfs_profile}」，已回落到 standard",),
        )

    preset, image, packages = resolve_profile(
        settings.rootfs_profile,
        preset=settings.rootfs_preset,
        image=settings.rootfs_image,
        packages=settings.bootstrap_packages,
    )
    if (
        settings.rootfs_preset != preset
        or settings.rootfs_image != image
        or settings.bootstrap_packages != packages
    ):
        settings = replace(
            settings,
            rootfs_preset=preset,
            rootfs_image=image,
            bootstrap_packages=packages,
        )

    extra: list[str] = list(settings.warnings)
    if settings.rootfs_preset not in VALID_PRESETS:
        extra.append(
            f"未知 rootfs_preset「{settings.rootfs_preset}」，已回落到 debian"
        )
        settings = replace(settings, rootfs_preset="debian")
    if settings.rootfs_preset == "custom" and not settings.rootfs_url:
        extra.append("rootfs_preset=custom 但没有填 rootfs_url，rootfs 无法准备")
    if settings.allow_all and not settings.admin_users:
        extra.append("allow_all 已开启且没有配置 admin_users，任何人都能重置沙盒")
    if settings.backend == "local":
        extra.append(
            "local 后端不隔离文件系统；allow_local_backend=false 时命令会被拒绝执行"
        )
    if settings.allow_network and settings.backend in ("bwrap", "docker"):
        extra.append("allow_network 已开启，沙盒可访问外网")
    if settings.web_auth not in ("dashboard", "token", "none"):
        extra.append(f"未知 web_auth「{settings.web_auth}」，已回落到 dashboard")
        settings = replace(settings, web_auth="dashboard")
    if settings.web_auth == "token" and not settings.web_token:
        extra.append("web_auth=token 但 web_token 为空，WebUI 将无法访问")
    if settings.web_auth == "none":
        extra.append("web_auth=none：任何能访问 WebUI 端口的人都能操作沙盒")
    if settings.max_runs_per_minute == 0:
        extra.append("max_runs_per_minute=0：不限频率，建议至少设 10")
    if settings.import_max_bytes == 0:
        extra.append("import_max_bytes=0，导入不限大小")
    if extra != list(settings.warnings):
        settings = replace(settings, warnings=tuple(extra))
    return settings


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    is_admin: bool
    reason: str


def check_access(
    settings: Settings,
    user_id: str,
    group_id: str | None = None,
) -> AccessDecision:
    """判断某个用户能否使用沙盒。

    顺序：功能开关 -> 管理员 -> allow_all -> 用户白名单 -> 群白名单。
    """
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()

    if not settings.enabled:
        return AccessDecision(False, False, "沙盒功能已被管理员关闭")
    if uid and uid in settings.admin_users:
        return AccessDecision(True, True, "管理员")
    if settings.allow_all:
        return AccessDecision(True, False, "allow_all 模式")
    if uid and uid in settings.allowed_users:
        return AccessDecision(True, False, "用户在 allowed_users 中")
    if gid and gid in settings.allowed_groups:
        return AccessDecision(True, False, "群在 allowed_groups 中")
    return AccessDecision(False, False, "你没有沙盒使用权限，请联系管理员开通")
