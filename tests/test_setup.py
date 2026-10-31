# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.backends import LocalBackend  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.health import HealthGuard  # noqa: E402
from sandboxkit.service import SandboxService  # noqa: E402
from sandboxkit.setup import SetupWizard  # noqa: E402
from sandboxkit.store import PermissionStore  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(
        backend="local",
        allow_local_backend=True,
        allow_all=False,
        timeout_seconds=5,
        memory_mb=0,
        quota_bytes=1024 * 1024,
        max_runs_per_minute=0,
        audit_enabled=False,
        worker_isolation=False,
    )
    base.update(overrides)
    return Settings(**base)


class TestSetupWizard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.permissions = PermissionStore(self.base / "permissions.json")
        self.service = SandboxService(
            _settings(), LocalBackend(), self.sessions, permissions=self.permissions
        )
        self.wizard = SetupWizard(
            self.service,
            self.permissions,
            base_dir=self.base,
            rootfs=self.base / "rootfs",
            health=HealthGuard(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_shape(self):
        data = self.wizard.probe()
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "local")
        self.assertFalse(data["rootfs"]["ready"])
        self.assertIn("runtime", data)
        self.assertIn("paths", data)
        self.assertIn("counts", data)
        self.assertEqual(data["counts"]["users"], 0)

    def test_self_test_reports_checks(self):
        data = self.wizard.self_test()
        self.assertIn("checks", data)
        self.assertGreaterEqual(data["total"], 6)
        names = [check["name"] for check in data["checks"]]
        self.assertIn("执行链路", names)
        self.assertIn("路径越界被拒", names)
        # 执行链路在 local + allow_local_backend 下必须通过
        exec_check = [c for c in data["checks"] if c["name"] == "执行链路"][0]
        self.assertTrue(exec_check["ok"], exec_check["detail"])

    def test_self_test_cleans_workspace(self):
        self.wizard.self_test()
        leftovers = list((self.base / "_selftest").glob("*"))
        self.assertEqual(leftovers, [])

    def test_self_test_fails_when_local_not_allowed(self):
        settings = _settings(allow_local_backend=False)
        service = SandboxService(settings, LocalBackend(), self.sessions)
        wizard = SetupWizard(service, self.permissions, base_dir=self.base, rootfs=self.base / "rootfs")
        data = wizard.self_test()
        exec_check = [c for c in data["checks"] if c["name"] == "执行链路"][0]
        self.assertFalse(exec_check["ok"])
        self.assertFalse(data["ok"])

    def test_apply_defaults_grants(self):
        result = self.wizard.apply_defaults("12345", quota_mb=64)
        self.assertTrue(result["ok"])
        perm = self.permissions.get("12345")
        self.assertIsNotNone(perm)
        self.assertTrue(perm.enabled)
        self.assertEqual(perm.quota_bytes, 64 * 1024 * 1024)

    def test_apply_defaults_requires_id(self):
        self.assertFalse(self.wizard.apply_defaults("")["ok"])

    def test_apply_then_access_allowed(self):
        self.wizard.apply_defaults("12345")
        decision = self.service.decide("12345", "")
        self.assertTrue(decision.allowed)

    def test_unknown_task(self):
        self.assertFalse(self.wizard.task("nope")["ok"])

    def test_probe_reports_preferred_profile(self):
        wizard = SetupWizard(
            self.service,
            self.permissions,
            base_dir=self.base,
            rootfs=self.base / "rootfs",
            preferred_getter=lambda: "full",
        )
        self.assertEqual(wizard.probe()["preferred"], "full")

    def test_probe_profiles_are_available(self):
        data = self.wizard.probe()
        names = [item["name"] for item in data["profiles"]]
        self.assertEqual(sorted(names), ["full", "minimal", "standard"])

    def test_check_sources_covers_every_preset(self):
        """按发行版各取几个，不能只截前 6 条（Alpine 候选多会把别的挤掉）。"""
        items = [("alpine", f"a{i}") for i in range(8)]
        items += [("ubuntu", "u1"), ("debian", "d1")]
        # 冒烟测试会重建 sys.modules 里的 sandboxkit，所以直接改函数自己的全局命名空间
        globs = SetupWizard.check_sources.__globals__
        with mock.patch.dict(
            globs,
            {
                "resolve_all": lambda _arch: items,
                "probe_url": lambda url: {"url": url, "ok": True, "status": 200},
            },
        ):
            data = self.wizard.check_sources(2)
        presets = [item["preset"] for item in data["sources"]]
        self.assertEqual(presets.count("alpine"), 2)
        self.assertIn("ubuntu", presets)
        self.assertIn("debian", presets)
        self.assertEqual(data["presets"]["alpine"], 8)

    def test_summary(self):
        data = self.wizard.summary()
        self.assertTrue(data["ok"])
        self.assertFalse(data["disabled"])
        self.assertEqual(data["backend"], "local")


if __name__ == "__main__":
    unittest.main()
