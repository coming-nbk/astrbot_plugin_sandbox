# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.backends import LocalBackend  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.logbook import LogBook  # noqa: E402
from sandboxkit.service import SandboxService  # noqa: E402
from sandboxkit.store import PermissionStore, ScopeStore, TrustStore  # noqa: E402
from sandboxkit.webapi import WebController  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(
        enabled=True,
        backend="local",
        allow_all=False,
        timeout_seconds=5,
        quota_bytes=1024 * 1024,
        import_max_bytes=64 * 1024,
        max_output_bytes=4096,
        reply_max_chars=2000,
        audit_enabled=False,
        max_procs=0,
        allow_local_backend=True,
        max_runs_per_minute=0,
    )
    base.update(overrides)
    return Settings(**base)


class TestWebController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.permissions = PermissionStore(self.base / "permissions.json")
        self.logbook = LogBook(self.base / "logs", retention_days=7)
        self.trust = TrustStore(self.base / "trust.json")
        self.scopes = ScopeStore(self.base / "scopes.json")
        self.service = SandboxService(
            _settings(),
            LocalBackend(),
            self.sessions,
            permissions=self.permissions,
            trust=self.trust,
            scopes=self.scopes,
            logbook=self.logbook,
        )
        self.controller = WebController(
            self.service,
            self.permissions,
            self.trust,
            self.scopes,
            base_dir=self.base,
            rootfs=self.base / "rootfs",
            audit_path=self.base / "audit.jsonl",
            logbook=self.logbook,
        )

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 概览 ----------

    def test_overview(self):
        data = self.controller.overview()
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "local")
        self.assertFalse(data["rootfs_ready"])
        self.assertEqual(data["session_count"], 0)

    def test_overview_counts_sessions(self):
        self.controller.save_file("u1", "a.txt", "hello")
        data = self.controller.overview()
        self.assertEqual(data["session_count"], 1)
        self.assertEqual(data["sessions"][0]["files"], 1)

    # ---------- 权限 ----------

    def test_save_and_list_user(self):
        self.assertTrue(self.controller.save_user({"user_id": "42", "note": "朋友"})["ok"])
        data = self.controller.list_users()
        self.assertEqual(len(data["users"]), 1)
        self.assertEqual(data["users"][0]["note"], "朋友")
        self.assertIn("allow_all", data["global_config"])

    def test_save_user_requires_id(self):
        result = self.controller.save_user({"note": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("user_id", result["error"])

    def test_delete_user(self):
        self.controller.save_user({"user_id": "42"})
        self.assertTrue(self.controller.delete_user("42")["removed"])
        self.assertEqual(self.controller.list_users()["users"], [])

    def test_permission_overrides_access(self):
        self.permissions.upsert({"user_id": "42", "enabled": True})
        allowed = self.service.handle(user_id="42", group_id="", sub="ls", arg="")
        self.assertNotIn("权限", allowed[0].text)
        denied = self.service.handle(user_id="43", group_id="", sub="ls", arg="")
        self.assertIn("权限", denied[0].text)

    def test_disabled_user_denied(self):
        self.permissions.upsert({"user_id": "42", "enabled": False})
        result = self.service.handle(user_id="42", group_id="", sub="ls", arg="")
        self.assertIn("关闭", result[0].text)

    def test_per_user_quota_applied(self):
        self.permissions.upsert({"user_id": "42", "quota_bytes": 1024 * 1024})
        view = self.service.with_settings(
            self.service.effective_settings("42")
        )
        self.assertEqual(view.settings.quota_bytes, 1024 * 1024)

    # ---------- 规则 ----------

    def test_trust_crud(self):
        saved = self.controller.save_trust({"pattern": "make *", "note": "构建"})
        self.assertTrue(saved["ok"])
        listed = self.controller.list_trust()
        self.assertEqual(listed["rules"][0]["pattern"], "make *")
        deleted = self.controller.delete_trust("make *")
        self.assertTrue(deleted["removed"])
        self.assertEqual(self.controller.list_trust()["rules"], [])

    def test_trust_requires_pattern(self):
        self.assertFalse(self.controller.save_trust({"note": "x"})["ok"])

    def test_scope_crud(self):
        saved = self.controller.save_scope(
            {"scope": "group", "target": "123", "allow": True}
        )
        self.assertTrue(saved["ok"])
        self.assertEqual(self.controller.list_scopes()["rules"][0]["key"], "group:123")
        self.assertTrue(self.controller.delete_scope("group:123")["removed"])

    def test_scope_invalid(self):
        self.assertFalse(self.controller.save_scope({"scope": "planet"})["ok"])

    def test_overview_counts_rules(self):
        self.controller.save_trust({"pattern": "x"})
        self.controller.save_scope({"scope": "all", "allow": True})
        data = self.controller.overview()
        self.assertEqual(data["trust_rules"], 1)
        self.assertEqual(data["scope_rules"], 1)

    # ---------- 文件 ----------

    def test_tree_and_preview(self):
        self.controller.save_file("u1", "notes/a.txt", "hello")
        tree = self.controller.tree("u1", ".", 2)
        self.assertTrue(tree["ok"])
        names = [row["name"] for row in tree["entries"]]
        self.assertIn("notes", names)
        data = self.controller.preview_file("u1", "notes/a.txt")
        self.assertTrue(data["ok"])
        self.assertEqual(data["kind"], "text")
        self.assertIn("hello", data["content"])

    def test_all_users_merges_permissions_and_sessions(self):
        self.permissions.upsert({"user_id": "42", "note": "朋友"})
        self.controller.save_file("u1", "a.txt", "x")  # 造一个没有权限记录的沙盒
        data = self.controller.all_users()
        self.assertTrue(data["ok"])
        ids = [row["id"] for row in data["users"]]
        self.assertIn("42", ids)
        self.assertTrue(any("（无权限记录）" in row["label"] for row in data["users"]))

    def test_tree_requires_user(self):
        self.assertFalse(self.controller.tree("", ".", 2)["ok"])

    def test_tree_escape(self):
        result = self.controller.tree("u1", "../..", 2)
        self.assertFalse(result["ok"])

    def test_raw_file_download(self):
        import base64

        self.controller.save_file("u1", "a.txt", "hello")
        data = self.controller.raw_file("u1", "a.txt")
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "a.txt")
        self.assertEqual(base64.b64decode(data["b64"]), b"hello")

    def test_raw_file_missing(self):
        self.assertFalse(self.controller.raw_file("u1", "nope.txt")["ok"])

    def test_raw_file_respects_limit(self):
        self.controller.save_file("u1", "big.bin", "x" * 100)
        self.controller.service.settings = self.controller.service.settings.__class__(
            **{**self.controller.service.settings.as_dict(), "import_max_bytes": 10}
        )
        self.assertFalse(self.controller.raw_file("u1", "big.bin")["ok"])

    def test_delete_file(self):
        self.controller.save_file("u1", "a.txt", "x")
        result = self.controller.delete_file("u1", "a.txt")
        self.assertTrue(result["ok"])
        self.assertFalse((self.sessions / self.controller.session_key("u1") / "a.txt").exists())

    def test_save_file_quota(self):
        self.controller.save_user({"user_id": "u1", "quota_bytes": 1024})
        result = self.controller.save_file("u1", "big.txt", "x" * 5000)
        self.assertFalse(result["ok"])

    # ---------- 执行 ----------

    def test_exec(self):
        result = self.controller.exec_command("u1", "echo hi")
        self.assertTrue(result["ok"])
        self.assertIn("hi", result["stdout"])
        self.assertEqual(result["exit_code"], 0)

    def test_logs_endpoint(self):
        self.controller.exec_command("u1", "echo hi")
        data = self.controller.logs(20)
        self.assertTrue(data["ok"])
        self.assertTrue(any("web_exec" in line for line in data["lines"]))

    def test_overview_reports_logs(self):
        self.controller.exec_command("u1", "echo hi")
        data = self.controller.overview()
        self.assertGreaterEqual(data["log_files"], 1)
        self.assertTrue(data["log_dir"].endswith("logs"))

    def test_cleanup_now(self):
        data = self.controller.cleanup_now()
        self.assertTrue(data["ok"])
        self.assertIn("removed", data)

    def test_exec_empty(self):
        self.assertFalse(self.controller.exec_command("u1", "   ")["ok"])

if __name__ == "__main__":
    unittest.main()

class TestBootstrapNotice(unittest.TestCase):
    """启动时自动下过 rootfs，界面要能看到这条记录。"""

    def test_overview_reads_last_bootstrap(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "last_bootstrap.json").write_text(
                json.dumps({"auto": True, "at": 1700000000, "preset": "ubuntu", "version": "24.04"}),
                encoding="utf-8",
            )
            from sandboxkit.webapi import _read_last_bootstrap

            data = _read_last_bootstrap(base)
            self.assertTrue(data["auto"])
            self.assertEqual(data["preset"], "ubuntu")

    def test_missing_file_is_none(self):
        import tempfile

        from sandboxkit.webapi import _read_last_bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_read_last_bootstrap(Path(tmp)))

class TestSecurityEndpoints(unittest.TestCase):
    """扫描结果要带「能不能修」，未知动作要明确拒绝。"""

    def setUp(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.logbook import LogBook
        from sandboxkit.service import SandboxService
        from sandboxkit.store import PermissionStore, ScopeStore, TrustStore
        from sandboxkit.webapi import WebController

        self.tmp = tempfile.TemporaryDirectory()
        base = _Path(self.tmp.name)
        sessions = base / "sessions"
        sessions.mkdir()
        settings = Settings(enabled=True, backend="chroot", allow_all=True, web_auth="none")
        service = SandboxService(settings, LocalBackend(), sessions)
        self.controller = WebController(
            service,
            PermissionStore(base / "permissions.json"),
            TrustStore(base / "trust.json"),
            ScopeStore(base / "scopes.json"),
            base_dir=base,
            rootfs=base / "rootfs",
            audit_path=base / "audit.jsonl",
            logbook=LogBook(base / "logs", retention_days=7),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_lists_items(self):
        data = self.controller.security_scan()
        self.assertTrue(data["ok"])
        self.assertIn("host", data)
        self.assertIn("kernel", data["host"])
        for item in data["items"]:
            self.assertIn("fixable", item)
            self.assertIn("severity", item)

    def test_unknown_fix_action_rejected(self):
        data = self.controller.security_fix("rm-rf")
        self.assertFalse(data["ok"])
        self.assertIn("不认识", data["error"])

    def test_empty_action_rejected(self):
        data = self.controller.security_fix("")
        self.assertFalse(data["ok"])

class TestSecuritySummary(unittest.TestCase):
    """AI 摘要没开就明确拒绝，开了就走缓存。"""

    def setUp(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.logbook import LogBook
        from sandboxkit.service import SandboxService
        from sandboxkit.store import PermissionStore, ScopeStore, TrustStore
        from sandboxkit.webapi import WebController

        self.tmp = tempfile.TemporaryDirectory()
        base = _Path(self.tmp.name)
        (base / "sessions").mkdir()
        self.settings = Settings(enabled=True, backend="chroot", allow_all=True)
        service = SandboxService(self.settings, LocalBackend(), base / "sessions")
        self.controller = WebController(
            service,
            PermissionStore(base / "p.json"),
            TrustStore(base / "t.json"),
            ScopeStore(base / "s.json"),
            base_dir=base,
            rootfs=base / "rootfs",
            audit_path=base / "a.jsonl",
            logbook=LogBook(base / "logs", retention_days=7),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_reports(self):
        data = self.controller.security_summary("CVE-1", "some text")
        self.assertFalse(data["ok"])
        self.assertIn("没有打开", data["error"])

    def test_enabled_uses_cache(self):
        from unittest import mock

        self.controller.service.settings = type(self.settings)(
            enabled=True, backend="chroot", allow_all=True,
            ai_summary_enabled=True, ai_summary_url="https://x/v1",
        )
        with mock.patch(
            "sandboxkit.ai_summary.summarize", return_value="中文摘要"
        ) as fake:
            first = self.controller.security_summary("CVE-1", "text")
            second = self.controller.security_summary("CVE-1", "text")
        self.assertTrue(first["ok"])
        self.assertEqual(first["summary"], "中文摘要")
        self.assertTrue(second["cached"])
        self.assertEqual(fake.call_count, 1)

class TestSecurityDismiss(unittest.TestCase):
    """标记已处理后，条目不再出现在扫描结果里。"""

    def setUp(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.logbook import LogBook
        from sandboxkit.service import SandboxService
        from sandboxkit.store import PermissionStore, ScopeStore, TrustStore
        from sandboxkit.webapi import WebController

        self.tmp = tempfile.TemporaryDirectory()
        base = _Path(self.tmp.name)
        (base / "sessions").mkdir()
        service = SandboxService(
            Settings(enabled=True, backend="local", allow_all=True),
            LocalBackend(),
            base / "sessions",
        )
        self.controller = WebController(
            service,
            PermissionStore(base / "p.json"),
            TrustStore(base / "t.json"),
            ScopeStore(base / "s.json"),
            base_dir=base,
            rootfs=base / "rootfs",
            audit_path=base / "a.jsonl",
            logbook=LogBook(base / "logs", retention_days=7),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_dismiss_filters_item(self):
        first = self.controller.security_scan()
        self.assertTrue(first["items"])
        target = first["items"][0]["id"]

        result = self.controller.security_dismiss(target)
        self.assertTrue(result["ok"])

        second = self.controller.security_scan()
        self.assertNotIn(target, [item["id"] for item in second["items"]])

    def test_empty_id_rejected(self):
        result = self.controller.security_dismiss("")
        self.assertFalse(result["ok"])

