# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.backends import LocalBackend, select_backend  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.service import FileOut, SandboxService, TextOut  # noqa: E402
from sandboxkit.store import PermissionStore, ScopeStore, TrustStore  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(
        enabled=True,
        backend="local",
        allow_all=True,
        timeout_seconds=5,
        quota_bytes=1024 * 1024,
        import_max_bytes=1000,
        max_output_bytes=4096,
        reply_max_chars=2000,
        audit_enabled=False,
        max_procs=0,
        allow_local_backend=True,
        max_runs_per_minute=0,
    )
    base.update(overrides)
    return Settings(**base)


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.host_dir = self.base / "host"
        self.host_dir.mkdir()
        (self.host_dir / "note.txt").write_text("宿主上的内容")
        self.settings = _settings(import_roots=(str(self.host_dir),))
        self.service = SandboxService(
            self.settings, LocalBackend(), self.sessions
        )

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, sub, arg="", user="u1", group=""):
        return self.service.handle(user_id=user, group_id=group, sub=sub, arg=arg)

    def text(self, outputs) -> str:
        return "\n".join(o.text for o in outputs if isinstance(o, TextOut))

    def test_help(self):
        self.assertIn("run <命令>", self.text(self.call("help")))

    def test_denied_user(self):
        settings = _settings(allow_all=False)
        service = SandboxService(settings, LocalBackend(), self.sessions)
        outputs = service.handle(user_id="x", group_id="", sub="run", arg="echo 1")
        self.assertIn("权限", self.text(outputs))

    def test_run_echo(self):
        out = self.text(self.call("run", "echo hi"))
        self.assertIn("hi", out)
        self.assertIn("exit=0", out)

    def test_run_without_arg_shows_usage(self):
        self.assertIn("用法", self.text(self.call("run")))

    def test_python_code(self):
        out = self.text(self.call("py", "print(6*7)"))
        if "exit=127" in out:
            self.skipTest("沙盒里没有 python3")
        self.assertIn("42", out)

    def test_import_then_cat(self):
        source = str(self.host_dir / "note.txt")
        out = self.text(self.call("import", source))
        self.assertIn("imports/note.txt", out)
        body = self.text(self.call("cat", "imports/note.txt"))
        self.assertIn("宿主上的内容", body)

    def test_import_denied_outside_roots(self):
        outside = self.base / "outside.txt"
        outside.write_text("x")
        out = self.text(self.call("import", str(outside)))
        self.assertIn("失败", out)

    def test_import_with_alias(self):
        source = str(self.host_dir / "note.txt")
        out = self.text(self.call("import", f"{source} as renamed.txt"))
        self.assertIn("renamed.txt", out)

    def test_export_returns_file(self):
        source = str(self.host_dir / "note.txt")
        self.call("import", source)
        outputs = self.call("export", "imports/note.txt")
        files = [o for o in outputs if isinstance(o, FileOut)]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].path.is_file())

    def test_export_missing(self):
        out = self.text(self.call("export", "imports/none.txt"))
        self.assertIn("普通文件", out)

    def test_ls(self):
        self.call("run", "echo x > a.txt")
        out = self.text(self.call("ls"))
        self.assertIn("a.txt", out)

    def test_rm(self):
        self.call("run", "echo x > a.txt")
        out = self.text(self.call("rm", "a.txt"))
        self.assertIn("已删除", out)

    def test_cat_escape_rejected(self):
        out = self.text(self.call("cat", "../../etc/passwd"))
        self.assertIn("失败", out)

    def test_usage(self):
        out = self.text(self.call("usage"))
        self.assertIn("配额", out)

    def test_backend_info(self):
        out = self.text(self.call("backend"))
        self.assertIn("local", out)

    def test_reset_clears(self):
        self.call("run", "echo x > a.txt")
        self.call("reset")
        self.assertNotIn("a.txt", self.text(self.call("ls")))

    def test_unknown_subcommand(self):
        self.assertIn("未知子命令", self.text(self.call("wat")))

    def test_bootstrap_is_admin_only(self):
        out = self.text(self.call("bootstrap"))
        self.assertIn("管理员", out)

    def test_sessions_are_isolated(self):
        self.call("run", "echo secret > a.txt", user="u1")
        other = self.text(self.call("ls", user="u2"))
        self.assertNotIn("a.txt", other)

    def test_quota_enforced_on_import(self):
        big = self.host_dir / "big.bin"
        big.write_bytes(b"0" * 2000)
        settings = _settings(
            import_roots=(str(self.host_dir),),
            import_max_bytes=10 * 1024,
            quota_bytes=1024,
        )
        service = SandboxService(settings, LocalBackend(), self.sessions)
        out = service.handle(
            user_id="q", group_id="", sub="import", arg=str(big)
        )
        self.assertIn("配额", "\n".join(o.text for o in out))

    def test_timeout_reported(self):
        out = self.text(self.call("run", "sleep 10"))
        self.assertIn("超时", out)


class TestCapabilityAndTrust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.permissions = PermissionStore(self.base / "permissions.json")
        self.trust = TrustStore(self.base / "trust.json")
        self.scopes = ScopeStore(self.base / "scopes.json")

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, **overrides):
        settings = _settings(**overrides)
        return SandboxService(
            settings,
            LocalBackend(),
            self.sessions,
            permissions=self.permissions,
            trust=self.trust,
            scopes=self.scopes,
        )

    def run_cmd(self, service, sub, arg, user="u1", group=""):
        return service.handle(user_id=user, group_id=group, sub=sub, arg=arg)

    def test_allow_exec_false_blocks_run(self):
        self.permissions.upsert({"user_id": "u1", "allow_exec": False})
        out = self.run_cmd(self.service(), "run", "echo hi")
        self.assertIn("执行命令的权限", out[0].text)

    def test_allow_py_false_blocks_python(self):
        self.permissions.upsert({"user_id": "u1", "allow_py": False})
        out = self.run_cmd(self.service(), "py", "print(1)")
        self.assertIn("Python", out[0].text)

    def test_allow_reset_false_blocks_reset(self):
        self.permissions.upsert({"user_id": "u1", "allow_reset": False})
        out = self.run_cmd(self.service(), "reset", "")
        self.assertIn("重置", out[0].text)

    def test_allow_export_false_blocks_export(self):
        self.permissions.upsert({"user_id": "u1", "allow_export": False})
        out = self.run_cmd(self.service(), "export", "imports/a.txt")
        self.assertIn("导出", out[0].text)

    def test_trusted_command_gets_longer_timeout(self):
        self.trust.upsert({"pattern": "sleep *", "timeout_seconds": 10})
        service = self.service(timeout_seconds=1)
        out = self.run_cmd(service, "run", "sleep 2")
        self.assertNotIn("超时", out[0].text)
        self.assertIn("exit=0", out[0].text)

    def test_untrusted_command_hits_timeout(self):
        service = self.service(timeout_seconds=1)
        out = self.run_cmd(service, "run", "sleep 2")
        self.assertIn("超时", out[0].text)

    def test_scope_rule_allows_group(self):
        self.scopes.upsert({"scope": "group", "target": "123", "allow": True})
        service = self.service(allow_all=False)
        allowed = self.run_cmd(service, "ls", "", group="123")
        denied = self.run_cmd(service, "ls", "", group="456")
        self.assertNotIn("权限", allowed[0].text)
        self.assertIn("权限", denied[0].text)

    def test_scope_rule_can_limit_users(self):
        self.scopes.upsert(
            {"scope": "group", "target": "123", "allow": True, "users": "u1"}
        )
        service = self.service(allow_all=False)
        self.assertNotIn("权限", self.run_cmd(service, "ls", "", group="123")[0].text)
        self.assertIn(
            "权限", self.run_cmd(service, "ls", "", user="u2", group="123")[0].text
        )

    def test_scope_deny_wins(self):
        self.scopes.upsert({"scope": "group", "target": "123", "allow": False})
        service = self.service(allow_all=True)
        out = self.run_cmd(service, "ls", "", group="123")
        self.assertIn("禁止", out[0].text)

    def test_user_permission_beats_scope(self):
        self.scopes.upsert({"scope": "group", "target": "123", "allow": False})
        self.permissions.upsert({"user_id": "u1", "enabled": True})
        service = self.service(allow_all=False)
        out = self.run_cmd(service, "ls", "", group="123")
        self.assertNotIn("禁止", out[0].text)


class TestRootHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_backend_never_hardens(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        self.assertFalse(service.limits().harden)

    def test_root_chroot_enables_hardening(self):
        from sandboxkit.backends import ChrootBackend

        settings = _settings(chroot_uid=0, root_hardening=True)
        service = SandboxService(settings, ChrootBackend(), self.sessions)
        self.assertTrue(service.limits().harden)

    def test_nobody_chroot_does_not_need_hardening(self):
        from sandboxkit.backends import ChrootBackend

        settings = _settings(chroot_uid=65534, root_hardening=True)
        service = SandboxService(settings, ChrootBackend(), self.sessions)
        self.assertFalse(service.limits().harden)

    def test_can_disable_hardening(self):
        from sandboxkit.backends import ChrootBackend

        settings = _settings(chroot_uid=0, root_hardening=False)
        service = SandboxService(settings, ChrootBackend(), self.sessions)
        self.assertFalse(service.limits().harden)


class TestPackageInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_bad_names(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        out = service.install_packages(["python3; rm -rf /"])
        self.assertIn("不合法", out)

    def test_rejects_too_many(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        out = service.install_packages([f"pkg{i}" for i in range(20)])
        self.assertIn("最多", out)

    def test_empty(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        self.assertIn("用法", service.install_packages([]))

    def test_docker_backend_hint(self):
        from sandboxkit.backends import DockerBackend

        service = SandboxService(_settings(), DockerBackend(), self.sessions)
        out = service.install_packages(["git"])
        self.assertIn("镜像", out)

    def test_admin_only(self):
        settings = _settings(allow_all=False)
        service = SandboxService(settings, LocalBackend(), self.sessions)
        out = service.handle(user_id="u1", group_id="", sub="install", arg="git")
        self.assertIn("权限", out[0].text)


class TestSelectBackend(unittest.TestCase):
    def test_unknown_configured_backend_falls_back(self):
        settings = _settings(backend="nope")
        backend, notes = select_backend(settings, None)
        self.assertEqual(backend.name, "local")
        self.assertTrue(notes)

    def test_explicit_local(self):
        backend, _notes = select_backend(_settings(backend="local"), None)
        self.assertEqual(backend.name, "local")

    def test_auto_without_rootfs_falls_back_to_local(self):
        backend, notes = select_backend(_settings(backend="auto"), None)
        self.assertEqual(backend.name, "local")
        self.assertTrue(any("local" in n for n in notes))


if __name__ == "__main__":
    unittest.main()

class TestHandOver(unittest.TestCase):
    """chroot 后端降权到 nobody，root 建的目录必须交出去。

    注意：这个沙盒里 chown 是空操作（proot 模拟），所以用 mock 验证
    「该改的都改了」，而不是断言真实属主变化。
    """

    def _service(self, root, uid=65534, gid=65534):
        from types import SimpleNamespace

        service = SandboxService(
            _settings(chroot_uid=uid, chroot_gid=gid),
            LocalBackend(),
            root,
        )
        service.backend = SimpleNamespace(name="chroot")
        return service

    def test_hand_over_walks_and_chowns(self):
        from unittest import mock

        from sandboxkit.session import Session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            session = Session(root, "u1")
            session.ensure()
            (session.dir / "a.txt").write_text("x", encoding="utf-8")

            service = self._service(root)
            with mock.patch("sandboxkit.service.os.chown") as chown:
                service.hand_over(session)

            targets = {str(call.args[0]) for call in chown.call_args_list}
            self.assertIn(str(session.dir), targets)
            self.assertIn(str(session.dir / "a.txt"), targets)
            self.assertIn(str(session.dir / "tmp"), targets)
            for call in chown.call_args_list:
                self.assertEqual(call.args[1:], (65534, 65534))

    def test_hand_over_skips_when_already_owned(self):
        import os
        from unittest import mock

        from sandboxkit.session import Session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            session = Session(root, "u2")
            session.ensure()

            # 目标 uid 就是当前 uid，说明已经交接过，不该再遍历
            service = self._service(root, uid=os.geteuid(), gid=os.getegid())
            with mock.patch("sandboxkit.service.os.chown") as chown:
                service.hand_over(session)
            chown.assert_not_called()

    def test_skips_when_backend_is_not_chroot(self):
        from types import SimpleNamespace
        from unittest import mock

        from sandboxkit.session import Session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            session = Session(root, "u3")
            session.ensure()
            service = SandboxService(_settings(), LocalBackend(), root)
            service.backend = SimpleNamespace(name="local")
            with mock.patch("sandboxkit.service.os.chown") as chown:
                service.hand_over(session)
            chown.assert_not_called()

class TestCommandGuards(unittest.TestCase):
    """超长命令和 NUL 必须在进沙盒之前就拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.service = SandboxService(_settings(), LocalBackend(), self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_command_rejected(self):
        outs = self.service.handle(
            user_id="u1", group_id=None, sub="run", arg="echo " + "a" * 9000
        )
        self.assertIn("命令太长", outs[0].text)

    def test_nul_rejected(self):
        outs = self.service.handle(
            user_id="u1", group_id=None, sub="run", arg="echo a\x00b"
        )
        self.assertIn("非法字符", outs[0].text)

class TestFixTasks(unittest.TestCase):
    """修复要能后台跑 + 轮询进度。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.service = SandboxService(_settings(), LocalBackend(), self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_fix_returns_task_id(self):
        result = self.service.start_fix("suid")
        self.assertTrue(result["ok"])
        self.assertTrue(result["task_id"].startswith("suid-"))

        state = self.service.task_state(result["task_id"])
        self.assertTrue(state["ok"])
        self.assertIn("lines", state["task"])

    def test_unknown_action_rejected(self):
        result = self.service.start_fix("rm-rf")
        self.assertFalse(result["ok"])
        self.assertIn("不认识", result["error"])

    def test_missing_task_reports(self):
        state = self.service.task_state("nope-1234")
        self.assertFalse(state["ok"])
        self.assertIn("不存在", state["error"])

class TestCommandGuardsExtra(unittest.TestCase):
    """禁用片段、长度上限、root 沙盒闸门。"""

    def _service(self, **overrides):
        base = dict(enabled=True, backend="local", allow_all=True,
                    allow_local_backend=True)
        base.update(overrides)
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "sessions"
        root.mkdir()
        service = SandboxService(_settings(**base), LocalBackend(), root)
        service._tmp = tmp
        return service

    def test_deny_pattern_blocks(self):
        service = self._service(deny_patterns=("rm -rf /",))
        outs = service.handle(
            user_id="u1", group_id=None, sub="run", arg="rm -rf / --no-preserve-root"
        )
        self.assertIn("禁用规则", outs[0].text)
        service._tmp.cleanup()

    def test_deny_pattern_is_case_insensitive(self):
        service = self._service(deny_patterns=("mkfs",))
        outs = service.handle(user_id="u1", group_id=None, sub="run", arg="MKFS.ext4 /dev/sda")
        self.assertIn("禁用规则", outs[0].text)
        service._tmp.cleanup()

    def test_command_length_uses_setting(self):
        service = self._service(max_command_chars=64)
        outs = service.handle(user_id="u1", group_id=None, sub="run", arg="echo " + "a" * 100)
        self.assertIn("超过 64 字符", outs[0].text)
        service._tmp.cleanup()

    def test_root_sandbox_needs_flag(self):
        from types import SimpleNamespace

        service = self._service(chroot_uid=0, chroot_gid=0)
        service.backend = SimpleNamespace(name="chroot")
        allowed, reason = service.can_execute()
        self.assertFalse(allowed)
        self.assertIn("allow_root_sandbox", reason)
        service._tmp.cleanup()

    def test_root_sandbox_allowed_when_flagged(self):
        from types import SimpleNamespace

        service = self._service(chroot_uid=0, chroot_gid=0, allow_root_sandbox=True)
        service.backend = SimpleNamespace(name="chroot")
        allowed, _ = service.can_execute()
        self.assertTrue(allowed)
        service._tmp.cleanup()

