# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""沙盒服务层：把「一句话」翻译成「一次受限执行」。

main.py 只管 AstrBot 的事件对象，所有真正的逻辑在这里。
这一层不 import astrbot，所以可以用 local 后端在测试里跑通整条链路。
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .audit import AuditLog
from .backends import Backend, BackendError, Prepared, rootfs_ready
from .config import AccessDecision, Settings, check_access
from .guard import ConcurrencyGuard, RateLimiter
from .bootstrap import install_packages as rootfs_install
from .limits import ExecResult, Limits, build_env, run
from .logbook import LogBook
from .worker_client import WorkerClient
from .paths import PathRejected, human_bytes, resolve_host_file
from .session import SandboxError, Session, tree_size
from .store import (
    PermissionStore,
    ScopeStore,
    TrustStore,
    apply_user_perm,
    match_scope,
    match_trust,
)


@dataclass
class TextOut:
    text: str


@dataclass
class FileOut:
    path: Path
    name: str


Out = TextOut | FileOut

HELP_BODY = """沙盒用法（命令前加 /sandbox，可简写 /sbx）：

  run <命令>            在沙盒里执行 shell 命令
  py <代码>             用 python 执行一段代码（需要 rootfs 内装了 python3）
  ls [路径]             列出沙盒里的文件
  cat <路径>            查看文本文件
  rm <路径>             删除文件或目录
  import <宿主路径> [as 名字]   把宿主文件复制进沙盒 imports/
  export <路径>         把沙盒文件发回聊天
  usage                 查看配额占用
  backend               查看当前隔离后端
  reset                 清空自己的沙盒（不可恢复）"""


class SandboxService:
    def __init__(
        self,
        settings: Settings,
        backend: Backend,
        sessions_root: Path | str,
        *,
        audit: AuditLog | None = None,
        rootfs: Path | None = None,
        backend_notes: Sequence[str] = (),
        rebootstrap: Callable[[], str] | None = None,
        permissions: PermissionStore | None = None,
        trust: TrustStore | None = None,
        scopes: ScopeStore | None = None,
        logbook: LogBook | None = None,
        limiter: RateLimiter | None = None,
        concurrency: ConcurrencyGuard | None = None,
        worker: WorkerClient | None = None,
        guide: "GuideTracker | None" = None,
        cve: "CveStore | None" = None,
    ):
        self.settings = settings
        self.backend = backend
        self.sessions_root = Path(sessions_root)
        self.audit = audit
        self.rootfs = Path(rootfs) if rootfs else None
        self.backend_notes = list(backend_notes)
        self.rebootstrap = rebootstrap
        self.permissions = permissions
        self.trust = trust
        self.scopes = scopes
        self.logbook = logbook
        self.worker = worker
        self.guide = guide
        self.cve = cve
        # 后台任务：修复要能「慢慢跑 + 有进度」
        self._tasks: dict[str, dict] = {}
        # 这两道闸必须跨副本共享：with_settings 会克隆 service，
        # 每次新建的话，带单用户权限的人每次请求都是全新的空桶，限流形同虚设
        self.limiter = limiter or RateLimiter(settings.max_runs_per_minute)
        self.concurrency = concurrency or ConcurrencyGuard(
            settings.max_concurrent_runs
        )

    # ---------- 对外入口 ----------

    def _perm(self, user_id: str):
        return self.permissions.get(user_id) if self.permissions else None

    def decide(self, user_id: str, group_id: str | None) -> AccessDecision:
        """用户级 > 场景规则 > 全局白名单。"""
        if not self.settings.enabled:
            return AccessDecision(False, False, "沙盒功能已被管理员关闭")
        uid = str(user_id or "")
        is_admin = uid in self.settings.admin_users

        perm = self._perm(uid)
        if perm is not None:
            if not perm.enabled:
                return AccessDecision(False, is_admin, "你的沙盒权限已被管理员关闭")
            return AccessDecision(True, is_admin, "permissions 授权")

        if self.scopes is not None:
            rule = match_scope(
                self.scopes.all(), group_id, not str(group_id or "").strip()
            )
            if rule is not None:
                if rule.users and uid not in rule.users:
                    return AccessDecision(False, is_admin, "当前场景下你没有沙盒权限")
                if not rule.allow:
                    return AccessDecision(
                        False, is_admin, rule.note or "当前场景禁止使用沙盒"
                    )
                return AccessDecision(True, is_admin, f"场景规则 {rule.key}")

        return check_access(self.settings, user_id, group_id)

    def effective_settings(self, user_id: str) -> Settings:
        perm = self.permissions.get(user_id) if self.permissions else None
        return apply_user_perm(self.settings, perm)

    def _denied_pattern(self, command: str) -> str:
        """返回命中的禁用片段，没命中返回空串。"""
        lowered = (command or "").lower()
        for pattern in getattr(self.settings, "deny_patterns", ()) or ():
            text = str(pattern).strip().lower()
            if text and text in lowered:
                return str(pattern)
        return ""

    def can_execute(self) -> tuple[bool, str]:
        """没有隔离后端时，默认不允许执行命令 —— 宁可不可用，也不给宿主 shell。"""
        if (
            self.backend.name == "chroot"
            and int(getattr(self.settings, "chroot_uid", 65534)) == 0
            and not getattr(self.settings, "allow_root_sandbox", False)
        ):
            return False, (
                "沙盒配置成了以 root 运行（chroot_uid=0），但没打开 allow_root_sandbox。"
                "root 一旦逃逸就是整台机器，所以默认拒绝执行；"
                "确实需要请在设置里打开这一项。"
            )

        if self.backend.name == "local" and not self.settings.allow_local_backend:
            return False, (
                "当前没有可用的隔离后端（bwrap / chroot / proot / docker），"
                "命令已被拒绝。请安装 bubblewrap 或配置 rootfs；"
                "确认接受风险后可把 allow_local_backend 设为 true。"
            )
        return True, ""

    def with_settings(self, settings: Settings) -> "SandboxService":
        return SandboxService(
            settings,
            self.backend,
            self.sessions_root,
            audit=self.audit,
            rootfs=self.rootfs,
            backend_notes=self.backend_notes,
            rebootstrap=self.rebootstrap,
            permissions=self.permissions,
            trust=self.trust,
            scopes=self.scopes,
            logbook=self.logbook,
            limiter=self.limiter,
            concurrency=self.concurrency,
            worker=self.worker,
        )

    def handle(
        self,
        *,
        user_id: str,
        group_id: str | None,
        sub: str,
        arg: str,
    ) -> list[Out]:
        decision = self.decide(user_id, group_id)
        if not decision.allowed:
            self._log("denied", user=user_id, group=group_id, reason=decision.reason)
            return [TextOut(decision.reason)]

        effective = self.effective_settings(user_id)
        view = self if effective is self.settings else self.with_settings(effective)

        plain = (sub or "").strip().lower()
        first_time = False
        if self.guide is not None and plain not in ("guide", "说明", "教程"):
            first_time = self.guide.mark(user_id)

        outs = view._handle(decision, user_id, group_id, sub, arg, first_time=first_time)
        if first_time and plain not in ("", "help", "帮助", "?"):
            outs = list(outs) + [
                TextOut("\n第一次用？发 /sbx guide 看完整说明和你的限制。")
            ]
        return outs

    def _handle(
        self,
        decision: AccessDecision,
        user_id: str,
        group_id: str | None,
        sub: str,
        arg: str,
        *,
        first_time: bool = False,
    ) -> list[Out]:
        sub = (sub or "").strip().lower()
        arg = (arg or "").strip()

        if sub in ("guide", "说明", "教程"):
            return [TextOut(self.guide_text(user_id, decision.is_admin))]

        if sub in ("cve", "漏洞"):
            return [TextOut(self._cve_command(arg, decision.is_admin, user_id))]

        if sub in ("audit", "security", "安全", "安全检查"):
            return [TextOut(self.security_report(is_admin=decision.is_admin))]

        if sub in ("harden", "加固"):
            if not decision.is_admin:
                return [TextOut("只有管理员可以执行加固。")]
            return [TextOut(self.harden_rootfs())]

        # 超长命令 / 带 NUL 的命令直接拒绝：既防误用，也防模型被诱导堆一条巨长的东西
        from .i18n import t as tr

        lang = getattr(self.settings, "language", "zh")
        limit = int(getattr(self.settings, "max_command_chars", 8192))
        if len(arg) > limit:
            return [TextOut(tr("err.too_long", lang, limit=limit))]
        if "\x00" in arg:
            return [TextOut(tr("err.nul", lang))]

        # 管理员配的禁用片段：按子串匹配，命中就拒
        hit = self._denied_pattern(arg)
        if hit:
            return [TextOut(tr("err.denied", lang, pattern=hit))]

        if sub in ("", "help", "帮助", "?"):
            # 第一次来就直接给完整说明，别丢一份命令表让人自己猜
            if first_time:
                return [TextOut(self.guide_text(user_id, decision.is_admin))]
            return [TextOut(self._help_text(decision.is_admin))]

        session = self.session(user_id)
        try:
            session.ensure()
        except OSError as exc:
            return [TextOut(f"无法创建沙盒目录：{exc}")]

        try:
            if sub == "run":
                if not self._capability(user_id, "allow_exec", "执行命令"):
                    return [TextOut("管理员没有给你执行命令的权限")]
                return [TextOut(self._run_shell(session, arg, user_id))]
            if sub in ("py", "python"):
                if not self._capability(user_id, "allow_py", "运行 Python"):
                    return [TextOut("管理员没有给你运行 Python 的权限")]
                return [TextOut(self._run_python(session, arg, user_id))]
            if sub in ("ls", "list"):
                return [TextOut(self._list(session, arg))]
            if sub in ("cat", "read"):
                return [TextOut(self._cat(session, arg))]
            if sub in ("rm", "del"):
                return [TextOut(self._rm(session, arg, user_id))]
            if sub == "import":
                return [TextOut(self._import(session, arg, user_id))]
            if sub == "export":
                if not self._capability(user_id, "allow_export", "导出文件"):
                    return [TextOut("管理员没有给你导出文件的权限")]
                return [self._export(session, arg, user_id)]
            if sub in ("usage", "quota", "df"):
                return [TextOut(self._usage(session))]
            if sub in ("backend", "info"):
                return [TextOut(self._backend_info(session))]
            if sub == "bootstrap":
                if not decision.is_admin:
                    return [TextOut("只有管理员可以执行 bootstrap")]
                if self.rebootstrap is None:
                    return [TextOut("bootstrap 未启用")]
                return [TextOut(self.rebootstrap())]
            if sub in ("install", "pkg"):
                if not decision.is_admin:
                    return [TextOut("只有管理员可以安装软件包（会影响所有用户）")]
                return [TextOut(self.install_packages(arg.split()))]
            if sub == "reset":
                if not self._capability(user_id, "allow_reset", "重置沙盒"):
                    return [TextOut("管理员没有给你重置沙盒的权限")]
                session.reset()
                self._log("reset", user=user_id)
                return [TextOut("沙盒已清空。")]
            return [TextOut(f"未知子命令「{sub}」。\n\n{HELP_BODY}")]
        except SandboxError as exc:
            return [TextOut(f"失败：{exc}")]
        except PathRejected as exc:
            return [TextOut(f"失败：{exc}")]
        except OSError as exc:
            return [TextOut(f"文件系统操作失败：{exc}")]

    def _capability(self, user_id: str, field: str, label: str) -> bool:
        perm = self._perm(user_id)
        if perm is None:
            return True
        return bool(getattr(perm, field, True))

    def session(self, key: str) -> Session:
        return Session(self.sessions_root, key)

    # ---------- 子命令 ----------

    PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,63}$")

    def install_packages(self, packages: Sequence[str]) -> str:
        """以 root 在 rootfs 里装包。

        沙盒内的进程是 nobody，apt 写不了 dpkg 数据库；所以装包走这条独立通道，
        由插件以 root 执行，只暴露「装哪些包」这一件事，不给沙盒 root shell。
        """
        names = [str(item).strip() for item in packages if str(item).strip()]
        if not names:
            return "用法：/sbx install <包名> [更多包名]"
        if len(names) > 12:
            return "一次最多 12 个包"
        bad = [name for name in names if not self.PACKAGE_NAME.match(name)]
        if bad:
            return "包名不合法：" + "、".join(bad)

        if self.backend.name == "docker":
            return "docker 后端的软件包应写进镜像，不要在这里装"
        if not rootfs_ready(self.rootfs):
            return "rootfs 未就绪，先下载"

        lines: list[str] = []
        ok, message = rootfs_install(
            self.rootfs, names, log=lambda text: lines.append(str(text))
        )
        self._log("install", packages=names, ok=ok)
        head = "安装完成" if ok else "安装失败"
        detail = "\n".join(lines[-6:])
        return f"{head}：{' '.join(names)}\n{detail}" + (f"\n{message}" if message else "")

    def _run_shell(self, session: Session, arg: str, user_id: str) -> str:
        if not arg:
            return "用法：/sbx run <命令>"
        allowed, reason = self.can_execute()
        if not allowed:
            return reason
        inner = [self.settings.sandbox_shell, "-c", arg]
        result = self.execute(
            session, inner, user=user_id, action="run", command=arg
        )
        return self._format(result)

    def _run_python(self, session: Session, arg: str, user_id: str) -> str:
        if not arg:
            return "用法：/sbx py <代码>"
        allowed, reason = self.can_execute()
        if not allowed:
            return reason
        rel = session.prepare_script(arg)
        self._own(session.dir / rel)
        inner = [self.settings.python_bin, rel]
        result = self.execute(
            session, inner, user=user_id, action="py", command=arg
        )
        text = self._format(result)
        if result.exit_code == 127:
            text += (
                "\n\n提示：沙盒里没有 python3。"
                "在插件配置里把 bootstrap_packages 设为 [\"python3\"] 后执行 /sbx bootstrap。"
            )
        return text

    def _list(self, session: Session, arg: str) -> str:
        rows = session.list_dir(arg or ".")
        if not rows:
            return "（空目录）"
        lines = [
            f"{'DIR ' if is_dir else 'FILE'}  {human_bytes(size):>9}  {name}"
            for name, size, is_dir in rows
        ]
        lines.append(f"共 {len(rows)} 项")
        return "\n".join(lines)

    def _cat(self, session: Session, arg: str) -> str:
        if not arg:
            return "用法：/sbx cat <路径>"
        return session.read_text(arg, self.settings.max_output_bytes)

    def _rm(self, session: Session, arg: str, user_id: str) -> str:
        if not arg:
            return "用法：/sbx rm <路径>"
        removed = session.remove(arg)
        self._log("rm", user=user_id, path=removed)
        return f"已删除 {removed}"

    def _import(self, session: Session, arg: str, user_id: str) -> str:
        if not self.settings.allow_import:
            return "管理员关闭了宿主导入功能"
        if not arg:
            return "用法：/sbx import <宿主绝对路径> [as 沙盒内文件名]"

        raw_path, name = arg, ""
        lowered = arg.lower()
        if " as " in lowered:
            index = lowered.rfind(" as ")
            raw_path, name = arg[:index], arg[index + 4 :].strip()

        source = resolve_host_file(
            raw_path,
            roots=self.settings.import_roots,
            deny_globs=self.settings.import_deny_globs,
            allow_dirs=self.settings.import_allow_dirs,
        )
        target = session.import_host_file(
            source,
            name=name,
            max_bytes=self.settings.import_max_bytes,
            quota=self.settings.quota_bytes,
            deny_globs=self.settings.import_deny_globs,
            max_files=self.settings.import_max_files,
        )
        self._own(target)
        self.hand_over(session)   # 目录里新建的东西也要交给运行用户
        rel = session.relative(target)
        self._log(
            "import",
            user=user_id,
            source=str(source),
            target=rel,
            bytes=tree_size(target),
        )
        return (
            f"已导入 {rel}（{human_bytes(tree_size(target))}）\n"
            f"现在可以：/sbx run cat {rel}"
        )

    def _export(self, session: Session, arg: str, user_id: str) -> Out:
        if not arg:
            return TextOut("用法：/sbx export <沙盒内路径>")
        target = session.resolve(arg)
        if not target.is_file():
            return TextOut("只能导出普通文件")
        size = target.stat().st_size
        limit = self.settings.import_max_bytes
        if limit > 0 and size > limit:
            return TextOut(
                f"文件 {human_bytes(size)} 超过导出上限 {human_bytes(limit)}"
            )
        rel = session.relative(target)
        self._log("export", user=user_id, path=rel, bytes=size)
        return FileOut(path=target, name=Path(rel).name)

    def _usage(self, session: Session) -> str:
        usage = session.usage()
        return (
            f"沙盒目录：{session.key}\n"
            f"配额：{usage.to_text(self.settings.quota_bytes)}\n"
            f"后端：{self.backend.name}"
        )

    def _backend_info(self, session: Session) -> str:
        lines = [
            f"后端：{self.backend.describe()}",
            f"rootfs：{'就绪 ' + str(self.rootfs) if rootfs_ready(self.rootfs) else '未就绪'}",
            f"超时：{self.settings.timeout_seconds}s",
            f"内存上限：{self.settings.memory_mb}MB",
            f"输出上限：{human_bytes(self.settings.max_output_bytes)}",
            f"网络：{'允许' if self.settings.allow_network else '禁止（local/proot 后端无法强制）'}",
            f"配额：{human_bytes(self.settings.quota_bytes)}",
        ]
        lines.extend(f"提示：{note}" for note in self.backend_notes)
        return "\n".join(lines)

    def _cve_command(self, arg: str, is_admin: bool, user_id: str) -> str:
        """本地 CVE 知识库：list / match / rm / add。"""
        from .audit_sec import kernel_release
        from .cve_store import CveError

        if self.cve is None:
            return "CVE 知识库未启用。"

        parts = (arg or "").strip().split(None, 1)
        action = parts[0].lower() if parts else "list"
        rest = parts[1] if len(parts) > 1 else ""

        if action in ("", "list", "列表"):
            return self.cve.render()

        if action in ("match", "命中"):
            items = self.cve.matching(kernel=kernel_release(), backend=self.backend.name)
            if not items:
                return f"当前内核 {kernel_release()} / 后端 {self.backend.name} 没有命中的条目。"
            return self.cve.render(items)

        if not is_admin:
            return "只有管理员可以改 CVE 知识库。"

        if action in ("rm", "del", "删除"):
            if not rest:
                return "用法：/sbx cve rm <CVE 编号>"
            return "已删除。" if self.cve.remove(rest.strip()) else "没找到这条。"

        if action in ("add", "添加"):
            fields: dict[str, str] = {}
            for token in rest.split():
                if "=" not in token:
                    continue
                key, _, value = token.partition("=")
                fields[key.strip().lower()] = value.strip()
            try:
                item = self.cve.upsert(
                    cve_id=fields.get("id", ""),
                    title=fields.get("title", ""),
                    severity=fields.get("severity", "medium"),
                    affects=fields.get("affects", ""),
                    detail=fields.get("detail", ""),
                    fix=fields.get("fix", ""),
                    added_by=user_id,
                )
            except CveError as exc:
                return f"没记上：{exc}\n格式：/sbx cve add id=CVE-xxxx severity=high affects=^5.15 title=... fix=..."
            return f"已记录 {item['id']}（{item['severity']}）"

        return (
            "用法：\n"
            "/sbx cve            看全部\n"
            "/sbx cve match      只看和本机内核相关的\n"
            "/sbx cve add id=CVE-xxxx severity=high affects=^5.15 title=... fix=...\n"
            "/sbx cve rm <编号>\n"
            "/sbx cve search <关键词>   借搜索插件去抓"
        )

    def security_findings(self) -> list:
        """本地检查 + 外部 feed + 本地 CVE 库，合并排序后的条目列表。"""
        from .audit_sec import fetch_feed, kernel_release, local_findings, match_feed

        findings = local_findings(
            self.settings, backend=self.backend.name, rootfs=self.rootfs
        )

        feed_url = getattr(self.settings, "security_feed_url", "")
        if feed_url:
            cache = (
                Path(self.sessions_root).parent / "security_feed_cache.json"
                if self.sessions_root
                else None
            )
            items = fetch_feed(
                feed_url,
                fmt=getattr(self.settings, "security_feed_format", "custom"),
                cache_path=cache,
                ttl=int(getattr(self.settings, "security_feed_ttl", 86400)),
            )
            findings += match_feed(
                items, kernel=kernel_release(), backend=self.backend.name
            )

        if self.cve is not None:
            from .audit_sec import Finding
            from .pkginfo import installed_versions

            packages = installed_versions(self.rootfs)
            for item in self.cve.matching(
                kernel=kernel_release(),
                backend=self.backend.name,
                packages=packages,
            ):
                findings.append(
                    Finding(
                        id="kb-" + item["id"],
                        title=f"{item['id']} {item.get('title', '')}".strip(),
                        severity=item.get("severity", "medium"),
                        detail=item.get("detail", ""),
                        fix=item.get("fix", "按条目里的建议处理。"),
                    )
                )

        order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        findings.sort(key=lambda item: order.get(item.severity, 9))
        return findings

    def security_report(self, *, is_admin: bool) -> str:
        """安全体检。只有管理员能跑，因为报告里含路径和配置细节。"""
        from .audit_sec import (
            fetch_feed,
            kernel_release,
            local_findings,
            match_feed,
            render,
        )

        if not is_admin:
            return "只有管理员可以跑安全体检。"

        from .audit_sec import kernel_release, render

        return render(
            self.security_findings(),
            kernel=kernel_release(),
            backend=self.backend.name,
            lang=getattr(self.settings, "language", "zh"),
        )

    # ---------- 后台任务 ----------

    def _task_new(self, kind: str) -> str:
        task_id = f"{kind}-{uuid.uuid4().hex[:8]}"
        self._tasks[task_id] = {
            "kind": kind,
            "lines": [],
            "done": False,
            "ok": False,
            "started": time.time(),
        }
        # 只留最近 20 个，别无限涨
        if len(self._tasks) > 20:
            for old in sorted(self._tasks, key=lambda k: self._tasks[k]["started"])[:5]:
                self._tasks.pop(old, None)
        return task_id

    def task_state(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在或已过期"}
        return {
            "ok": True,
            "task": {
                "id": task_id,
                "kind": task["kind"],
                "lines": list(task["lines"]),
                "done": task["done"],
                "ok": task["ok"],
            },
        }

    def start_fix(self, action: str) -> dict:
        """启动一个修复任务，立刻返回 task_id；进度靠 task_state 轮询。"""
        action = (action or "").strip()
        if action not in ("suid", "upgrade"):
            return {"ok": False, "error": f"不认识的动作：{action or '（空）'}"}

        task_id = self._task_new(action)
        task = self._tasks[task_id]

        def run() -> None:
            try:
                if action == "suid":
                    task["lines"].append("扫描 rootfs 里的 setuid/setgid…")
                    message = self.harden_rootfs()
                else:
                    from .bootstrap import upgrade_packages

                    if not self.rootfs or not Path(self.rootfs).is_dir():
                        task["lines"].append("rootfs 未就绪")
                        task["ok"] = False
                        return
                    task["lines"].append("开始升级 rootfs 软件包（可能要几分钟）…")
                    ok, output = upgrade_packages(
                        Path(self.rootfs),
                        log=lambda text: task["lines"].append(str(text)),
                    )
                    task["ok"] = bool(ok)
                    task["lines"].append(output[:400])
                    task["lines"].append("升级完成" if ok else "升级失败")
                    return
                task["lines"].append(message)
                task["ok"] = True
            except Exception as exc:  # noqa: BLE001 - 任务失败不能让线程炸掉
                task["lines"].append(f"失败：{exc}")
                task["ok"] = False
            finally:
                task["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def upgrade_rootfs(self) -> str:
        """把沙盒 rootfs 的包升到最新（不动宿主内核）。"""
        from .bootstrap import upgrade_packages

        if not self.rootfs or not Path(self.rootfs).is_dir():
            return "rootfs 还没就绪，先下载。"
        lines: list[str] = []
        ok, output = upgrade_packages(
            Path(self.rootfs), log=lambda text: lines.append(str(text))
        )
        self._log("rootfs_upgrade", ok=ok)
        head = "升级完成" if ok else "升级失败"
        tail = "\n".join(lines[-6:])
        return f"{head}\n{tail}\n{output}"[:1500]

    def harden_rootfs(self) -> str:
        """把 rootfs 里的 setuid/setgid 位清掉。这是插件自己能做的修复。"""
        from .bootstrap import strip_suid

        if not self.rootfs or not Path(self.rootfs).is_dir():
            return "rootfs 还没就绪，没什么可清的。"
        cleared = strip_suid(Path(self.rootfs))
        self._log("harden", cleared=cleared)
        if not cleared:
            return "扫描完成：rootfs 里没有 setuid/setgid 文件。"
        return f"已清掉 {cleared} 个 setuid/setgid 位。"

    def guide_text(self, user_id: str, is_admin: bool) -> str:
        """按这个用户当前生效的配置生成使用说明。"""
        from .guide import build_guide

        perm = self._perm(user_id)
        caps = {
            "allow_exec": getattr(perm, "allow_exec", True) if perm else True,
            "allow_py": getattr(perm, "allow_py", True) if perm else True,
            "allow_export": getattr(perm, "allow_export", True) if perm else True,
            "allow_reset": getattr(perm, "allow_reset", True) if perm else True,
        }
        settings = self.settings
        return build_guide(
            lang=getattr(self.settings, "language", "zh"),
            user_id=user_id,
            is_admin=is_admin,
            quota_bytes=settings.quota_bytes,
            timeout_seconds=settings.timeout_seconds,
            memory_mb=settings.memory_mb,
            max_runs_per_minute=settings.max_runs_per_minute,
            allow_network=settings.allow_network,
            allow_import=settings.allow_import,
            backend=self.backend.name,
            preset=settings.rootfs_preset,
            version=settings.rootfs_version,
            caps=caps,
        )

    def _help_text(self, is_admin: bool) -> str:
        text = HELP_BODY
        if self.backend.name == "local":
            text += "\n\n注意：当前是 local 后端，沙盒里的命令能读写宿主文件系统。"
        if is_admin:
            text += "\n\n管理员：/sbx bootstrap 重新下载并解压 minirootfs"
        return text

    # ---------- 执行 ----------

    def limits(
        self,
        *,
        bypass_memory: bool = False,
        bypass_cpu: bool = False,
        timeout: float | None = None,
    ) -> Limits:
        return Limits(
            timeout=float(timeout or self.settings.timeout_seconds),
            memory_mb=self.settings.memory_mb,
            cpu_seconds=self.settings.cpu_seconds,
            max_procs=self.settings.max_procs,
            max_file_bytes=self.settings.quota_bytes,
            max_output_bytes=self.settings.max_output_bytes,
            max_cores=self.settings.max_cores,
            memory_watchdog_mb=(
                self.settings.memory_mb if self.settings.memory_watchdog else 0
            ),
            cpu_percent_limit=self.settings.cpu_percent_limit,
            cpu_burst_seconds=float(self.settings.cpu_burst_seconds),
            bypass_memory=bypass_memory,
            bypass_cpu=bypass_cpu,
            harden=(
                self.settings.root_hardening
                and self.backend.name == "chroot"
                and self.settings.chroot_uid == 0
            ),
        )

    def prepare(self, session: Session, inner: Sequence[str]) -> Prepared:
        self.hand_over(session)
        return self.backend.prepare(
            inner,
            session_dir=session.dir,
            settings=self.settings,
            rootfs=self.rootfs,
        )

    def _run_prepared(self, prepared: Prepared, limits: Limits, env) -> ExecResult:
        """有 worker 就走 worker，崩了也不带走宿主。"""
        worker = self.worker
        if worker is not None and self.settings.worker_isolation:
            result = worker.run(
                prepared.argv,
                cwd=prepared.cwd,
                limits=limits,
                env=env,
                preexec_spec=prepared.preexec_spec,
            )
            if result.error and not result.timed_out and self.settings.worker_fallback:
                if self.logbook is not None:
                    self.logbook.warn("worker_fallback", reason=result.error[:200])
                return run(
                    prepared.argv,
                    cwd=prepared.cwd,
                    limits=limits,
                    env=env,
                    preexec=prepared.preexec,
                )
            return result
        return run(
            prepared.argv,
            cwd=prepared.cwd,
            limits=limits,
            env=env,
            preexec=prepared.preexec,
        )

    @staticmethod
    def _refusal(inner: Sequence[str], message: str) -> ExecResult:
        return ExecResult(
            argv=[str(item) for item in inner],
            exit_code=-1,
            stdout="",
            stderr="",
            duration=0.0,
            error=message,
        )

    def execute(
        self,
        session: Session,
        inner: Sequence[str],
        *,
        user: str = "",
        action: str = "exec",
        command: str = "",
    ) -> ExecResult:
        session.ensure()
        for name in ("imports", "tmp", "out"):
            self._own(session.dir / name)
        self._own(session.dir)

        allowed, reason = self.can_execute()
        if not allowed:
            return self._refusal(inner, reason)

        ok, retry = self.limiter.allow(user or "anonymous")
        if not ok:
            return self._refusal(inner, f"请求太频繁，{retry:.0f} 秒后再试")
        if not self.concurrency.acquire():
            return self._refusal(inner, "当前并发任务已满，稍后再试")

        rule = match_trust(self.trust.all(), command) if (self.trust and command) else None
        bypass_memory = bool(rule and rule.bypass_memory)
        bypass_cpu = bool(rule and rule.bypass_cpu)
        timeout_override = float(rule.timeout_seconds) if rule and rule.timeout_seconds else None

        quota = self.settings.quota_bytes
        started = time.monotonic()
        try:
            # 扫一遍目录在文件多的时候不便宜，所以按 quota_check_seconds 缓存
            usage_before = session.usage_cached(
                float(self.settings.quota_check_seconds)
            )
            if quota > 0 and usage_before.bytes > quota:
                return self._refusal(
                    inner,
                    f"沙盒已超出配额（{human_bytes(usage_before.bytes)} / "
                    f"{human_bytes(quota)}），先清理再执行",
                )
            prepared = self.prepare(session, inner)
            env = build_env(prepared.home or str(session.dir))
            env.update(prepared.env_extra)
            limits = self.limits(
                bypass_memory=bypass_memory,
                bypass_cpu=bypass_cpu,
                timeout=timeout_override,
            )
            result = self._run_prepared(prepared, limits, env)
        except (OSError, ValueError, BackendError) as exc:
            return self._refusal(inner, f"准备执行失败：{exc}")
        finally:
            self.concurrency.release()

        session.invalidate()
        self._log(
            action,
            user=user,
            argv=prepared.argv,
            backend=self.backend.name,
            exit=result.exit_code,
            timeout=result.timed_out,
            trust=rule.pattern if rule else "",
            ms=int((time.monotonic() - started) * 1000),
        )
        return result

    def _own(self, path: Path) -> None:
        """chroot 后端会降权到 nobody，root 创建的文件要交出去。"""
        if self.backend.name != "chroot" or os.geteuid() != 0:
            return
        try:
            os.chown(path, self.settings.chroot_uid, self.settings.chroot_gid)
        except OSError:
            return

    def hand_over(self, session: "Session", *, limit: int = 4000) -> None:
        """把整个沙盒目录交给实际运行用户。

        root 建出来的目录是 root:root 0700，降权后的进程连 cd 都进不去，
        更别说在里面写文件。这里递归改属主，目录顶层的属主已经对了就跳过。
        """
        if self.backend.name != "chroot" or os.geteuid() != 0:
            return
        root_dir = Path(session.dir)
        if not root_dir.is_dir():
            return
        uid = self.settings.chroot_uid
        gid = self.settings.chroot_gid
        try:
            if root_dir.stat().st_uid == uid:
                return
        except OSError:
            return

        seen = 0
        for current, dirs, files in os.walk(root_dir):
            for name in [current] + [os.path.join(current, item) for item in dirs]:
                try:
                    os.chown(name, uid, gid)
                except OSError:
                    continue
            for name in files:
                try:
                    os.chown(os.path.join(current, name), uid, gid)
                except OSError:
                    continue
                seen += 1
                if seen >= limit:
                    return

    def _format(self, result: ExecResult) -> str:
        return result.to_text(self.settings.reply_max_chars)

    def sweep_quota(self) -> list[str]:
        """定时扫一遍，把超配额的沙盒找出来记日志。"""
        quota = self.settings.quota_bytes
        if quota <= 0:
            return []
        over: list[str] = []
        root = Path(self.sessions_root)
        if not root.is_dir():
            return []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            session = Session(root, child.name)
            try:
                usage = session.usage()
            except OSError:
                continue
            if usage.bytes > quota:
                over.append(child.name)
                if self.logbook is not None:
                    self.logbook.warn(
                        "quota_exceeded",
                        session=child.name,
                        used=human_bytes(usage.bytes),
                        quota=human_bytes(quota),
                    )
        return over

    def prune_backend(self) -> list[str]:
        """docker 后端闲置容器回收；其它后端什么也不做。"""
        prune = getattr(self.backend, "prune", None)
        if not callable(prune):
            return []
        try:
            return prune(self.settings.docker_idle_minutes * 60)
        except Exception:  # noqa: BLE001 - 回收失败不该影响主流程
            return []

    def _log(self, action: str, **fields) -> None:
        if self.audit is not None:
            self.audit.write(action, **fields)
        if self.logbook is not None:
            self.logbook.write("info", action, **fields)
