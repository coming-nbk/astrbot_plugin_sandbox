# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.bootstrap import APK_ALIASES, install_packages  # noqa: E402


class TestInstallPackages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rootfs = Path(self.tmp.name)
        (self.rootfs / "sbin").mkdir()
        (self.rootfs / "sbin" / "apk").write_text("")

    def _make_apt(self):
        """切成 apt 环境：删掉 apk，避免两个包管理器同时存在。"""
        (self.rootfs / "sbin" / "apk").unlink()
        (self.rootfs / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        (self.rootfs / "usr" / "bin" / "apt-get").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_apk_batch_success(self):
        calls = []

        def fake_run(rootfs, argv, timeout=600.0):
            calls.append(list(argv))
            return True, ""

        with mock.patch("sandboxkit.bootstrap._run_in_rootfs", side_effect=fake_run):
            ok, message = install_packages(self.rootfs, ["python3", "git"], log=lambda _m: None)
        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(calls[0][:3], ["/sbin/apk", "add", "--no-cache"])

    def test_apk_aliases_translated(self):
        calls = []

        def fake_run(rootfs, argv, timeout=600.0):
            calls.append(list(argv))
            return True, ""

        with mock.patch("sandboxkit.bootstrap._run_in_rootfs", side_effect=fake_run):
            install_packages(
                self.rootfs, ["python3-pip", "build-essential", "sqlite3"], log=lambda _m: None
            )
        joined = " ".join(calls[0])
        self.assertIn("py3-pip", joined)
        self.assertIn("build-base", joined)
        self.assertIn("sqlite", joined)
        self.assertNotIn("python3-pip", joined)
        self.assertNotIn("build-essential", joined)

    def test_partial_failure_keeps_going(self):
        def fake_run(rootfs, argv, timeout=600.0):
            joined = " ".join(argv)
            if "py3-pip" in joined or "python3-pip" in joined:
                return False, "ERROR: unable to select packages"
            return True, ""

        with mock.patch("sandboxkit.bootstrap._run_in_rootfs", side_effect=fake_run):
            ok, message = install_packages(
                self.rootfs, ["python3", "python3-pip", "git"], log=lambda _m: None
            )
        self.assertTrue(ok, "至少装上了部分包就算成功")
        self.assertIn("部分失败", message)
        self.assertIn("py3-pip", message)

    def test_all_fail(self):
        with mock.patch(
            "sandboxkit.bootstrap._run_in_rootfs", side_effect=lambda *a, **k: (False, "404")
        ):
            ok, message = install_packages(self.rootfs, ["a", "b"], log=lambda _m: None)
        self.assertFalse(ok)
        self.assertIn("全部失败", message)

    def test_apt_runs_update_first(self):
        calls = []

        def fake_run(rootfs, argv, timeout=600.0):
            calls.append(list(argv))
            return True, ""

        self._make_apt()
        with mock.patch("sandboxkit.bootstrap._run_in_rootfs", side_effect=fake_run):
            ok, _message = install_packages(self.rootfs, ["python3"], log=lambda _m: None)
        self.assertTrue(ok)
        self.assertEqual(calls[0][1], "update")
        self.assertIn("install", calls[1])

    def test_apt_update_failure_aborts(self):
        def fake_run(rootfs, argv, timeout=600.0):
            if "update" in argv:
                return False, "network down"
            return True, ""

        self._make_apt()
        with mock.patch("sandboxkit.bootstrap._run_in_rootfs", side_effect=fake_run):
            ok, message = install_packages(self.rootfs, ["python3"], log=lambda _m: None)
        self.assertFalse(ok)
        self.assertIn("apt-get update", message)

    def test_no_package_manager(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        ok, message = install_packages(empty, ["python3"], log=lambda _m: None)
        self.assertFalse(ok)
        self.assertIn("既没有 apk 也没有 apt-get", message)

    def test_empty_list_is_noop(self):
        ok, message = install_packages(self.rootfs, [], log=lambda _m: None)
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_alias_table_covers_standard_profile(self):
        # standard / full 套餐里的 Debian 包名，在 Alpine 下都得有落点或原样尝试
        for name in ("python3-pip", "build-essential", "sqlite3"):
            self.assertIn(name, APK_ALIASES)


if __name__ == "__main__":
    unittest.main()
