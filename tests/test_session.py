# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.session import SandboxError, Session, sanitize_key  # noqa: E402


class TestSanitizeKey(unittest.TestCase):
    def test_readable_and_stable(self):
        key = sanitize_key("12345")
        self.assertTrue(key.startswith("12345-"))
        self.assertEqual(key, sanitize_key("12345"))

    def test_weird_input_is_slugged(self):
        key = sanitize_key("../../etc/passwd")
        self.assertNotIn("/", key)
        self.assertNotIn("..", key)
        self.assertLessEqual(len(key), 48)

    def test_empty_input(self):
        key = sanitize_key("")
        self.assertTrue(key)
        self.assertNotIn("/", key)

    def test_distinct_inputs_differ(self):
        self.assertNotEqual(sanitize_key("a b"), sanitize_key("a/b"))


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = Session(self.root, "user-1")
        self.session.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_from_key_does_not_rehash(self):
        session = Session.from_key(self.root, "u1-c5ea7155")
        self.assertEqual(session.key, "u1-c5ea7155")
        self.assertEqual(session.dir.name, "u1-c5ea7155")

    def test_ensure_creates_layout(self):
        for name in ("imports", "tmp", "out"):
            self.assertTrue((self.session.dir / name).is_dir())

    def test_list_and_read(self):
        (self.session.dir / "a.txt").write_text("hello")
        names = [row[0] for row in self.session.list_dir(".")]
        self.assertIn("a.txt", names)
        self.assertEqual(self.session.read_text("a.txt", 100), "hello")

    def test_read_truncates(self):
        (self.session.dir / "big.txt").write_text("x" * 500)
        text = self.session.read_text("big.txt", 100)
        self.assertIn("只显示前", text)
        self.assertLess(len(text), 500)

    def test_remove(self):
        (self.session.dir / "a.txt").write_text("x")
        self.session.remove("a.txt")
        self.assertFalse((self.session.dir / "a.txt").exists())

    def test_remove_root_refused(self):
        with self.assertRaises(SandboxError):
            self.session.remove(".")

    def test_reset(self):
        (self.session.dir / "a.txt").write_text("x")
        self.session.reset()
        self.assertFalse((self.session.dir / "a.txt").exists())
        self.assertTrue((self.session.dir / "imports").is_dir())

    def test_import_and_quota(self):
        src = self.root / "src.bin"
        src.write_bytes(b"0" * 100)
        target = self.session.import_host_file(
            src, max_bytes=1000, quota=10000
        )
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent.name, "imports")

    def test_import_over_limit(self):
        src = self.root / "src.bin"
        src.write_bytes(b"0" * 100)
        with self.assertRaises(SandboxError):
            self.session.import_host_file(src, max_bytes=10, quota=10000)

    def test_import_over_quota(self):
        src = self.root / "src.bin"
        src.write_bytes(b"0" * 100)
        with self.assertRaises(SandboxError):
            self.session.import_host_file(src, max_bytes=1000, quota=50)

    def test_import_name_sanitized(self):
        src = self.root / "src.bin"
        src.write_bytes(b"x")
        target = self.session.import_host_file(
            src, name="../../evil.sh", max_bytes=100, quota=10000
        )
        self.assertEqual(target.parent, self.session.dir / "imports")
        self.assertNotIn("..", target.name)

    def test_import_collision_keeps_both(self):
        src = self.root / "a.txt"
        src.write_text("1")
        first = self.session.import_host_file(src, max_bytes=100, quota=10000)
        second = self.session.import_host_file(src, max_bytes=100, quota=10000)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_usage_cached_reuses_value(self):
        (self.session.dir / "a.txt").write_text("x" * 10)
        first = self.session.usage_cached(ttl=100)
        second = self.session.usage_cached(ttl=100)
        self.assertIs(first, second)

    def test_invalidate_forces_rescan(self):
        (self.session.dir / "a.txt").write_text("x" * 10)
        first = self.session.usage_cached(ttl=100)
        (self.session.dir / "b.txt").write_text("y" * 5)
        self.session.invalidate()
        second = self.session.usage_cached(ttl=100)
        self.assertNotEqual(first.bytes, second.bytes)

    def test_usage_counts_regular_files(self):
        (self.session.dir / "a.txt").write_text("x" * 10)
        usage = self.session.usage()
        self.assertEqual(usage.bytes, 10)
        self.assertGreaterEqual(usage.files, 1)

    def test_prepare_script(self):
        rel = self.session.prepare_script("print(1)")
        self.assertTrue(rel.startswith("tmp/"))
        self.assertTrue((self.session.dir / rel).is_file())

    def test_escape_rejected(self):
        with self.assertRaises(SandboxError):
            self.session.resolve("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
