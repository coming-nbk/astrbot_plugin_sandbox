# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.session import SandboxError, Session  # noqa: E402
from sandboxkit.webfs import (  # noqa: E402
    delete_path,
    list_tree,
    preview,
    save_text,
    session_summary,
)


class TestTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(Path(self.tmp.name), "u1")
        self.session.ensure()
        (self.session.dir / "a.txt").write_text("a")
        (self.session.dir / "sub").mkdir()
        (self.session.dir / "sub" / "b.txt").write_text("b")

    def tearDown(self):
        self.tmp.cleanup()

    def test_depth_one_hides_children(self):
        data = list_tree(self.session, ".", depth=1)
        sub = [row for row in data["entries"] if row["name"] == "sub"][0]
        self.assertNotIn("children", sub)

    def test_depth_two_includes_children(self):
        data = list_tree(self.session, ".", depth=2)
        sub = [row for row in data["entries"] if row["name"] == "sub"][0]
        self.assertEqual(sub["children"][0]["name"], "b.txt")

    def test_kinds(self):
        data = list_tree(self.session, ".", depth=1)
        kinds = {row["name"]: row["kind"] for row in data["entries"]}
        self.assertEqual(kinds["a.txt"], "text")
        self.assertEqual(kinds["sub"], "dir")

    def test_escape_rejected(self):
        with self.assertRaises(SandboxError):
            list_tree(self.session, "../..", depth=1)

    def test_missing_dir(self):
        with self.assertRaises(SandboxError):
            list_tree(self.session, "nope", depth=1)

    def test_summary(self):
        data = session_summary(self.session)
        self.assertGreaterEqual(data["files"], 2)
        self.assertEqual(data["bytes"], 2)


class TestPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(Path(self.tmp.name), "u1")
        self.session.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_text(self):
        (self.session.dir / "a.py").write_text("print(1)")
        data = preview(self.session, "a.py")
        self.assertEqual(data["kind"], "text")
        self.assertIn("print(1)", data["content"])

    def test_truncated(self):
        (self.session.dir / "big.txt").write_text("x" * 5000)
        data = preview(self.session, "big.txt", max_bytes=100)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["content"]), 100)

    def test_binary(self):
        (self.session.dir / "a.bin").write_bytes(b"\x00\x01\x02")
        data = preview(self.session, "a.bin")
        self.assertEqual(data["kind"], "binary")
        self.assertIn("head_b64", data, "二进制要带一段头部，前端才能显示十六进制")

    def test_image(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        (self.session.dir / "a.png").write_bytes(payload)
        data = preview(self.session, "a.png")
        self.assertEqual(data["kind"], "image")
        self.assertIn("data:image/png;base64,", data["data_url"])
        self.assertEqual(
            base64.b64decode(data["data_url"].split(",", 1)[1]), payload
        )

    def test_dir_preview_lists_entries(self):
        (self.session.dir / "sub").mkdir()
        (self.session.dir / "sub" / "x.txt").write_text("x")
        data = preview(self.session, "sub")
        self.assertEqual(data["kind"], "dir")
        self.assertEqual(data["entries"][0]["name"], "x.txt")

    def test_escape_rejected(self):
        with self.assertRaises(SandboxError):
            preview(self.session, "../../etc/passwd")


class TestWriteDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(Path(self.tmp.name), "u1")
        self.session.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_creates_file(self):
        data = save_text(
            self.session, "notes/a.txt", "hello", max_bytes=100, quota=10 ** 6
        )
        self.assertEqual(data["size"], 5)
        self.assertEqual((self.session.dir / "notes" / "a.txt").read_text(), "hello")

    def test_save_over_limit(self):
        with self.assertRaises(SandboxError):
            save_text(self.session, "a.txt", "x" * 100, max_bytes=10, quota=10 ** 6)

    def test_save_over_quota(self):
        with self.assertRaises(SandboxError):
            save_text(self.session, "a.txt", "x" * 100, max_bytes=1000, quota=50)

    def test_save_root_refused(self):
        with self.assertRaises(SandboxError):
            save_text(self.session, ".", "x", max_bytes=10, quota=10 ** 6)

    def test_delete_file(self):
        (self.session.dir / "a.txt").write_text("x")
        self.assertEqual(delete_path(self.session, "a.txt"), "a.txt")
        self.assertFalse((self.session.dir / "a.txt").exists())

    def test_delete_root_refused(self):
        with self.assertRaises(SandboxError):
            delete_path(self.session, ".")

    def test_delete_missing(self):
        with self.assertRaises(SandboxError):
            delete_path(self.session, "nope.txt")


if __name__ == "__main__":
    unittest.main()
