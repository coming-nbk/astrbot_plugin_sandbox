# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit import pluginconf  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402


class TestValidate(unittest.TestCase):
    def test_mb_scaled_to_bytes(self):
        clean = pluginconf.validate({"quota_bytes": 64})
        self.assertEqual(clean["quota_bytes"], 64 * 1024 * 1024)

    def test_bool_coercion(self):
        self.assertTrue(pluginconf.validate({"enabled": "true"})["enabled"])
        self.assertFalse(pluginconf.validate({"enabled": "0"})["enabled"])

    def test_list_from_string(self):
        clean = pluginconf.validate({"admin_users": "1, 2，3"})
        self.assertEqual(clean["admin_users"], ["1", "2", "3"])

    def test_select_validated(self):
        with self.assertRaises(pluginconf.ConfigError):
            pluginconf.validate({"backend": "firecracker"})

    def test_negative_int_rejected(self):
        with self.assertRaises(pluginconf.ConfigError):
            pluginconf.validate({"timeout_seconds": -5})

    def test_unknown_keys_ignored(self):
        clean = pluginconf.validate({"nonsense": 1, "enabled": True})
        self.assertEqual(clean, {"enabled": True})

    def test_empty_updates_rejected(self):
        with self.assertRaises(pluginconf.ConfigError):
            pluginconf.validate({})


class TestSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config" / "astrbot_plugin_sandbox_config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_file(self):
        merged = pluginconf.save_config(self.path, {"enabled": True})
        self.assertTrue(self.path.is_file())
        self.assertTrue(json.loads(self.path.read_text("utf-8"))["enabled"])
        self.assertEqual(merged["enabled"], True)

    def test_merges_existing(self):
        pluginconf.save_config(self.path, {"enabled": True})
        pluginconf.save_config(self.path, {"timeout_seconds": 45})
        data = json.loads(self.path.read_text("utf-8"))
        self.assertTrue(data["enabled"])
        self.assertEqual(data["timeout_seconds"], 45)

    def test_backup_created(self):
        pluginconf.save_config(self.path, {"enabled": True})
        pluginconf.save_config(self.path, {"enabled": False})
        backups = list(self.path.parent.glob("*.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_root_switch(self):
        pluginconf.save_config(self.path, {"chroot_uid": 0})
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["chroot_uid"], 0)

    def test_snapshot_has_all_fields(self):
        rows = pluginconf.snapshot(self.path, Settings())
        keys = [row["key"] for row in rows]
        self.assertIn("chroot_uid", keys)
        self.assertIn("backend", keys)
        self.assertEqual(len(rows), len(pluginconf.FIELDS))

    def test_snapshot_scales_bytes_back_to_mb(self):
        pluginconf.save_config(self.path, {"quota_bytes": 128})
        rows = {row["key"]: row for row in pluginconf.snapshot(self.path, Settings())}
        self.assertEqual(rows["quota_bytes"]["value"], 128)

    def test_find_config_prefers_existing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}", "utf-8")
        found = pluginconf.find_config(self.path.parent.parent, "astrbot_plugin_sandbox")
        self.assertEqual(found, self.path)


if __name__ == "__main__":
    unittest.main()

class TestSnapshotTolerance(unittest.TestCase):
    """配置文件坏了，设置页也不能 500。"""

    def test_broken_json_falls_back(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.config import load_settings

        path = _Path(tempfile.mkdtemp()) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        warnings: list[str] = []
        rows = pluginconf.snapshot(path, load_settings({}, fallback_import_roots=["/tmp"]), warnings)
        self.assertEqual(len(rows), len(pluginconf.FIELDS))
        self.assertTrue(warnings)
        self.assertIn("配置文件读不出来", warnings[0])

    def test_missing_file_is_silent(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.config import load_settings

        path = _Path(tempfile.mkdtemp()) / "none.json"
        warnings: list[str] = []
        rows = pluginconf.snapshot(path, load_settings({}, fallback_import_roots=["/tmp"]), warnings)
        self.assertEqual(len(rows), len(pluginconf.FIELDS))
        self.assertEqual(warnings, [])

class TestBomTolerance(unittest.TestCase):
    """AstrBot 写出来的配置文件可能带 UTF-8 BOM，不能因此读不出来。"""

    def test_read_config_with_bom(self):
        import tempfile
        from pathlib import Path as _Path

        path = _Path(tempfile.mkdtemp()) / "cfg.json"
        path.write_bytes(b"\xef\xbb\xbf" + '{"enabled": true}'.encode("utf-8"))
        self.assertEqual(pluginconf.read_config(path), {"enabled": True})

    def test_save_config_with_bom_file(self):
        import tempfile
        from pathlib import Path as _Path

        path = _Path(tempfile.mkdtemp()) / "cfg.json"
        path.write_bytes(b"\xef\xbb\xbf" + '{"enabled": true}'.encode("utf-8"))
        merged = pluginconf.save_config(path, {"timeout_seconds": 45})
        self.assertTrue(merged["enabled"])
        self.assertEqual(merged["timeout_seconds"], 45)

class TestFieldGroups(unittest.TestCase):
    """每个字段都必须落在已知分组里，页面才好排版。"""

    def test_no_ungrouped_field(self):
        from sandboxkit.pluginconf import FIELDS, GROUP_ORDER

        for field in FIELDS:
            self.assertIn(field.group, GROUP_ORDER, f"{field.key} 没有分组")
            self.assertNotEqual(field.group, "其它", f"{field.key} 落在了「其它」")

    def test_snapshot_carries_group(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.config import load_settings

        path = _Path(tempfile.mkdtemp()) / "cfg.json"
        rows = pluginconf.snapshot(path, load_settings({}, fallback_import_roots=["/tmp"]))
        for row in rows:
            self.assertIn("group", row)
            self.assertTrue(row["group"])

