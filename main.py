# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AstrBot 沙盒插件入口。

Copyright (C) 2026 maid

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See LICENSE for the full text.

本程序按 AGPLv3 发布：如果你把它改成网络服务对外提供，必须同样开放修改后的源码。

职责只有三件：
1. 把 AstrBot 的配置、事件对象翻译成 sandboxkit 的调用
2. 把 sandboxkit 的返回值翻译成 AstrBot 的消息结果
3. 生命周期：启动时准备 rootfs、选后端、开审计

真正的逻辑都在 sandboxkit/ 里，那一层不依赖 astrbot，可以单独测试。
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import importlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

def _build_chain(text: str):
    """拼一个纯文本消息链。不同 AstrBot 版本导出位置不一样，逐个试。"""
    try:
        from astrbot.api.message_components import Plain
    except Exception:  # noqa: BLE001
        return text
    for module_name in ("astrbot.api.event", "astrbot.core.message.message_event_result"):
        try:
            module = importlib.import_module(module_name)
            chain_cls = getattr(module, "MessageChain", None)
            if chain_cls is not None:
                return chain_cls([Plain(text)])
        except Exception:  # noqa: BLE001
            continue
    return [Plain(text)]


_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _reset_core_modules() -> str:
    """无条件清掉 sys.modules 里的 sandboxkit。

    卸载重装时 AstrBot 会删目录再解压回来，路径不变 —— 只看「来源目录」判断
    不出新旧，旧模块就留在缓存里，新 main.py 配上旧 config，报
    `Settings object has no attribute ...`。所以这里一律清掉重新加载。
    返回被清掉的模块原本来自哪里（用于日志）。
    """
    cached = sys.modules.get("sandboxkit")
    origin = getattr(cached, "__file__", "") or ""
    names = [
        key
        for key in list(sys.modules)
        if key == "sandboxkit" or key.startswith("sandboxkit.")
    ]
    for key in names:
        del sys.modules[key]
    return origin if names else ""


_STALE_CORE = _reset_core_modules()

# 版本对不上时给一句人话，而不是让 AttributeError 冒到 AstrBot 日志里
_REQUIRED_SETTINGS_FIELDS = (
    "rootfs_profile",
    "rootfs_image",
    "worker_isolation",
    "max_cores",
)


def _verify_core() -> str:
    from sandboxkit.config import Settings

    missing = [name for name in _REQUIRED_SETTINGS_FIELDS if not hasattr(Settings, name)]
    return ",".join(missing)
_MISSING_FIELDS = _verify_core()

try:
    from sandboxkit.audit import AuditLog
    from sandboxkit.backends import select_backend
    from sandboxkit.bootstrap import (
        BootstrapError,
        bootstrap_with_fallback,
        detect_arch,
        ensure_resolv_conf,
        is_ready,
    )
    from sandboxkit.config import load_settings
    from sandboxkit.env import advice as runtime_advice, detect as detect_runtime
    from sandboxkit.cve_store import CveStore
    from sandboxkit.guard import RateLimiter
    from sandboxkit.guide import GuideTracker
    from sandboxkit.health import HealthGuard
    from sandboxkit.setup import SetupWizard
    from sandboxkit.worker_client import WorkerClient
    from sandboxkit.logbook import LogBook
    from sandboxkit import pluginconf
    from sandboxkit.parse import split_command
    from sandboxkit.service import FileOut, SandboxService, TextOut
    from sandboxkit.store import PermissionStore, ScopeStore, TrustStore
    from sandboxkit.webapi import WebController
except ImportError:  # pragma: no cover - 以包形式被加载时的回退
    from .sandboxkit.audit import AuditLog
    from .sandboxkit.backends import select_backend
    from .sandboxkit.bootstrap import (
        BootstrapError,
        bootstrap_with_fallback,
        detect_arch,
        ensure_resolv_conf,
        is_ready,
    )
    from .sandboxkit.config import load_settings
    from .sandboxkit.env import advice as runtime_advice, detect as detect_runtime
    from .sandboxkit.cve_store import CveStore
    from .sandboxkit.guard import RateLimiter
    from .sandboxkit.guide import GuideTracker
    from .sandboxkit.health import HealthGuard
    from .sandboxkit.setup import SetupWizard
    from .sandboxkit.worker_client import WorkerClient
    from .sandboxkit.logbook import LogBook
    from .sandboxkit import pluginconf
    from .sandboxkit.parse import split_command
    from .sandboxkit.service import FileOut, SandboxService, TextOut
    from .sandboxkit.store import PermissionStore, ScopeStore, TrustStore
    from .sandboxkit.webapi import WebController

try:
    import astrbot.api.message_components as Comp
except ImportError:  # pragma: no cover
    Comp = None  # type: ignore[assignment]

try:  # AstrBot >= 4.29 推荐的插件 Web 层
    from astrbot.api.web import error_response, json_response, request
    WEB_NATIVE = True
except ImportError:  # pragma: no cover - 老版本回退到 quart
    WEB_NATIVE = False
    try:
        from quart import jsonify as _quart_jsonify, request
    except ImportError:
        _quart_jsonify = None  # type: ignore[assignment]
        request = None  # type: ignore[assignment]

    def json_response(payload, status: int = 200):
        if _quart_jsonify is None:
            return payload
        return _quart_jsonify(payload) if status == 200 else (_quart_jsonify(payload), status)

    def error_response(message: str, status: int = 400):
        return json_response({"ok": False, "error": message}, status)


def _resolve_data_dir() -> Path:
    """拿到 AstrBot 的数据目录；拿不到就退回插件目录。"""
    for module_name in (
        "astrbot.core.utils.astrbot_path",
        "astrbot.core.utils.path_utils",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for attr in ("get_astrbot_data_path", "get_data_path"):
            func = getattr(module, attr, None)
            if callable(func):
                try:
                    return Path(func())
                except Exception:  # noqa: BLE001 - 不同版本行为不一致
                    continue
    return _PLUGIN_DIR / "data"


@register(
    "astrbot_plugin_sandbox",
    "maid",
    "给 bot 一个能跑命令的独立沙盒：宿主机文件可导入、按用户/群授权、页面里管文件与执行",
    "1.0.1",
)
class SandboxPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        raw: dict[str, Any] = {}
        if config is not None:
            try:
                raw = dict(config)
            except (TypeError, ValueError):
                logger.warning("沙盒插件：配置格式异常，使用默认值")
        self.data_dir = _resolve_data_dir()
        self._config_path = pluginconf.find_config(
            self.data_dir, "astrbot_plugin_sandbox"
        )
        # AstrBot 自己的配置对象（不是副本）。保存时必须回写它，
        # 否则宿主一保存就会用它的内存状态覆盖我们写的文件。
        self._host_config = config
        # 启动时对齐：配置文件是准，宿主内存里不一致就改宿主
        synced = pluginconf.sync_into_host(self._config_path, config)
        if synced:
            logger.info(
                f"沙盒插件：已按配置文件对齐宿主配置（{', '.join(synced)}）"
            )
            try:
                raw = dict(config)
            except (TypeError, ValueError):
                pass
        self.raw_config = raw
        base_settings = load_settings(
            raw, fallback_import_roots=[str(self.data_dir)]
        )
        self.base_dir = (
            Path(base_settings.sandbox_root)
            if base_settings.sandbox_root
            else self.data_dir / "plugin_data" / "astrbot_plugin_sandbox"
        )
        self.settings = base_settings
        self.permissions = PermissionStore(self.base_dir / "permissions.json")
        self.trust = TrustStore(self.base_dir / "trust.json")
        self.scopes = ScopeStore(self.base_dir / "scopes.json")
        self.preferred_profile = self._read_preferred_profile()
        self.rootfs_dir = self.base_dir / "rootfs"
        self.sessions_root = self.base_dir / "sessions"
        self.audit = AuditLog(
            self.base_dir / "audit.jsonl",
            max_bytes=self.settings.audit_max_bytes,
            enabled=self.settings.audit_enabled,
        )
        self.logbook = LogBook(
            self.base_dir / "logs",
            enabled=self.settings.log_enabled,
            retention_days=self.settings.log_retention_days,
            max_files=self.settings.log_max_files,
            max_bytes=self.settings.log_max_bytes,
        )
        self._cleanup_task: asyncio.Task | None = None
        self._pool: ThreadPoolExecutor | None = None
        self.worker: WorkerClient | None = None
        self.health = HealthGuard(
            rss_limit_mb=self.settings.plugin_max_rss_mb,
            thread_limit=self.settings.plugin_max_threads,
            kill_switch=self.base_dir / "DISABLED",
        )
        self.web_limiter = RateLimiter(self.settings.web_writes_per_minute)
        self.runtime = detect_runtime()
        self.backend = None
        self.service: SandboxService | None = None
        self.controller: WebController | None = None
        self.wizard = None
        self.load_error = ""
        self.ready = False

    # ---------- 生命周期 ----------

    def _warn_duplicates(self) -> None:
        """同目录下还有别的副本时提醒一句：它们会抢同一个包名。"""
        if _STALE_CORE:
            logger.info(f"沙盒插件：已丢弃进程内的旧模块（{_STALE_CORE}）")
        parent = _PLUGIN_DIR.parent
        try:
            others = [
                child.name
                for child in parent.iterdir()
                if child.is_dir()
                and child.name.startswith("astrbot_plugin_sandbox")
                and child.resolve() != _PLUGIN_DIR
                and (child / "main.py").is_file()
                and ".bak-" not in child.name
            ]
        except OSError:
            others = []
        if others:
            logger.warning(
                "沙盒插件：发现重复副本 " + "、".join(others) + "，只保留一个"
            )

    async def initialize(self) -> None:
        if _MISSING_FIELDS:
            self.load_error = (
                "插件文件不完整：Settings 缺少 "
                f"{_MISSING_FIELDS}。请确认解压覆盖完整，并**完全重启 AstrBot**"
                "（重载插件不会清掉进程里的旧模块）"
            )
            logger.error(f"沙盒插件：{self.load_error}")
            return

        self._warn_duplicates()
        # 沙盒的活全部丢进自己的线程池：默认执行器是和 AstrBot 共用的，
        # 32 个线程被 30 秒的命令占满，宿主自己的 to_thread 就得排队
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(2, self.settings.max_concurrent_runs + 2),
                thread_name_prefix="sandbox",
            )
        try:
            await asyncio.to_thread(self._setup)
        except Exception as exc:  # noqa: BLE001 - 启动失败不应该拖垮 AstrBot
            logger.error(f"沙盒插件初始化失败：{exc}")
        if getattr(self.settings, "security_notify", True) and self.service is not None:
            try:
                self._watch_task = asyncio.create_task(self._security_watch())
            except RuntimeError:  # pragma: no cover - 没有事件循环
                self._watch_task = None

        try:
            self._register_web_api()
        except Exception as exc:  # noqa: BLE001 - WebUI 挂了不影响聊天功能
            logger.error(f"沙盒插件：注册 WebUI 接口失败（{exc}）")
        await self._start_cleanup()

    def config_snapshot(self) -> dict:
        return self._config_snapshot()

    def config_save(self, updates) -> dict:
        return self._config_save(updates)

    def _config_snapshot(self) -> dict:
        warnings: list[str] = []
        try:
            fields = pluginconf.snapshot(self._config_path, self.settings, warnings)
        except Exception as exc:  # noqa: BLE001 - 设置页不能整页 500
            logger.error(f"沙盒插件：读配置失败（{exc}）")
            return {
                "ok": False,
                "path": str(self._config_path),
                "fields": [],
                "error": f"读配置失败：{exc}",
            }
        return {
            "ok": True,
            "path": str(self._config_path),
            "fields": fields,
            "warnings": warnings,
        }

    def _config_save(self, updates) -> dict:
        merged = pluginconf.save_config(self._config_path, updates)
        self.raw_config = merged
        new_settings = load_settings(
            merged, fallback_import_roots=[str(self.data_dir)]
        )
        self.settings = new_settings
        self._sync_host_config(merged)
        if self.service is not None:
            self.service.settings = new_settings
        if self.wizard is not None:
            self.wizard.service = self.service
        restart = [
            field.key
            for field in pluginconf.FIELDS
            if field.key in updates and field.restart
        ]
        restart_labels = [
            field.label
            for field in pluginconf.FIELDS
            if field.key in updates and field.restart
        ]
        self.logbook.info("config_save", keys=",".join(sorted(updates)))
        return {
            "ok": True,
            "saved": sorted(updates),
            "restart_required": restart,
            "restart_labels": restart_labels,
            "path": str(self._config_path),
        }

    def _sync_host_config(self, merged: dict) -> None:
        """把改动写回 AstrBot 的配置对象，否则重启会被它覆盖。"""
        host = getattr(self, "_host_config", None)
        if host is None:
            return
        try:
            for key, value in merged.items():
                host[key] = value
            save = getattr(host, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:  # noqa: BLE001 - 宿主接口变了也不能让保存失败
            logger.warning(f"沙盒插件：回写宿主配置失败（{exc}）")

    def _read_preferred_profile(self) -> str:
        path = self.base_dir / "profile.json"
        try:
            import json

            data = json.loads(path.read_text("utf-8-sig"))
            return str(data.get("profile") or "")
        except (OSError, ValueError):
            return ""

    def _write_preferred_profile(self, name: str) -> None:
        if not name:
            return
        self.preferred_profile = name
        path = self.base_dir / "profile.json"
        try:
            import json

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"profile": name}, ensure_ascii=False), "utf-8")
        except OSError as exc:
            logger.warning(f"沙盒插件：记录默认套餐失败（{exc}）")

    def _setup(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)

        for warning in self.settings.warnings:
            logger.warning(f"沙盒插件：{warning}")

        for tip in runtime_advice(self.runtime):
            logger.info(f"沙盒插件：{tip}")
            self.logbook.info("runtime", tip=tip)
        if self.runtime.in_container:
            self.logbook.info(
                "runtime",
                container=self.runtime.container_kind,
                root=self.runtime.is_root,
                docker_cli=self.runtime.has_docker_cli,
                socket=self.runtime.docker_socket or "-",
            )

        self._prepare_rootfs()
        self.backend, notes = select_backend(self.settings, self.rootfs_dir)
        for note in notes:
            logger.warning(f"沙盒插件：{note}")

        if self.backend.name == "chroot":
            # chroot 之后只能看到 rootfs 内部，所以会话目录必须搬进去。
            # Docker 默认没有 CAP_SYS_ADMIN，bind mount 走不通，只能这样。
            self.sessions_root = self.rootfs_dir / "sandbox"
            self.sessions_root.mkdir(parents=True, exist_ok=True)
            logger.info(f"沙盒插件：chroot 模式，会话目录 = {self.sessions_root}")

        if self.settings.worker_isolation and self.worker is None:
            self.worker = WorkerClient(
                package_root=_PLUGIN_DIR, on_crash=self._on_worker_crash
            )

        self.service = SandboxService(
            self.settings,
            self.backend,
            self.sessions_root,
            audit=self.audit,
            rootfs=self.rootfs_dir,
            backend_notes=notes,
            rebootstrap=self._rebootstrap,
            permissions=self.permissions,
            trust=self.trust,
            scopes=self.scopes,
            logbook=self.logbook,
            worker=self.worker,
            guide=GuideTracker(self.base_dir / "guide_seen.json"),
            cve=CveStore(self.base_dir / "cve_kb.json"),
        )
        self.wizard = SetupWizard(
            self.service,
            self.permissions,
            base_dir=self.base_dir,
            rootfs=self.rootfs_dir,
            health=self.health,
            worker=self.worker,
            log=lambda level, message: self.logbook.write(level, message),
            preferred_getter=lambda: self.preferred_profile,
            preferred_setter=self._write_preferred_profile,
        )

        if self.worker is not None and self.worker.warmup():
            logger.info("沙盒插件：执行进程已预热")
        self.controller = WebController(
            self.service,
            self.permissions,
            trust=self.trust,
            scopes=self.scopes,
            base_dir=self.base_dir,
            rootfs=self.rootfs_dir,
            audit_path=self.base_dir / "audit.jsonl",
            logbook=self.logbook,
            runtime=self.runtime,
            health=self.health,
            worker=self.worker,
            wizard=self.wizard,
            config_handler=self,
        )
        self.ready = True
        logger.info(
            f"沙盒插件就绪：backend={self.backend.name} rootfs={self.rootfs_dir}"
        )
        self.logbook.info(
            "ready",
            backend=self.backend.name,
            rootfs=str(self.rootfs_dir),
            cores=self.settings.max_cores or "all",
            memory_mb=self.settings.memory_mb,
        )

    def _prepare_rootfs(self) -> None:
        needs_rootfs = self.settings.backend in ("auto", "bwrap", "chroot", "proot")
        if not needs_rootfs:
            return
        if is_ready(self.rootfs_dir):
            return
        if not self.settings.auto_bootstrap:
            logger.warning("沙盒插件：rootfs 未就绪且 auto_bootstrap 关闭")
            return

        try:
            url, version = self._rootfs_source()
        except BootstrapError as exc:
            logger.error(f"沙盒插件：{exc}")
            return

        preset = self.settings.rootfs_preset
        image = self.settings.rootfs_image
        packages = self.settings.bootstrap_packages
        if self.preferred_profile:
            from sandboxkit.profiles import get_profile

            chosen = get_profile(self.preferred_profile)
            preset, image, packages = chosen.preset, chosen.image, chosen.packages
            logger.info(f"沙盒插件：使用记住的套餐 {chosen.name}（{chosen.description}）")

        try:
            info = bootstrap_with_fallback(
                self.rootfs_dir,
                preset=preset,
                version=version,
                arch=detect_arch(),
                url=url,
                image=image,
                expected_sha256=self.settings.rootfs_sha256,
                packages=packages,
                log=lambda message: logger.info(f"沙盒插件：{message}"),
            )
        except BootstrapError as exc:
            logger.error(f"沙盒插件：rootfs 准备失败，将回落到 local 后端（{exc}）")
            return

        if self.settings.allow_network and ensure_resolv_conf(self.rootfs_dir):
            logger.info("沙盒插件：已写入 resolv.conf")
        logger.info(
            f"沙盒插件：rootfs {info.preset} {info.version} {info.arch} 就绪"
        )
        # 记一笔：这次是启动时自动下的，不是用户点的。界面上要告诉用户
        self._record_bootstrap(info, auto=True, reason="启动时 rootfs 未就绪")

    def _record_bootstrap(self, info, *, auto: bool, reason: str = "") -> None:
        """把最近一次 rootfs 安装写进 marker 文件，重启后界面还能看到。"""
        payload = {
            "auto": auto,
            "at": int(time.time()),
            "preset": getattr(info, "preset", ""),
            "version": getattr(info, "version", ""),
            "arch": getattr(info, "arch", ""),
            "reason": reason,
        }
        try:
            # 必须和 webapi 的读取方一致：都在 plugin_data 目录下
            path = Path(self.base_dir) / "last_bootstrap.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"沙盒插件：记录 bootstrap 状态失败（{exc}）")

    def _rootfs_source(self) -> tuple[str, str]:
        """返回 (下载地址, 版本)。

        只有用户显式填了 rootfs_url 才返回具体地址；否则返回空，
        让 bootstrap_with_fallback 自己去按「预置 -> 备选」逐个试 ——
        在这里先把地址算死，备选链就永远走不到。
        """
        if self.settings.rootfs_url:
            return self.settings.rootfs_url, self.settings.rootfs_version
        if self.settings.rootfs_preset == "custom":
            raise BootstrapError("rootfs_preset=custom 但没有填 rootfs_url")
        return "", ""

    def _on_worker_crash(self, reason: str) -> None:
        """worker 崩一次就记一次失败；连着崩会触发熔断，不再无限重开。"""
        opened = self.health.record_failure(f"worker: {reason}")
        logger.error(f"沙盒插件：执行进程异常（{reason}）")
        self.logbook.error("worker_crash", reason=reason)
        if opened:
            logger.error("沙盒插件：执行进程连续崩溃，已熔断")

    def _rebootstrap(self) -> str:
        try:
            url, version = self._rootfs_source()
            info = bootstrap_with_fallback(
                self.rootfs_dir,
                preset=self.settings.rootfs_preset,
                version=version,
                arch=detect_arch(),
                url=url,
                image=self.settings.rootfs_image,
                expected_sha256=self.settings.rootfs_sha256,
                packages=self.settings.bootstrap_packages,
                force=True,
                log=lambda message: logger.info(f"沙盒插件：{message}"),
            )
        except BootstrapError as exc:
            return f"重建失败：{exc}"
        self.backend, notes = select_backend(self.settings, self.rootfs_dir)
        if self.service is not None:
            self.service.backend = self.backend
            self.service.backend_notes = notes
        return (
            f"rootfs 已重建：{info.preset} {info.version} {info.arch}\n"
            f"后端：{self.backend.describe()}"
        )

    # ---------- 日志清理 ----------

    async def _start_cleanup(self) -> None:
        if not self.settings.log_enabled and not self.settings.audit_enabled:
            return
        try:
            await asyncio.to_thread(self._cleanup_once)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"沙盒插件：首次清理失败（{exc}）")
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def _cleanup_once(self) -> list[str]:
        removed = self.logbook.cleanup() if self.logbook.enabled else []
        if self.service is not None:
            try:
                self.service.sweep_quota()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"沙盒插件：配额巡检失败（{exc}）")
            try:
                self.service.prune_backend()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"沙盒插件：回收闲置容器失败（{exc}）")
        if self.audit.enabled:
            self.audit.purge(self.settings.audit_retention_days)
        if removed:
            logger.info(f"沙盒插件：清理了 {len(removed)} 个过期日志文件")
            self.logbook.info("cleanup", removed=len(removed))
        return removed

    async def _cleanup_loop(self) -> None:
        interval = max(1, self.settings.cleanup_interval_hours) * 3600
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(self._cleanup_once)
                except Exception as exc:  # noqa: BLE001 - 清理失败不该杀循环
                    logger.warning(f"沙盒插件：定时清理失败（{exc}）")
        except asyncio.CancelledError:
            return

    async def terminate(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        self.service = None
        logger.info("沙盒插件已卸载")

    async def _in_pool(self, func, *args, **kwargs):
        pool = self._pool
        if pool is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            pool, functools.partial(func, *args, **kwargs)
        )

    # ---------- 命令 ----------

    @filter.command("sandbox")
    async def sandbox_cmd(self, event: AstrMessageEvent):
        async for result in self._dispatch(event):
            yield result

    @filter.command("sbx")
    async def sbx_cmd(self, event: AstrMessageEvent):
        async for result in self._dispatch(event):
            yield result

    @filter.command("沙盒")
    async def sandbox_cn_cmd(self, event: AstrMessageEvent):
        async for result in self._dispatch(event):
            yield result

    # ---------- 安全巡检 ----------

    def _notify_path(self) -> Path:
        return Path(self.base_dir) / "notify_target.json"

    def _remember_target(self, event) -> None:
        """记住最后一次调用命令的管理员会话，提醒就发给他。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:  # noqa: BLE001
            return
        if not umo:
            return
        try:
            self._notify_path().write_text(
                json.dumps({"target": umo, "at": int(time.time())}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load_target(self) -> str:
        try:
            data = json.loads(self._notify_path().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return ""
        return str(data.get("target") or "")

    def _seen_path(self) -> Path:
        return Path(self.base_dir) / "security_notified.json"

    def _load_seen(self) -> dict:
        try:
            data = json.loads(self._seen_path().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_seen(self, seen: dict) -> None:
        # 只留 30 天，别无限涨
        cutoff = time.time() - 30 * 86400
        kept = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > cutoff}
        try:
            self._seen_path().write_text(
                json.dumps(kept, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    async def _security_once(self) -> str:
        """跑一次巡检，返回要发的文字；不需要发就返回空串。"""
        if not getattr(self.settings, "security_notify", True):
            return ""
        if self.service is None:
            return ""
        target = self._load_target()
        if not target:
            return ""

        findings = await self._in_pool(self.service.security_findings)
        high = [item for item in findings if item.severity == "high"]
        if not high:
            return ""

        seen = self._load_seen()
        fresh = [item for item in high if item.id not in seen]
        if not fresh:
            return ""

        now = time.time()
        for item in fresh:
            seen[item.id] = now
        self._save_seen(seen)

        lines = [f"沙盒安全巡检：{len(fresh)} 项新的高风险"]
        for item in fresh[:5]:
            lines.append(f"· {item.title}")
            if item.detail:
                lines.append(f"  {item.detail[:120]}")
            if item.fix:
                lines.append(f"  修法：{item.fix}")
        if len(fresh) > 5:
            lines.append(f"…还有 {len(fresh) - 5} 项")
        lines.append("")
        lines.append("详情：/sbx audit，或在 WebUI「安全」页直接处理。")
        return "\n".join(lines)

    async def _security_watch(self) -> None:
        interval = max(1, int(getattr(self.settings, "security_scan_hours", 12))) * 3600
        # 启动后先等一会儿，别和初始化抢资源
        await asyncio.sleep(90)
        while True:
            try:
                text = await self._security_once()
                if text:
                    target = self._load_target()
                    if target:
                        await self.context.send_message(target, _build_chain(text))
                        logger.info(f"沙盒插件：已向管理员推送 {len(text)} 字安全提醒")
            except asyncio.CancelledError:  # pragma: no cover - 正常退出
                raise
            except Exception as exc:  # noqa: BLE001 - 巡检失败不能拖垮插件
                logger.warning(f"沙盒插件：安全巡检失败（{exc}）")
            await asyncio.sleep(interval)

    @staticmethod
    def _platform_of(event) -> str:
        """拿当前平台名。不同 AstrBot 版本字段不一样，能拿到一个就行。"""
        for name in ("get_platform_name", "get_platform"):
            getter = getattr(event, name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:  # noqa: BLE001
                    value = None
                if value:
                    return str(value)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return umo.split(":", 1)[0] if umo else ""

    async def _cve_search(self, keyword: str) -> str:
        """借搜索插件抓一轮，把结果交给模型去判断。"""
        from sandboxkit import search_bridge

        path = search_bridge.find_crawler(
            getattr(self.settings, "security_search_path", ""),
            near=_PLUGIN_DIR,
        )
        if path is None:
            return (
                "没找到 astrbot_plugin_search_engines/crawler.py。\n"
                "把 security_search_path 指到那个插件的目录，或者把它装在同一个插件根目录下。"
            )

        # 用搜索插件自己那套网关代理：每次请求换出口 IP，避免被搜索引擎限流
        gateway = search_bridge.load_gateway(
            search_bridge.find_config(self.data_dir)
        )

        results, error = await search_bridge.search(
            keyword,
            crawler_path=path,
            engine=getattr(self.settings, "security_search_engine", "bing"),
            pages=1,
            gateway=gateway,
            limit=8,
        )
        if error:
            return error
        used = "（已用代理轮换）" if gateway else "（直连，没读到搜索插件的代理配置）"
        return (
            search_bridge.render(results, keyword)
            + f"\n{used}\n\n看完用 /sbx cve add id=... severity=... affects=... 记下来，下次 /sbx audit 会自动匹配。"
        )

    async def _dispatch(self, event: AstrMessageEvent):
        service = self.service
        if service is None:
            yield event.plain_result(
                self.load_error or "沙盒还没准备好，请查看 AstrBot 日志"
            )
            return

        allowed, reason = self.health.allow()
        if not allowed:
            yield event.plain_result(reason)
            return

        # 平台白名单：留空表示全部平台
        allowed_platforms = tuple(getattr(self.settings, "allowed_platforms", ()) or ())
        if allowed_platforms:
            platform = self._platform_of(event)
            if not platform:
                from sandboxkit.i18n import t as tr

                yield event.plain_result(
                    tr("err.platform_unknown", getattr(self.settings, "language", "zh"))
                )
                return
            if platform.lower() not in {item.lower() for item in allowed_platforms}:
                from sandboxkit.i18n import t as tr

                yield event.plain_result(
                    tr(
                        "err.platform_blocked",
                        getattr(self.settings, "language", "zh"),
                        platform=platform,
                        allowed="、".join(allowed_platforms),
                    )
                )
                return

        sub, arg = split_command(event.message_str)

        try:
            decision = service.decide(
                event.get_sender_id(), event.get_group_id() if hasattr(event, "get_group_id") else None
            )
            if getattr(decision, "is_admin", False):
                self._remember_target(event)
        except Exception:  # noqa: BLE001 - 记不住会话不影响命令
            pass

        if sub in ("cve", "漏洞") and arg.lower().startswith(("search ", "搜 ")):
            keyword = arg.split(None, 1)[1].strip()
            yield event.plain_result(await self._cve_search(keyword))
            return

        try:
            outputs = await self._in_pool(
                service.handle,
                user_id=str(event.get_sender_id()),
                group_id=str(event.get_group_id() or ""),
                sub=sub,
                arg=arg,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常穿透到 AstrBot
            opened = self.health.record_failure(str(exc))
            logger.error(f"沙盒插件执行异常：{exc}")
            if opened:
                logger.error("沙盒插件：连续内部错误，已暂停 300 秒")
            yield event.plain_result(f"沙盒执行异常：{exc}")
            return
        self.health.record_success()

        for out in outputs:
            if isinstance(out, FileOut):
                yield self._file_result(event, out)
            else:
                yield event.plain_result(out.text)

    def _file_result(self, event: AstrMessageEvent, out: FileOut):
        # 有些平台发不了任意文件（例如部分官方机器人接口），
        # 那种情况把 export_as_file 关掉，只回宿主路径。
        if not getattr(self.settings, "export_as_file", True):
            return event.plain_result(f"文件已导出到宿主：{out.path}")
        component = getattr(Comp, "File", None) if Comp is not None else None
        if component is None:
            return event.plain_result(f"文件已生成（宿主路径）：{out.path}")
        try:
            chain = [component(file=str(out.path), name=out.name)]
            return event.chain_result(chain)
        except TypeError:
            try:
                return event.chain_result([component(file=str(out.path))])
            except Exception:  # noqa: BLE001
                return event.plain_result(f"文件已生成（宿主路径）：{out.path}")
        except Exception:  # noqa: BLE001
            return event.plain_result(f"文件已生成（宿主路径）：{out.path}")

    # ---------- WebUI ----------

    WEB_PREFIX = "/astrbot_plugin_sandbox"

    def _register_web_api(self) -> None:
        if not self.settings.web_enabled:
            logger.info("沙盒插件：WebUI 已关闭（web_enabled=false）")
            return
        if request is None:
            logger.warning("沙盒插件：当前环境没有 Web 层，WebUI 不可用")
            return
        routes = (
            ("/overview", self._api_overview, ["GET"], "沙盒概览"),
            ("/users", self._api_users, ["GET"], "权限列表"),
            ("/users/save", self._api_user_save, ["POST"], "保存用户权限"),
            ("/users/delete", self._api_user_delete, ["POST"], "删除用户权限"),
            ("/users/all", self._api_all_users, ["GET"], "文件页的用户列表"),
            ("/tree", self._api_tree, ["GET"], "目录树"),
            ("/file", self._api_file, ["GET"], "文件预览"),
            ("/file/save", self._api_file_save, ["POST"], "保存文件"),
            ("/file/delete", self._api_file_delete, ["POST"], "删除文件"),
            ("/file/raw", self._api_file_raw, ["GET"], "下载沙盒文件（base64）"),
            ("/packages/install", self._api_packages, ["POST"], "安装软件包（root）"),
            # 注意：端点名不要用 "config"，会和宿主 AstrBot 自己的 /api/config 撞车
            ("/settings", self._api_config, ["GET"], "读取插件配置"),
            ("/settings/save", self._api_config_save, ["POST"], "保存插件配置"),
            ("/config", self._api_config, ["GET"], "读取插件配置（旧路径）"),
            ("/config/save", self._api_config_save, ["POST"], "保存插件配置（旧路径）"),
            ("/exec", self._api_exec, ["POST"], "在沙盒执行命令"),
            ("/setup/probe", self._api_setup_probe, ["GET"], "部署向导：环境自检"),
            ("/setup/test", self._api_setup_test, ["POST"], "部署向导：运行自检"),
            ("/setup/sources", self._api_setup_sources, ["GET"], "部署向导：检测下载源"),
            ("/setup/rootfs", self._api_setup_rootfs, ["POST"], "部署向导：下载 rootfs"),
            ("/setup/task", self._api_setup_task, ["GET"], "部署向导：任务进度"),
            ("/setup/apply", self._api_setup_apply, ["POST"], "部署向导：一键授权"),
            ("/security", self._api_security, ["GET"], "安全体检"),
            ("/security/scan", self._api_security_scan, ["GET"], "风险清单"),
            ("/security/fix", self._api_security_fix, ["POST"], "一键修复（异步）"),
            ("/security/task", self._api_security_task, ["GET"], "修复进度"),
            ("/security/summary", self._api_security_summary, ["POST"], "AI 翻译并总结"),
            ("/security/dismiss", self._api_security_dismiss, ["POST"], "标记已处理"),
            ("/ui/prefs", self._api_ui_prefs, ["POST"], "界面偏好（语言/引导）"),
            ("/harden", self._api_harden, ["POST"], "清 setuid 加固"),
            ("/trust", self._api_trust, ["GET"], "信任规则列表"),
            ("/trust/save", self._api_trust_save, ["POST"], "保存信任规则"),
            ("/trust/delete", self._api_trust_delete, ["POST"], "删除信任规则"),
            ("/scope", self._api_scope, ["GET"], "场景规则列表"),
            ("/scope/save", self._api_scope_save, ["POST"], "保存场景规则"),
            ("/scope/delete", self._api_scope_delete, ["POST"], "删除场景规则"),
            ("/logs", self._api_logs, ["GET"], "最近的运行日志"),
            ("/logs/cleanup", self._api_logs_cleanup, ["POST"], "立即清理过期日志"),
        )
        for suffix, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"{self.WEB_PREFIX}{suffix}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001 - 单条路由失败不影响其它
                logger.error(f"沙盒插件：注册 {suffix} 失败（{exc}）")

    def _ok(self, payload):
        return json_response(payload)

    def _fail(self, message: str, status: int = 400):
        return error_response(message, status)

    def _query(self, name: str, default: str = "") -> str:
        try:
            return request.query.get(name, default)
        except AttributeError:
            return request.args.get(name, default)

    async def _body(self) -> dict:
        try:
            data = await request.json(default={})
        except TypeError:
            data = await request.get_json(silent=True)
        except Exception:  # noqa: BLE001 - 请求体坏了就当空
            data = None
        return data if isinstance(data, dict) else {}

    def _controller(self):
        if self.controller is None:
            return None, self._fail("沙盒还没准备好", 503)
        return self.controller, None

    def _web_guard(self):
        """dashboard 模式下由 bridge 带着控制台身份转发，这里不再自己判。

        token 模式是给直连调试用的额外一层。
        """
        if request is None:
            return None
        if self.settings.web_auth in ("none", "dashboard"):
            return None
        token = self.settings.web_token
        given = ""
        try:
            given = request.headers.get("X-Sandbox-Token") or self._query("token")
        except Exception:  # noqa: BLE001
            given = ""
        if token and hmac.compare_digest(str(given), token):
            return None
        return self._fail("未授权：需要 web_token", 401)

    async def _api_call(self, name: str, *args):
        guard = self._web_guard()
        if guard is not None:
            return guard

        allowed, reason = self.health.allow()
        if not allowed:
            return self._fail(reason, 503)

        if name in WEB_WRITE_METHODS:
            ok, retry = self.web_limiter.allow("webui")
            if not ok:
                return self._fail(f"操作太频繁，{retry:.0f} 秒后再试", 429)

        controller, error = self._controller()
        if error is not None:
            return error
        try:
            data = await self._in_pool(getattr(controller, name), *args)
        except Exception as exc:  # noqa: BLE001 - 插件内部错误不能冒到宿主
            self.health.record_failure(str(exc))
            logger.error(f"沙盒插件：{name} 失败（{exc}）")
            return self._fail(f"内部错误：{exc}", 500)
        self.health.record_success()
        return self._ok(data)

    # ---------- 各接口 ----------

    async def _api_overview(self):
        return await self._api_call("overview")

    async def _api_users(self):
        return await self._api_call("list_users")

    async def _api_user_save(self):
        return await self._api_call("save_user", await self._body())

    async def _api_user_delete(self):
        body = await self._body()
        return await self._api_call("delete_user", str(body.get("user_id", "")))

    async def _api_all_users(self):
        return await self._api_call("all_users")

    async def _api_tree(self):
        depth = self._query("depth", "2")
        try:
            depth_value = int(depth)
        except (TypeError, ValueError):
            depth_value = 2
        return await self._api_call(
            "tree",
            self._query("user"),
            self._query("path", "."),
            depth_value,
        )

    async def _api_file(self):
        return await self._api_call(
            "preview_file", self._query("user"), self._query("path")
        )

    async def _api_file_save(self):
        body = await self._body()
        return await self._api_call(
            "save_file",
            str(body.get("user", "")),
            str(body.get("path", "")),
            str(body.get("content", "")),
        )

    async def _api_file_delete(self):
        body = await self._body()
        return await self._api_call(
            "delete_file", str(body.get("user", "")), str(body.get("path", ""))
        )

    async def _api_file_raw(self):
        return await self._api_call(
            "raw_file", self._query("user"), self._query("path")
        )

    async def _api_config(self):
        return await self._api_call("get_config")

    async def _api_config_save(self):
        return await self._api_call("save_config", await self._body())

    async def _api_packages(self):
        body = await self._body()
        packages = body.get("packages") or []
        if isinstance(packages, str):
            packages = packages.split()
        return await self._api_call("install_packages", [str(p) for p in packages])

    async def _api_exec(self):
        body = await self._body()
        return await self._api_call(
            "exec_command",
            str(body.get("user", "")),
            str(body.get("command", "")),
        )

    async def _api_trust(self):
        return await self._api_call("list_trust")

    async def _api_trust_save(self):
        return await self._api_call("save_trust", await self._body())

    async def _api_trust_delete(self):
        body = await self._body()
        return await self._api_call("delete_trust", str(body.get("pattern", "")))

    async def _api_scope(self):
        return await self._api_call("list_scopes")

    async def _api_scope_save(self):
        return await self._api_call("save_scope", await self._body())

    async def _api_scope_delete(self):
        body = await self._body()
        return await self._api_call("delete_scope", str(body.get("key", "")))

    async def _api_logs(self):
        try:
            lines = int(self._query("lines", "100"))
        except (TypeError, ValueError):
            lines = 100
        return await self._api_call("logs", lines)

    async def _api_logs_cleanup(self):
        return await self._api_call("cleanup_now")

    async def _api_setup_probe(self):
        return await self._api_call("setup_probe")

    async def _api_setup_test(self):
        body = await self._body()
        return await self._api_call("setup_test", bool(body.get("deep")))

    async def _api_setup_sources(self):
        return await self._api_call("setup_sources", 2)

    async def _api_setup_rootfs(self):
        body = await self._body()
        return await self._api_call("setup_rootfs", str(body.get("profile", "")))

    async def _api_setup_task(self):
        return await self._api_call("setup_task", self._query("task_id"))

    async def _api_security_scan(self):
        return await self._api_call("security_scan")

    async def _api_security_fix(self):
        body = await self._body()
        return await self._api_call("security_fix", str(body.get("action", "")))

    async def _api_ui_prefs(self):
        return await self._api_call("ui_prefs_save", await self._body())

    async def _api_security_dismiss(self):
        body = await self._body()
        return await self._api_call("security_dismiss", str(body.get("id", "")))

    async def _api_security_summary(self):
        body = await self._body()
        result = await self._summary_payload(
            str(body.get("id", "")), str(body.get("text", ""))
        )
        if result.get("ok"):
            return self._ok(result)
        return self._fail(result.get("error") or "摘要失败")

    def _summary_provider(self):
        """拿 AstrBot 当前选中的对话模型。拿不到就返回 None。"""
        for name in ("get_using_provider", "get_provider", "get_default_provider"):
            getter = getattr(self.context, name, None)
            if not callable(getter):
                continue
            try:
                provider = getter()
            except Exception:  # noqa: BLE001 - 不同版本签名不一样
                provider = None
            if provider is not None:
                return provider
        return None

    async def _summary_payload(self, cve_id: str, text: str) -> dict:
        """返回 {ok, summary} 或 {ok: False, error}，不抛异常。"""
        from sandboxkit.ai_summary import PROMPT, SummaryCache, SummaryError

        settings = self.settings
        if not getattr(settings, "ai_summary_enabled", False):
            return {"ok": False, "error": "没有打开 AI 摘要：去「设置」里打开 ai_summary_enabled"}

        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "没有可总结的原文"}

        cache = SummaryCache(Path(self.base_dir) / "summary_cache.json")
        cached = cache.get(text)
        if cached:
            return {"ok": True, "id": cve_id, "summary": cached, "cached": True}

        source = getattr(settings, "ai_summary_source", "current")
        if source == "current":
            provider = self._summary_provider()
            if provider is None:
                return {
                    "ok": False,
                    "error": "AstrBot 里没有可用的对话模型：去供应商里加一个，"
                    "或把 ai_summary_source 改成 custom 填接口",
                }
            try:
                response = await provider.text_chat(
                    prompt=PROMPT + "\n\n" + text[:4000],
                    session_id="sandbox-security-summary",
                )
            except Exception as exc:  # noqa: BLE001 - 模型侧的错都归这里
                return {"ok": False, "error": f"调用模型失败：{exc}"}
            summary = (
                getattr(response, "completion_text", None) or str(response or "")
            ).strip()
        else:
            from sandboxkit.ai_summary import summarize

            try:
                summary = await asyncio.to_thread(
                    summarize,
                    text,
                    base_url=getattr(settings, "ai_summary_url", ""),
                    api_key=getattr(settings, "ai_summary_key", ""),
                    model=getattr(settings, "ai_summary_model", "gpt-4o-mini"),
                )
            except SummaryError as exc:
                return {"ok": False, "error": str(exc)}

        if not summary:
            return {"ok": False, "error": "模型返回了空内容"}

        cache.put(text, summary)
        self.logbook.info("security_summary", cve=cve_id, source=source)
        return {"ok": True, "id": cve_id, "summary": summary, "cached": False}

    async def _api_security_task(self):
        return await self._api_call("security_task", self._query("task_id"))

    async def _api_security(self):
        return await self._api_call("security_report")

    async def _api_harden(self):
        return await self._api_call("harden")

    async def _api_setup_apply(self):
        body = await self._body()
        return await self._api_call(
            "setup_apply",
            str(body.get("user_id", "")),
            int(body.get("quota_mb") or 0),
        )

    # ---------- LLM 工具 ----------

    def _tool_ready(self, event: AstrMessageEvent):
        """统一的工具入口：走 decide（含单用户权限和场景规则），再套生效设置。"""
        service = self.service
        if service is None:
            return None, None, "沙盒还没准备好"
        allowed, reason = self.health.allow()
        if not allowed:
            return None, None, reason
        user_id = str(event.get_sender_id())
        decision = service.decide(user_id, str(event.get_group_id() or ""))
        if not decision.allowed:
            return None, None, decision.reason
        view = service.with_settings(service.effective_settings(user_id))
        return view, view.session(user_id), ""

    async def _run_exec(
        self,
        event: AstrMessageEvent,
        inner_factory,
        action: str,
        command: str = "",
    ) -> str:
        service, session, error = self._tool_ready(event)
        if service is None:
            return error
        try:
            inner = inner_factory(session)
            result = await self._in_pool(
                service.execute,
                session,
                inner,
                user=str(event.get_sender_id()),
                action=action,
                command=command,
            )
        except Exception as exc:  # noqa: BLE001 - 工具里抛异常会污染 LLM 调用链
            self.health.record_failure(str(exc))
            logger.error(f"沙盒插件：工具 {action} 失败（{exc}）")
            return f"沙盒内部错误：{exc}"
        self.health.record_success()
        return result.to_text(service.settings.reply_max_chars)

    async def _run_handle(self, event: AstrMessageEvent, sub: str, arg: str):
        service = self.service
        if service is None:
            return [TextOut("沙盒还没准备好")]
        try:
            return await self._in_pool(
                service.handle,
                user_id=str(event.get_sender_id()),
                group_id=str(event.get_group_id() or ""),
                sub=sub,
                arg=arg,
            )
        except Exception as exc:  # noqa: BLE001
            self.health.record_failure(str(exc))
            logger.error(f"沙盒插件：工具 {sub} 失败（{exc}）")
            return [TextOut(f"沙盒内部错误：{exc}")]

    @filter.llm_tool(name="sandbox_exec")
    async def sandbox_exec(self, event: AstrMessageEvent, command: str):
        """在沙盒里执行一条 shell 命令，返回 stdout/stderr。

        Args:
            command(string): 要执行的 shell 命令，例如 "ls -la"
        """
        # 工具的返回值会回灌给模型；yield 到聊天是错的
        return await self._run_exec(
            event,
            lambda _session: [self.settings.sandbox_shell, "-c", command],
            "llm_exec",
            command,
        )

    @filter.llm_tool(name="sandbox_python")
    async def sandbox_python(self, event: AstrMessageEvent, code: str):
        """在沙盒里用 python 执行一段代码。

        Args:
            code(string): 完整的 python 代码
        """
        return await self._run_exec(
            event,
            lambda session: [
                self.settings.python_bin,
                session.prepare_script(code),
            ],
            "llm_py",
            code,
        )

    @filter.llm_tool(name="sandbox_import")
    async def sandbox_import(self, event: AstrMessageEvent, host_path: str, name: str = ""):
        """把宿主上的文件复制进沙盒，之后可以在沙盒里读写它。

        Args:
            host_path(string): 宿主上的绝对路径，必须在管理员允许的目录内
            name(string): 可选，复制进沙盒后的文件名
        """
        arg = f"{host_path} as {name}" if name else host_path
        outputs = await self._run_handle(event, "import", arg)
        return "\n".join(
            out.text if isinstance(out, TextOut) else out.name for out in outputs
        )

    @filter.llm_tool(name="sandbox_export")
    async def sandbox_export(self, event: AstrMessageEvent, path: str):
        """把沙盒里的文件发回聊天。

        Args:
            path(string): 沙盒内的相对路径
        """
        outputs = await self._run_handle(event, "export", path)
        lines = []
        for out in outputs:
            if isinstance(out, FileOut):
                lines.append(f"已导出到宿主：{out.path}（{out.name}）")
            else:
                lines.append(out.text)
        return "\n".join(lines)

    @filter.llm_tool(name="sandbox_list")
    async def sandbox_list(self, event: AstrMessageEvent, path: str = "."):
        """列出沙盒目录里的文件。

        Args:
            path(string): 沙盒内的相对路径，默认当前目录
        """
        outputs = await self._run_handle(event, "ls", path)
        return "\n".join(
            out.text if isinstance(out, TextOut) else out.name for out in outputs
        )


WEB_WRITE_METHODS = frozenset(
    {
        "save_user",
        "delete_user",
        "save_file",
        "delete_file",
        "exec_command",
        "save_trust",
        "delete_trust",
        "save_scope",
        "delete_scope",
        "cleanup_now",
        "setup_test",
        "setup_rootfs",
        "setup_apply",
        "install_packages",
        "save_config",
    }
)


__all__ = ["SandboxPlugin"]
