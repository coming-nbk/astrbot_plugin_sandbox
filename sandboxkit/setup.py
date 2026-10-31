# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebUI 里的部署向导。

三件事，全部由页面触发、结果回页面：
1. 环境自检：容器/后端/rootfs/目录权限/限额，一次性列出来
2. 自检用例：在临时沙盒里真跑一遍关键链路（越界、超时、执行、看门狗）
3. 一键授权：给指定用户写一条 permissions 记录，不碰 AstrBot 的配置

长任务（下载 rootfs）走后台线程 + 任务 id 轮询，避免 HTTP 请求挂住。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backends import rootfs_ready
from .bootstrap import (
    BootstrapError,
    bootstrap_with_fallback,
    detect_arch,
    read_marker,
)
from .config import Settings
from .env import advice, detect
from .health import HealthGuard
from .limits import Limits
from .paths import PathRejected, resolve_sandbox_path
from .mirrors import probe as probe_url, resolve_all
from .profiles import PROFILES, get_profile
from .service import SandboxService
from .session import SandboxError, Session
from .store import PermissionStore


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    ms: int = 0
    skipped: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: str
    name: str
    started: float
    lines: list[str] = field(default_factory=list)
    done: bool = False
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "started": self.started,
            "lines": self.lines[-200:],
            "done": self.done,
            "ok": self.ok,
            "error": self.error,
            "elapsed": round(time.time() - self.started, 1),
        }


class SetupWizard:
    def __init__(
        self,
        service: SandboxService,
        permissions: PermissionStore,
        *,
        base_dir: Path,
        rootfs: Path,
        health: HealthGuard | None = None,
        worker=None,
        log: Callable[[str, str], None] | None = None,
        preferred_getter: Callable[[], str] | None = None,
        preferred_setter: Callable[[str], None] | None = None,
    ):
        self.service = service
        self.permissions = permissions
        self.base_dir = Path(base_dir)
        self.rootfs = Path(rootfs)
        self.health = health
        self.worker = worker
        self._log = log or (lambda level, message: None)
        self._get_preferred = preferred_getter or (lambda: "")
        self._set_preferred = preferred_setter or (lambda _name: None)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    # ---------- 环境自检 ----------

    def probe(self) -> dict:
        settings = self.service.settings
        runtime = detect()
        marker = read_marker(self.rootfs)
        return {
            "ok": True,
            "runtime": runtime.to_dict(),
            "advice": advice(runtime),
            "backend": self.service.backend.name,
            "backend_desc": self.service.backend.describe(),
            "rootfs": {
                "ready": rootfs_ready(self.rootfs),
                "path": str(self.rootfs),
                "preset": marker.preset if marker else settings.rootfs_preset,
                "version": marker.version if marker else "",
                "arch": marker.arch if marker else "",
                "installed_at": marker.installed_at if marker else "",
            },
            "worker": self.worker.stats() if self.worker else {},
            "health": self.health.status().to_dict() if self.health else {},
            "profiles": [item.to_dict() for item in PROFILES.values()],
            "profile": settings.rootfs_profile,
            "preferred": self._get_preferred(),
            "settings": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "allow_local_backend": settings.allow_local_backend,
                "allow_all": settings.allow_all,
                "allow_network": settings.allow_network,
                "quota_bytes": settings.quota_bytes,
                "timeout_seconds": settings.timeout_seconds,
                "memory_mb": settings.memory_mb,
                "max_cores": settings.max_cores,
                "worker_isolation": settings.worker_isolation,
            },
            "paths": {
                "base": str(self.base_dir),
                "sessions": str(self.service.sessions_root),
                "logs": str(self.base_dir / "logs"),
                "audit": str(self.base_dir / "audit.jsonl"),
                "disabled_file": str(self.base_dir / "DISABLED"),
            },
            "counts": {
                "users": len(self.permissions.all()),
                "sessions": self._count_sessions(),
            },
        }

    def _count_sessions(self) -> int:
        root = Path(self.service.sessions_root)
        if not root.is_dir():
            return 0
        return sum(1 for child in root.iterdir() if child.is_dir())

    # ---------- 自检用例 ----------

    def self_test(self, *, deep: bool = False) -> dict:
        checks: list[Check] = []
        # chroot 后端只能看到 rootfs 内部，所以自检目录要放在会话根下
        workspace = Path(self.service.sessions_root)
        session = Session(workspace, f"probe-{uuid.uuid4().hex[:6]}")

        checks.append(self._timed("沙盒目录可写", lambda: self._check_writable(session)))
        checks.append(self._timed("路径越界被拒", self._check_traversal))
        checks.append(self._timed("宿主导入白名单", self._check_import_roots))
        checks.append(self._timed("执行链路", lambda: self._check_exec(session)))
        checks.append(self._timed("超时终止", lambda: self._check_timeout(session)))
        checks.append(self._timed("内存看门狗", lambda: self._check_memory(session, deep)))
        if deep:
            checks.append(self._timed("CPU 看门狗", lambda: self._check_cpu(session)))
            checks.append(self._timed("docker 复用", self._check_docker))
        checks.append(self._timed("后端隔离", self._check_backend))
        checks.append(self._timed("rootfs", self._check_rootfs))
        checks.append(self._timed("/dev 设备", self._check_dev))

        for child in sorted(workspace.glob("*")):
            try:
                if child.is_dir():
                    import shutil

                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

        failed = [check for check in checks if not check.ok and not check.skipped]
        self._log("info", f"自检完成：{len(checks) - len(failed)}/{len(checks)} 通过")
        return {
            "ok": not failed,
            "checks": [check.to_dict() for check in checks],
            "passed": len(checks) - len(failed),
            "total": len(checks),
        }

    def _timed(self, name: str, func) -> Check:
        started = time.monotonic()
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001 - 自检要兜住一切
            return Check(name, False, f"异常：{exc}", int((time.monotonic() - started) * 1000))
        if isinstance(result, Check):
            result.ms = int((time.monotonic() - started) * 1000)
            return result
        ok, detail = result if isinstance(result, tuple) else (bool(result), "")
        return Check(name, ok, detail, int((time.monotonic() - started) * 1000))

    def _check_writable(self, session: Session):
        session.ensure()
        probe = session.dir / ".write-test"
        probe.write_text("ok", "utf-8")
        content = probe.read_text("utf-8")
        probe.unlink()
        mode = oct(session.dir.stat().st_mode & 0o777)
        return content == "ok", f"目录 {mode}"

    def _check_traversal(self):
        try:
            resolve_sandbox_path("../../etc/passwd", self.base_dir)
        except PathRejected as exc:
            return True, f"已拒绝：{exc}"
        return False, "越界路径没有被拒绝"

    def _check_import_roots(self):
        roots = self.service.settings.import_roots
        if not roots:
            return Check("宿主导入白名单", True, "未配置，导入功能关闭", skipped=True)
        missing = [root for root in roots if not Path(root).exists()]
        if missing:
            return False, f"配置的目录不存在：{', '.join(missing)}"
        return True, f"{len(roots)} 个目录"

    def _check_exec(self, session: Session):
        allowed, reason = self.service.can_execute()
        if not allowed:
            return Check("执行链路", False, reason)
        result = self.service.execute(
            session, ["/bin/sh", "-c", "echo selftest"], user="selftest", action="selftest"
        )
        if result.ok and "selftest" in result.stdout:
            return True, f"backend={self.service.backend.name} exit=0"
        return False, (result.error or result.stderr or "没有输出")[:200]

    def _check_timeout(self, session: Session):
        limits = Limits(timeout=0.5, memory_mb=0)
        started = time.monotonic()
        result = self.service._run_prepared(  # noqa: SLF001 - 自检就是要打到底层
            self.service.prepare(session, ["/bin/sh", "-c", "sleep 5"]), limits, {}
        )
        elapsed = time.monotonic() - started
        if result.timed_out and elapsed < 5:
            return True, f"{elapsed:.1f}s 内终止"
        return False, f"timed_out={result.timed_out} elapsed={elapsed:.1f}s"

    def _check_memory(self, session: Session, deep: bool):
        if self.service.backend.name != "local":
            return Check("内存看门狗", True, "非 local 后端，跳过", skipped=True)
        code = "x = bytearray(96 * 1024 * 1024); import time; time.sleep(3)"
        limits = Limits(
            timeout=10,
            memory_mb=0,
            memory_watchdog_mb=32,
            watchdog_interval=0.05,
        )
        result = self.service._run_prepared(  # noqa: SLF001
            self.service.prepare(session, ["python3", "-c", code]), limits, {}
        )
        if result.memory_killed:
            return True, "进程组超限后被杀"
        if result.error:
            return Check("内存看门狗", True, f"跳过：{result.error[:80]}", skipped=True)
        return False, "内存超限没有被终止"

    def _check_cpu(self, session: Session):
        limits = Limits(
            timeout=10,
            memory_mb=0,
            cpu_percent_limit=60,
            cpu_burst_seconds=1,
            watchdog_interval=0.05,
        )
        result = self.service._run_prepared(  # noqa: SLF001
            self.service.prepare(session, ["/bin/sh", "-c", "while :; do :; done"]),
            limits,
            {},
        )
        if result.cpu_killed:
            return True, "死循环被终止"
        return False, "死循环没有被终止"

    def _check_docker(self):
        from .backends import DockerBackend

        backend = DockerBackend()
        ok, why = backend.available(None)
        if not ok:
            return Check("docker 复用", True, f"跳过：{why}", skipped=True)
        return True, "docker CLI 与 socket 可用"

    def _check_backend(self):
        name = self.service.backend.name
        if name == "local" and not self.service.settings.allow_local_backend:
            return False, "当前是 local 后端，命令会被拒绝（allow_local_backend=false）"
        if name == "local":
            return True, "local（无文件系统隔离）"
        return True, name

    def _check_rootfs(self):
        name = self.service.backend.name
        if name == "docker":
            return Check("rootfs", True, "docker 后端用镜像，不需要 rootfs", skipped=True)
        if name == "local":
            return Check("rootfs", True, "local 后端用宿主文件系统，不需要 rootfs", skipped=True)
        if rootfs_ready(self.rootfs):
            return True, str(self.rootfs)
        return False, "rootfs 未就绪，命令可能跑不起来"

    def check_sources(self, per_preset: int = 2) -> dict:
        """爬一遍索引并逐个探测。

        按发行版各取前 N 个 —— 只截前 6 条的话，Alpine 有 8 个候选就把
        Ubuntu / Debian 全挤掉了，看起来像「只有 alpine」。
        """
        arch = detect_arch()
        items = resolve_all(arch)
        buckets: dict[str, list[str]] = {}
        for preset, url in items:
            buckets.setdefault(preset, []).append(url)

        picked: list[tuple[str, str]] = []
        for preset, urls in buckets.items():
            for url in urls[: max(1, int(per_preset))]:
                picked.append((preset, url))

        results = []
        for preset, url in picked:
            result = probe_url(url)
            result["preset"] = preset
            results.append(result)

        return {
            "ok": True,
            "arch": arch,
            "found": len(items),
            "checked": len(results),
            "presets": {name: len(urls) for name, urls in buckets.items()},
            "sources": results,
        }

    def _check_dev(self):
        if self.service.backend.name != "chroot":
            return Check("/dev 设备", True, "非 chroot 后端，跳过", skipped=True)
        null = self.rootfs / "dev" / "null"
        try:
            if null.exists() or null.is_symlink():
                return True, "/dev/null 就绪"
        except OSError:
            pass
        return False, (
            "rootfs 里没有 /dev/null（需要 CAP_MKNOD 才能创建）。"
            "重新下载 rootfs 会自动补；也可以手动：mknod "
            f"{null} c 1 3 && chmod 666 {null}"
        )

    # ---------- 长任务：下载 rootfs ----------

    def start_rootfs(self, profile: str = "") -> dict:
        chosen = get_profile(profile) if profile else None
        name = f"rootfs:{chosen.name}" if chosen else "rootfs"
        task = Task(id=uuid.uuid4().hex[:8], name=name, started=time.time())
        with self._lock:
            self._tasks[task.id] = task

        def _run() -> None:
            try:
                settings = self.service.settings
                if chosen is not None:
                    preset, image, packages = (
                        chosen.preset,
                        chosen.image,
                        chosen.packages,
                    )
                    task.lines.append(
                        f"套餐 {chosen.name}：{chosen.description}"
                    )
                else:
                    preset, image, packages = (
                        settings.rootfs_preset,
                        settings.rootfs_image,
                        settings.bootstrap_packages,
                    )
                info = bootstrap_with_fallback(
                    self.rootfs,
                    preset=preset,
                    version=settings.rootfs_version,
                    arch=detect_arch(),
                    url=settings.rootfs_url,
                    image=image,
                    expected_sha256=settings.rootfs_sha256,
                    packages=packages,
                    force=True,
                    log=lambda message: task.lines.append(str(message)),
                )
                task.lines.append(f"完成：{info.preset} {info.version} {info.arch}")
                if chosen is not None:
                    self._set_preferred(chosen.name)
                    task.lines.append(f"已把 {chosen.name} 记为默认套餐，下次自动下载用它")
                task.ok = True
            except BootstrapError as exc:
                task.error = str(exc)
                task.lines.append(f"失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                task.error = str(exc)
                task.lines.append(f"异常：{exc}")
            finally:
                task.done = True
                self._log("info" if task.ok else "error", f"rootfs 任务结束：{task.error or 'ok'}")

        threading.Thread(target=_run, name="sandbox-rootfs", daemon=True).start()
        return {"ok": True, "task": task.to_dict()}

    def task(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(str(task_id))
        if task is None:
            return {"ok": False, "error": "任务不存在或已过期"}
        return {"ok": True, "task": task.to_dict()}

    # ---------- 一键授权 ----------

    def apply_defaults(self, user_id: str, *, quota_mb: int = 0) -> dict:
        user_id = str(user_id or "").strip()
        if not user_id:
            return {"ok": False, "error": "请填要授权的用户 ID"}
        try:
            perm = self.permissions.upsert(
                {
                    "user_id": user_id,
                    "enabled": True,
                    "note": "由部署向导授权",
                    "quota_bytes": max(0, int(quota_mb)) * 1024 * 1024,
                    "allow_exec": True,
                    "allow_py": True,
                    "allow_export": True,
                    "allow_reset": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        self._log("info", f"部署向导授权 {user_id}")
        return {
            "ok": True,
            "user": perm.to_dict(),
            "hint": "现在可以在聊天里发 /sbx run echo ok",
        }

    def summary(self) -> dict:
        return {
            "ok": True,
            "backend": self.service.backend.name,
            "rootfs_ready": rootfs_ready(self.rootfs),
            "users": len(self.permissions.all()),
            "disabled": (self.base_dir / "DISABLED").exists(),
            "health": self.health.status().to_dict() if self.health else {},
        }
