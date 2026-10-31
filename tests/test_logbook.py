# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.logbook import LogBook  # noqa: E402


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "logs"
        self.book = LogBook(self.dir, retention_days=7, max_files=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_creates_file(self):
        self.book.info("hello", user="u1")
        files = self.book.files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text("utf-8")
        self.assertIn("hello", text)
        self.assertIn("user=u1", text)

    def test_file_is_private(self):
        self.book.info("hello")
        mode = stat.S_IMODE(self.book.files()[0].stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_dir_is_private(self):
        self.book.info("hello")
        mode = stat.S_IMODE(self.dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_disabled_writes_nothing(self):
        book = LogBook(self.dir, enabled=False)
        book.info("hello")
        self.assertFalse(self.dir.exists())

    def test_multiline_message_stays_one_line(self):
        self.book.error("line1\nline2")
        content = self.book.files()[0].read_text("utf-8")
        self.assertEqual(len(content.strip().split("\n")), 1)

    def test_rotate_on_size(self):
        book = LogBook(self.dir, max_bytes=64)
        for index in range(10):
            book.info("x" * 30, n=index)
        self.assertGreaterEqual(len(book.files()), 2)

    def test_tail_returns_last_lines(self):
        for index in range(5):
            self.book.info(f"line-{index}")
        tail = self.book.tail(2)
        self.assertEqual(len(tail), 2)
        self.assertIn("line-4", tail[-1])

    def test_total_bytes(self):
        self.book.info("abc")
        self.assertGreater(self.book.total_bytes(), 0)


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "logs"
        self.dir.mkdir(parents=True)
        self.book = LogBook(self.dir, retention_days=3, max_files=10)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name: str, age_days: float) -> Path:
        path = self.dir / name
        path.write_text("x\n", "utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_removes_expired_by_name(self):
        old = self._touch("sandbox-2020-01-01.log", 0)
        keep = self._touch(f"sandbox-{time.strftime('%Y-%m-%d')}.log", 0)
        removed = self.book.cleanup()
        self.assertIn(old.name, removed)
        self.assertTrue(keep.exists())

    def test_removes_expired_by_mtime(self):
        old = self._touch("sandbox-weird.log", 10)
        removed = self.book.cleanup()
        self.assertIn(old.name, removed)

    def test_keeps_recent(self):
        recent = self._touch("sandbox-recent.log", 1)
        self.book.cleanup()
        self.assertTrue(recent.exists())

    def test_max_files_trimming(self):
        names = [f"sandbox-2026-01-{day:02d}.log" for day in range(1, 6)]
        for name in names:
            self._touch(name, 0)
        book = LogBook(self.dir, retention_days=3650, max_files=2)
        removed = book.cleanup()
        self.assertEqual(len(removed), 3)
        self.assertEqual(len(book.files()), 2)

    def test_disabled_cleanup_does_nothing(self):
        old = self._touch("sandbox-2020-01-01.log", 0)
        book = LogBook(self.dir, enabled=False)
        self.assertEqual(book.cleanup(), [])
        self.assertTrue(old.exists())

    def test_empty_dir(self):
        empty = Path(self.tmp.name) / "nothing"
        book = LogBook(empty)
        self.assertEqual(book.cleanup(), [])
        self.assertEqual(book.files(), [])


if __name__ == "__main__":
    unittest.main()
