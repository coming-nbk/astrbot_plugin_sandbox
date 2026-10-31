# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebUI 的后端控制器。

这一层不认识 Quart：输入是 dict，输出也是 dict，所以能在测试里直接调用。
main.py 只负责把 HTTP 请求拆成参数、把返回值塞进 jsonify。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Mapping

from .backends import rootfs_ready
from .bootstrap import read_marker
from .env import Runtime
from .health import HealthGuard
from .logbook import LogBook
from .metrics import snapshot as metrics_snapshot
from .paths import PathRejected, human_bytes
from .service import SandboxService
from .session import SandboxError, Session, sanitize_key
from .store import (
    PermissionStore,
    ScopeStore,
    StoreError,
    TrustStore,
    normalize_perm,
    normalize_scope,
    normalize_trust,
)
from .webfs import delete_path, list_tree, preview, save_text, session_summary

def same_origin(origin: str, host: str) -> bool:
    """Origin 的主机部分必须和 Host 一致，挡跨站表单提交。"""
    if not origin or not host:
        return False
    if origin == "null":
        return False
    stripped = origin.split("://", 1)[-1]
    return stripped.split("/", 1)[0].lower() == host.lower()


def is_json_request(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _ui_prefs_path(base_dir) -> Path:
    return Path(base_dir) / "ui_prefs.json"


def _ui_prefs(base_dir) -> dict:
    """界面偏好：选过语言没有、引导看过没有。iframe 里 localStorage 不可用，只能存服务端。"""
    try:
        data = json.loads(_ui_prefs_path(base_dir).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "language": str(data.get("language") or ""),
        "tour_seen": bool(data.get("tour_seen")),
    }


def _read_dismissed(base_dir) -> dict:
    try:
        data = json.loads(
            (Path(base_dir) / "security_dismissed.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _host_facts() -> dict:
    """给页面看的环境事实：内核、是否容器、有没有防火墙工具。"""
    import shutil

    from .audit_sec import kernel_release
    from .env import detect as detect_runtime

    try:
        runtime = detect_runtime()
        container = runtime.container_kind if runtime.in_container else ""
    except Exception:  # noqa: BLE001
        container = ""

    modules = Path("/lib/modules")
    return {
        "kernel": kernel_release(),
        "container": container,
        "host_kernel": bool(container),
        "nft": bool(shutil.which("nft")),
        "iptables": bool(shutil.which("iptables")),
        "modules": modules.is_dir() and any(modules.iterdir()),
    }


def _security_findings(service) -> list[dict]:
    """概览里带一份安全体检摘要，页面不用额外请求。"""
    try:
        findings = service.security_findings()
    except Exception:  # noqa: BLE001 - 体检失败不能拖垮概览
        return []
    return [item.to_dict() for item in findings]


def _security_counts(service) -> dict:
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in _security_findings(service):
        if item["severity"] in counts:
            counts[item["severity"]] += 1
    return counts


def _security_warnings(service) -> list[str]:
    findings = _security_findings(service)
    return [f"安全：{item['title']}" for item in findings[:3]]


def _read_last_bootstrap(base_dir) -> dict | None:
    """最近一次 rootfs 安装记录，重启后仍然可读。"""
    path = Path(base_dir) / "last_bootstrap.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ok(**fields: Any) -> dict:
    return {"ok": True, **fields}


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


class WebController:
    def __init__(
        self,
        service: SandboxService,
        permissions: PermissionStore,
        trust: TrustStore | None = None,
        scopes: ScopeStore | None = None,
        *,
        base_dir: Path,
        rootfs: Path,
        audit_path: Path,
        logbook: LogBook | None = None,
        runtime: Runtime | None = None,
        health: HealthGuard | None = None,
        worker=None,
        wizard=None,
        config_handler=None,
        started_at: float | None = None,
    ):
        self.service = service
        self.permissions = permissions
        self.trust = trust
        self.scopes = scopes
        self.base_dir = Path(base_dir)
        self.rootfs = Path(rootfs)
        self.audit_path = Path(audit_path)
        self.logbook = logbook
        self.runtime = runtime
        self.health = health
        self.worker = worker
        self.wizard = wizard
        self.config_handler = config_handler
        self.started_at = started_at or time.time()

    # ---------- 概览 ----------

    def overview(self) -> dict:
        settings = self.service.settings
        marker = read_marker(self.rootfs)
        sessions = self._scan_sessions()
        return _ok(
            backend=self.service.backend.name,
            backend_desc=self.service.backend.describe(),
            backend_notes=self.service.backend_notes,
            rootfs_ready=rootfs_ready(self.rootfs),
            rootfs_preset=marker.preset if marker else settings.rootfs_preset,
            rootfs_version=marker.version if marker else "",
            rootfs_arch=marker.arch if marker else "",
            rootfs_installed_at=marker.installed_at if marker else "",
            sessions=sessions,
            session_count=len(sessions),
            users=len(self.permissions.all()),
            trust_rules=len(self.trust.all()) if self.trust else 0,
            scope_rules=len(self.scopes.all()) if self.scopes else 0,
            enabled=settings.enabled,
            allow_all=settings.allow_all,
            quota_bytes=settings.quota_bytes,
            quota_text=human_bytes(settings.quota_bytes),
            timeout_seconds=settings.timeout_seconds,
            allow_network=settings.allow_network,
            allow_import=settings.allow_import,
            warnings=list(settings.warnings) + _security_warnings(self.service),
            security=_security_findings(self.service),
            security_summary=_security_counts(self.service),
            base_dir=str(self.base_dir),
            audit_path=str(self.audit_path),
            log_dir=str(self.logbook.dir) if self.logbook else "",
            log_files=len(self.logbook.files()) if self.logbook else 0,
            log_bytes=human_bytes(self.logbook.total_bytes()) if self.logbook else "0B",
            runtime=self.runtime.to_dict() if self.runtime else {},
            health=self.health.status().to_dict() if self.health else {},
            worker=self.worker.stats() if self.worker else {},
            uptime_seconds=int(time.time() - self.started_at),
            language=getattr(settings, "language", "zh"),
            ui=_ui_prefs(self.base_dir),
            metrics=metrics_snapshot(self.base_dir),
            bootstrap=_read_last_bootstrap(self.base_dir),
        )

    def _scan_sessions(self) -> list[dict]:
        root = Path(self.service.sessions_root)
        rows = []
        if not root.is_dir():
            return rows
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            size = 0
            files = 0
            for current, _dirs, names in os.walk(child):
                for name in names:
                    try:
                        size += (Path(current) / name).stat().st_size
                    except OSError:
                        continue
                    files += 1
            rows.append(
                {
                    "key": child.name,
                    "files": files,
                    "bytes": size,
                    "bytes_text": human_bytes(size),
                }
            )
        return rows

    # ---------- 权限 ----------

    def list_users(self) -> dict:
        settings = self.service.settings
        return _ok(
            users=[perm.to_dict() for perm in self.permissions.all()],
            global_config={
                "allow_all": settings.allow_all,
                "allowed_users": sorted(settings.allowed_users),
                "allowed_groups": sorted(settings.allowed_groups),
                "admin_users": sorted(settings.admin_users),
            },
            defaults={
                "quota_bytes": settings.quota_bytes,
                "quota_text": human_bytes(settings.quota_bytes),
                "timeout_seconds": settings.timeout_seconds,
                "allow_network": settings.allow_network,
                "allow_import": settings.allow_import,
            },
        )

    def save_user(self, payload: Mapping[str, Any]) -> dict:
        try:
            perm = normalize_perm(payload)
        except StoreError as exc:
            return _fail(str(exc))
        try:
            self.permissions.upsert(perm.to_dict())
        except StoreError as exc:
            return _fail(str(exc))
        return _ok(user=perm.to_dict())

    def delete_user(self, user_id: str) -> dict:
        try:
            removed = self.permissions.delete(user_id)
        except StoreError as exc:
            return _fail(str(exc))
        return _ok(removed=removed)

    # ---------- 配置 ----------

    def get_config(self) -> dict:
        if self.config_handler is None:
            return _fail("配置读写未启用")
        return self.config_handler.config_snapshot()

    def save_config(self, updates: Mapping[str, Any]) -> dict:
        if self.config_handler is None:
            return _fail("配置读写未启用")
        try:
            return self.config_handler.config_save(updates or {})
        except Exception as exc:  # noqa: BLE001 - 配置错误要原样回给页面
            return _fail(str(exc))

    # ---------- 部署向导 ----------

    def setup_probe(self) -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        return self.wizard.probe()

    def setup_test(self, deep: bool = False) -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        return self.wizard.self_test(deep=bool(deep))

    def setup_sources(self, per_preset: int = 2) -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        return self.wizard.check_sources(per_preset)

    def setup_rootfs(self, profile: str = "") -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        return self.wizard.start_rootfs(profile)

    def setup_task(self, task_id: str = "") -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        return self.wizard.task(task_id)

    def security_scan(self) -> dict:
        """风险清单：每条带「能不能一键修」和对应动作。"""
        items = _security_findings(self.service)
        fix_map = {
            "setuid-binaries": "suid",
        }
        dismissed = _read_dismissed(self.base_dir)
        out = []
        for item in items:
            if item["id"] in dismissed:
                continue
            action = fix_map.get(item["id"], "")
            # 本地检查是「确认适用」；feed / 知识库条目只知道「可能有关系」
            origin = "review" if item["id"].startswith(("feed-", "kb-")) else "confirmed"
            out.append(
                {**item, "fixable": bool(action), "action": action, "origin": origin}
            )
        counts = {"high": 0, "medium": 0, "low": 0}
        for item in out:
            if item["severity"] in counts:
                counts[item["severity"]] += 1
        return _ok(
            items=out,
            host=_host_facts(),
            counts=counts,
            ai=bool(getattr(self.service.settings, "ai_summary_enabled", False)),
            language=getattr(self.service.settings, "language", "zh"),
        )

    def security_fix(self, action: str = "") -> dict:
        """启动修复任务，立刻返回 task_id；进度用 security_task 轮询。"""
        try:
            result = self.service.start_fix(action)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"启动失败：{exc}")
        if not result.get("ok"):
            return _fail(result.get("error") or "启动失败")
        # 注意：_audit 的第一个参数本身就叫 action，这里不能再用 action= 关键字
        self._audit("security_fix", target=action)
        return _ok(task_id=result["task_id"], action=action)

    def security_summary(self, cve_id: str = "", text: str = "") -> dict:
        """把一条漏洞原文翻成中文并总结。带缓存，同一段文字只花一次钱。"""
        from .ai_summary import SummaryCache, SummaryError, summarize

        settings = self.service.settings
        if not getattr(settings, "ai_summary_enabled", False):
            return _fail("没有打开 AI 摘要：去「设置」里打开 ai_summary_enabled 并填接口地址")

        text = (text or "").strip()
        if not text:
            return _fail("没有可总结的原文")

        cache = SummaryCache(Path(self.base_dir) / "summary_cache.json")
        cached = cache.get(text)
        if cached:
            return _ok(id=cve_id, summary=cached, cached=True)

        try:
            summary = summarize(
                text,
                base_url=getattr(settings, "ai_summary_url", ""),
                api_key=getattr(settings, "ai_summary_key", ""),
                model=getattr(settings, "ai_summary_model", "gpt-4o-mini"),
            )
        except SummaryError as exc:
            return _fail(str(exc))

        cache.put(text, summary)
        self._audit("security_summary", cve=cve_id)
        return _ok(id=cve_id, summary=summary, cached=False)

    def ui_prefs_save(self, payload: Mapping[str, Any] | None = None) -> dict:
        """记下「选了哪种语言」「引导看过没」。"""
        payload = payload or {}
        current = _ui_prefs(self.base_dir)
        language = str(payload.get("language") or current["language"])
        if language and language not in ("zh", "en"):
            return _fail("语言只能是 zh 或 en")
        data = {
            "language": language,
            "tour_seen": bool(payload.get("tour_seen", current["tour_seen"])),
        }
        try:
            path = _ui_prefs_path(self.base_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            return _fail(f"写不进去：{exc}")

        # 语言同时写进插件配置，机器人回复也跟着变
        if language and self.config_handler is not None:
            try:
                self.config_handler.config_save({"language": language})
            except Exception:  # noqa: BLE001 - 配置写失败不影响界面
                pass
        return _ok(**data)

    def security_dismiss(self, finding_id: str = "") -> dict:
        """标记「已处理」，之后不再出现在风险列表里。"""
        finding_id = (finding_id or "").strip()
        if not finding_id:
            return _fail("缺少条目 id")
        data = _read_dismissed(self.base_dir)
        data[finding_id] = int(time.time())
        try:
            path = Path(self.base_dir) / "security_dismissed.json"
            path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            return _fail(f"写不进去：{exc}")
        self._audit("security_dismiss", finding=finding_id)
        return _ok(dismissed=finding_id)

    def security_task(self, task_id: str = "") -> dict:
        result = self.service.task_state(task_id)
        if not result.get("ok"):
            return _fail(result.get("error") or "任务不存在")
        return _ok(**result)

    def security_report(self) -> dict:
        """安全体检报告（含本地检查 + 可选外部漏洞源）。"""
        try:
            report = self.service.security_report(is_admin=True)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"体检失败：{exc}")
        return _ok(report=report, findings=_security_findings(self.service))

    def harden(self) -> dict:
        """清 rootfs 里的 setuid/setgid —— 插件自己能做的修复。"""
        try:
            message = self.service.harden_rootfs()
        except Exception as exc:  # noqa: BLE001
            return _fail(f"加固失败：{exc}")
        return _ok(message=message)

    def setup_apply(self, user_id: str = "", quota_mb: int = 0) -> dict:
        if self.wizard is None:
            return _fail("部署向导未启用")
        data = self.wizard.apply_defaults(user_id, quota_mb=int(quota_mb or 0))
        # 顺手生成这个用户的使用说明，管理员可以直接转给他
        if isinstance(data, dict) and data.get("ok") and self.service is not None:
            try:
                data["guide"] = self.service.guide_text(user_id, False)
            except Exception:  # noqa: BLE001 - 说明生成失败不该让授权失败
                data["guide"] = ""
        return data

    # ---------- 信任规则 ----------

    def list_trust(self) -> dict:
        if self.trust is None:
            return _ok(rules=[])
        return _ok(rules=[rule.to_dict() for rule in self.trust.all()])

    def save_trust(self, payload: Mapping[str, Any]) -> dict:
        if self.trust is None:
            return _fail("信任规则未启用")
        try:
            rule = self.trust.upsert(payload)
        except StoreError as exc:
            return _fail(str(exc))
        self._audit("trust_save", pattern=rule.pattern)
        return _ok(rule=rule.to_dict())

    def delete_trust(self, pattern: str) -> dict:
        if self.trust is None:
            return _fail("信任规则未启用")
        try:
            removed = self.trust.delete(pattern)
        except StoreError as exc:
            return _fail(str(exc))
        self._audit("trust_delete", pattern=pattern)
        return _ok(removed=removed)

    # ---------- 场景规则 ----------

    def list_scopes(self) -> dict:
        if self.scopes is None:
            return _ok(rules=[])
        return _ok(rules=[rule.to_dict() for rule in self.scopes.all()])

    def save_scope(self, payload: Mapping[str, Any]) -> dict:
        if self.scopes is None:
            return _fail("场景规则未启用")
        try:
            rule = normalize_scope(payload)
        except StoreError as exc:
            return _fail(str(exc))
        try:
            self.scopes.upsert(rule.to_dict())
        except StoreError as exc:
            return _fail(str(exc))
        self._audit("scope_save", key=rule.key, allow=rule.allow)
        return _ok(rule=rule.to_dict())

    def delete_scope(self, key: str) -> dict:
        if self.scopes is None:
            return _fail("场景规则未启用")
        try:
            removed = self.scopes.delete(key)
        except StoreError as exc:
            return _fail(str(exc))
        self._audit("scope_delete", key=key)
        return _ok(removed=removed)

    # ---------- 软件包 ----------

    def install_packages(self, packages: list[str]) -> dict:
        message = self.service.install_packages(packages or [])
        if message.startswith("安装完成"):
            return _ok(message=message)
        return _fail(message)

    # ---------- 文件 ----------

    def _service_for(self, user: str) -> SandboxService:
        """单用户权限（配额、超时、联网）要在这里生效。"""
        effective = self.service.effective_settings(user)
        if effective is self.service.settings:
            return self.service
        return self.service.with_settings(effective)

    def all_users(self) -> dict:
        """给文件页用的用户列表：有权限记录的 + 已有沙盒目录的。"""
        rows: list[dict] = []
        seen: set[str] = set()
        root = Path(self.service.sessions_root)

        for perm in self.permissions.all():
            key = sanitize_key(perm.user_id)
            seen.add(key)
            rows.append(
                {
                    "id": perm.user_id,
                    "key": key,
                    "label": perm.note or perm.user_id,
                    "has_session": (root / key).is_dir(),
                    "enabled": perm.enabled,
                }
            )

        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name not in seen:
                    rows.append(
                        {
                            "id": child.name,
                            "key": child.name,
                            "label": child.name + "（无权限记录）",
                            "has_session": True,
                            "enabled": None,
                        }
                    )
        return _ok(users=rows)

    def _session(self, user: str):
        if not str(user or "").strip():
            raise SandboxError("缺少 user 参数")
        text = str(user).strip()
        root = Path(self.service.sessions_root)
        # 传进来的是已经算好的目录名（文件页下拉里那种）就直接用，别再 hash 一次
        if (root / text).is_dir() and sanitize_key(text) != text:
            session = Session.from_key(root, text)
        else:
            session = self._service_for(text).session(text)
        session.ensure()
        return session

    def tree(self, user: str, path: str = ".", depth: int = 2) -> dict:
        try:
            session = self._session(user)
            data = list_tree(session, path or ".", depth=depth)
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        data["summary"] = session_summary(session)
        return _ok(**data)

    def preview_file(self, user: str, path: str) -> dict:
        try:
            session = self._session(user)
            data = preview(
                session, path, max_bytes=self.service.settings.max_output_bytes * 4
            )
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        return _ok(**data)

    def save_file(self, user: str, path: str, content: str) -> dict:
        try:
            service = self._service_for(user)
            session = self._session(user)
            data = save_text(
                session,
                path,
                content,
                max_bytes=service.settings.import_max_bytes,
                quota=service.settings.quota_bytes,
            )
            # 网页写进来的文件是 root 建的，要交出去，否则沙盒里读不了
            service.hand_over(session)
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        self._audit("web_save", user=user, path=path, bytes=data.get("size", 0))
        return _ok(**data)

    def raw_file(self, user: str, path: str) -> dict:
        """把沙盒里的文件读成 base64，给页面直接下载。

        不走消息/文件发送通道，所以不受 AstrBot 那个
        "Sandbox runtime is disabled by configuration" 的影响。
        """
        try:
            service = self._service_for(user)
            session = self._session(user)
            target = session.resolve(path)
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        if not target.is_file():
            return _fail("只能下载普通文件")

        size = target.stat().st_size
        limit = service.settings.import_max_bytes
        if limit > 0 and size > limit:
            return _fail(
                f"文件 {human_bytes(size)} 超过下载上限 {human_bytes(limit)}"
            )
        try:
            payload = target.read_bytes()
        except OSError as exc:
            return _fail(f"读取失败：{exc}")
        self._audit("download", user=user, path=path, bytes=size)
        return _ok(
            name=target.name,
            size=size,
            mime=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            b64=base64.b64encode(payload).decode("ascii"),
        )

    def delete_file(self, user: str, path: str) -> dict:
        try:
            session = self._session(user)
            removed = delete_path(session, path)
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        self._audit("web_delete", user=user, path=removed)
        return _ok(path=removed)

    def exec_command(self, user: str, command: str) -> dict:
        if not str(command or "").strip():
            return _fail("命令不能为空")
        try:
            service = self._service_for(user)
            session = self._session(user)
            result = service.execute(
                session,
                [service.settings.sandbox_shell, "-c", command],
                user=f"webui:{user}",
                action="web_exec",
                command=command,
            )
        except (SandboxError, PathRejected, OSError) as exc:
            return _fail(str(exc))
        return _ok(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=int(result.duration * 1000),
            timed_out=result.timed_out,
            truncated=result.truncated,
        )

    def logs(self, lines: int = 100) -> dict:
        if self.logbook is None:
            return _fail("日志未启用")
        return _ok(lines=self.logbook.tail(max(1, min(int(lines), 500))))

    def cleanup_now(self) -> dict:
        """手动触发一次清理，顺手把结果回报出来。"""
        removed = self.logbook.cleanup() if self.logbook else []
        purged = False
        if self.service.audit is not None:
            purged = self.service.audit.purge(self.service.settings.audit_retention_days)
        return _ok(removed=removed, audit_purged=purged)

    def session_key(self, user: str) -> str:
        return sanitize_key(user)

    def _audit(self, action: str, **fields: Any) -> None:
        if self.service.audit is not None:
            self.service.audit.write(action, **fields)

