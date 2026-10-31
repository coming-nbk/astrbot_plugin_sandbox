# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.store import (  # noqa: E402
    PermissionStore,
    ScopeStore,
    StoreError,
    TrustStore,
    apply_user_perm,
    match_scope,
    match_trust,
    normalize_perm,
    normalize_scope,
    normalize_trust,
)


class TestNormalize(unittest.TestCase):
    def test_requires_user_id(self):
        with self.assertRaises(StoreError):
            normalize_perm({})

    def test_clamps(self):
        perm = normalize_perm(
            {"user_id": "1", "quota_bytes": 10 ** 15, "timeout_seconds": 99999}
        )
        self.assertLessEqual(perm.quota_bytes, 64 * 1024 * 1024 * 1024)
        self.assertEqual(perm.timeout_seconds, 3600)

    def test_tri_state_flags(self):
        self.assertIsNone(normalize_perm({"user_id": "1"}).allow_network)
        self.assertIsNone(normalize_perm({"user_id": "1", "allow_network": ""}).allow_network)
        self.assertTrue(normalize_perm({"user_id": "1", "allow_network": "true"}).allow_network)
        self.assertFalse(normalize_perm({"user_id": "1", "allow_network": False}).allow_network)


class TestPermissionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "permissions.json"
        self.store = PermissionStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_and_get(self):
        self.store.upsert({"user_id": "42", "note": "朋友", "quota_bytes": 1024})
        perm = self.store.get("42")
        self.assertIsNotNone(perm)
        self.assertEqual(perm.note, "朋友")
        self.assertEqual(perm.quota_bytes, 1024)

    def test_persists_to_disk(self):
        self.store.upsert({"user_id": "42"})
        self.assertTrue(self.path.is_file())
        reloaded = PermissionStore(self.path)
        self.assertIsNotNone(reloaded.get("42"))

    def test_delete(self):
        self.store.upsert({"user_id": "42"})
        self.assertTrue(self.store.delete("42"))
        self.assertFalse(self.store.delete("42"))
        self.assertIsNone(self.store.get("42"))

    def test_corrupt_file_is_quarantined(self):
        self.path.write_text("{not json", "utf-8")
        store = PermissionStore(self.path)
        self.assertEqual(store.all(), [])
        self.assertTrue(list(self.path.parent.glob("*.broken")))

    def test_all_is_sorted(self):
        for uid in ("3", "1", "2"):
            self.store.upsert({"user_id": uid})
        self.assertEqual([p.user_id for p in self.store.all()], ["1", "2", "3"])

    def test_written_file_is_valid_json(self):
        self.store.upsert({"user_id": "42"})
        data = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["users"][0]["user_id"], "42")


class TestApplyUserPerm(unittest.TestCase):
    def test_none_returns_same(self):
        settings = Settings()
        self.assertIs(apply_user_perm(settings, None), settings)

    def test_overrides(self):
        settings = Settings(quota_bytes=100, timeout_seconds=30)
        perm = normalize_perm(
            {"user_id": "1", "quota_bytes": 500, "timeout_seconds": 5, "allow_network": True}
        )
        effective = apply_user_perm(settings, perm)
        self.assertEqual(effective.quota_bytes, 500)
        self.assertEqual(effective.timeout_seconds, 5)
        self.assertTrue(effective.allow_network)
        self.assertEqual(settings.quota_bytes, 100)

    def test_zero_means_follow_global(self):
        settings = Settings(quota_bytes=100, timeout_seconds=30)
        perm = normalize_perm({"user_id": "1", "quota_bytes": 0, "timeout_seconds": 0})
        effective = apply_user_perm(settings, perm)
        self.assertEqual(effective.quota_bytes, 100)
        self.assertEqual(effective.timeout_seconds, 30)


class TestTrustStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "trust.json"
        self.store = TrustStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_and_persist(self):
        self.store.upsert({"pattern": "make *", "note": "构建"})
        self.assertTrue(self.path.is_file())
        again = TrustStore(self.path)
        self.assertIsNotNone(again.get("make *"))

    def test_pattern_required(self):
        with self.assertRaises(StoreError):
            self.store.upsert({"pattern": ""})

    def test_delete(self):
        self.store.upsert({"pattern": "make *"})
        self.assertTrue(self.store.delete("make *"))
        self.assertIsNone(self.store.get("make *"))

    def test_match_glob(self):
        self.store.upsert({"pattern": "make *"})
        rules = self.store.all()
        self.assertIsNotNone(match_trust(rules, "make build"))
        self.assertIsNone(match_trust(rules, "rm -rf /"))

    def test_match_ignores_disabled(self):
        self.store.upsert({"pattern": "make *", "enabled": False})
        self.assertIsNone(match_trust(self.store.all(), "make build"))

    def test_match_empty_command(self):
        self.store.upsert({"pattern": "*"})
        self.assertIsNone(match_trust(self.store.all(), "   "))

    def test_normalize_defaults(self):
        rule = normalize_trust({"pattern": "x"})
        self.assertTrue(rule.bypass_memory)
        self.assertTrue(rule.bypass_cpu)
        self.assertEqual(rule.timeout_seconds, 0)


class TestScopeStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "scopes.json"
        self.store = ScopeStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_by_key(self):
        self.store.upsert({"scope": "group", "target": "123", "allow": True})
        self.assertIsNotNone(self.store.get("group:123"))

    def test_scope_validated(self):
        with self.assertRaises(StoreError):
            self.store.upsert({"scope": "planet", "target": "1"})

    def test_group_requires_target(self):
        with self.assertRaises(StoreError):
            self.store.upsert({"scope": "group", "target": ""})

    def test_users_parsed(self):
        rule = normalize_scope({"scope": "group", "target": "1", "users": "a, b"})
        self.assertEqual(rule.users, ("a", "b"))

    def test_specific_group_wins(self):
        self.store.upsert({"scope": "group", "target": "*", "allow": True})
        self.store.upsert({"scope": "group", "target": "123", "allow": False})
        rules = self.store.all()
        rule = match_scope(rules, "123", False)
        self.assertEqual(rule.target, "123")
        self.assertFalse(rule.allow)

    def test_private_rule(self):
        self.store.upsert({"scope": "private", "allow": False})
        self.assertIsNotNone(match_scope(self.store.all(), "", True))
        self.assertIsNone(match_scope(self.store.all(), "123", False))

    def test_no_match(self):
        self.store.upsert({"scope": "group", "target": "999"})
        self.assertIsNone(match_scope(self.store.all(), "123", False))


if __name__ == "__main__":
    unittest.main()
