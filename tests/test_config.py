# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.config import Settings, check_access, load_settings  # noqa: E402


class TestLoadSettings(unittest.TestCase):
    def test_defaults_are_sane(self):
        s = load_settings(None)
        self.assertTrue(s.enabled)
        self.assertEqual(s.backend, "auto")
        self.assertFalse(s.allow_all)
        self.assertFalse(s.allow_network)
        self.assertGreater(s.timeout_seconds, 0)

    def test_string_numbers_are_coerced(self):
        s = load_settings({"timeout_seconds": "12", "memory_mb": " 256 "})
        self.assertEqual(s.timeout_seconds, 12)
        self.assertEqual(s.memory_mb, 256)

    def test_garbage_numbers_fall_back(self):
        s = load_settings({"timeout_seconds": "abc", "memory_mb": None})
        self.assertEqual(s.timeout_seconds, 30)
        self.assertEqual(s.memory_mb, 512)

    def test_clamping(self):
        s = load_settings({"timeout_seconds": 999999, "memory_mb": 1})
        self.assertEqual(s.timeout_seconds, 3600)
        self.assertEqual(s.memory_mb, 64)

    def test_bool_variants(self):
        for value in (True, "true", "1", "yes", "开"):
            self.assertTrue(load_settings({"enabled": value}).enabled)
        for value in (False, "false", "0", "off", "关", ""):
            self.assertFalse(load_settings({"enabled": value}).enabled)

    def test_list_accepts_string_and_list(self):
        s = load_settings({"allowed_users": "123, 456\n789"})
        self.assertEqual(s.allowed_users, frozenset({"123", "456", "789"}))
        s2 = load_settings({"allowed_users": [123, "456"]})
        self.assertEqual(s2.allowed_users, frozenset({"123", "456"}))

    def test_unknown_backend_falls_back_with_warning(self):
        s = load_settings({"backend": "firecracker"})
        self.assertEqual(s.backend, "auto")
        self.assertTrue(any("firecracker" in w for w in s.warnings))

    def test_empty_import_roots_uses_fallback(self):
        s = load_settings({}, fallback_import_roots=["/opt/astrbot/data"])
        self.assertEqual(s.import_roots, ("/opt/astrbot/data",))
        self.assertTrue(s.warnings)

    def test_explicit_import_roots_win(self):
        s = load_settings(
            {"import_roots": ["/tmp/a"]}, fallback_import_roots=["/opt/astrbot/data"]
        )
        self.assertEqual(s.import_roots, ("/tmp/a",))

    def test_deny_globs_default_kept_when_absent(self):
        s = load_settings({})
        self.assertIn(".env", s.import_deny_globs)

    def test_deny_globs_explicit_empty_is_respected(self):
        s = load_settings({"import_deny_globs": []})
        self.assertEqual(s.import_deny_globs, ())


class TestAccess(unittest.TestCase):
    def test_disabled_blocks_everyone(self):
        s = Settings(enabled=False, allow_all=True)
        self.assertFalse(check_access(s, "1").allowed)

    def test_admin_always_allowed(self):
        s = Settings(admin_users=frozenset({"42"}))
        d = check_access(s, "42")
        self.assertTrue(d.allowed)
        self.assertTrue(d.is_admin)

    def test_allow_all(self):
        s = Settings(allow_all=True)
        self.assertTrue(check_access(s, "anyone").allowed)

    def test_user_whitelist(self):
        s = Settings(allowed_users=frozenset({"7"}))
        self.assertTrue(check_access(s, "7").allowed)
        self.assertFalse(check_access(s, "8").allowed)

    def test_group_whitelist(self):
        s = Settings(allowed_groups=frozenset({"100"}))
        self.assertTrue(check_access(s, "8", "100").allowed)
        self.assertFalse(check_access(s, "8", "101").allowed)

    def test_empty_ids_never_match(self):
        s = Settings(allowed_users=frozenset({""}), allowed_groups=frozenset({""}))
        self.assertFalse(check_access(s, "", "").allowed)


if __name__ == "__main__":
    unittest.main()
