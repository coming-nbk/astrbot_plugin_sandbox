# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.paths import (  # noqa: E402
    PathRejected,
    human_bytes,
    resolve_host_file,
    resolve_sandbox_path,
    sandbox_relative,
)


class TestSandboxPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sandbox"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_relative_path(self):
        target = resolve_sandbox_path("a/b.txt", self.root)
        self.assertTrue(str(target).startswith(str(self.root.resolve())))

    def test_dot_is_root(self):
        self.assertEqual(
            Path(resolve_sandbox_path(".", self.root)), Path(self.root.resolve())
        )

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_sandbox_path("../etc/passwd", self.root)
        with self.assertRaises(PathRejected):
            resolve_sandbox_path("a/../../x", self.root)

    def test_absolute_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_sandbox_path("/etc/passwd", self.root)

    def test_empty_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_sandbox_path("   ", self.root)

    def test_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("secret")
        link = self.root / "link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):  # quickcheck:ignore - 平台差异
            self.skipTest("当前环境不支持符号链接")
        with self.assertRaises(PathRejected):
            resolve_sandbox_path("link", self.root)

    def test_prefix_sibling_not_confused(self):
        sibling = Path(str(self.root) + "-evil")
        sibling.mkdir()
        target = sibling / "x"
        target.write_text("x")
        with self.assertRaises(PathRejected):
            resolve_sandbox_path(f"../{sibling.name}/x", self.root)

    def test_relative_roundtrip(self):
        inner = self.root / "a" / "b.txt"
        inner.parent.mkdir()
        inner.write_text("hi")
        self.assertEqual(sandbox_relative(inner, self.root), "a/b.txt")


class TestHostFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.allowed = self.base / "data"
        self.allowed.mkdir()
        self.secret = self.allowed / ".env"
        self.secret.write_text("TOKEN=1")
        self.normal = self.allowed / "config.json"
        self.normal.write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside_root_ok(self):
        path = resolve_host_file(str(self.normal), roots=[str(self.allowed)])
        self.assertEqual(path, Path(os.path.realpath(self.normal)))

    def test_outside_root_rejected(self):
        other = self.base / "other.txt"
        other.write_text("x")
        with self.assertRaises(PathRejected):
            resolve_host_file(str(other), roots=[str(self.allowed)])

    def test_relative_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_host_file("config.json", roots=[str(self.allowed)])

    def test_deny_glob(self):
        with self.assertRaises(PathRejected):
            resolve_host_file(
                str(self.secret), roots=[str(self.allowed)], deny_globs=[".env"]
            )

    def test_no_roots_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_host_file(str(self.normal), roots=[])

    def test_missing_file_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_host_file(
                str(self.allowed / "nope.txt"), roots=[str(self.allowed)]
            )

    def test_directory_rejected_by_default(self):
        with self.assertRaises(PathRejected):
            resolve_host_file(str(self.allowed), roots=[str(self.allowed)])

    def test_directory_allowed_when_enabled(self):
        path = resolve_host_file(
            str(self.allowed), roots=[str(self.allowed)], allow_dirs=True
        )
        self.assertTrue(path.is_dir())

    def test_symlink_out_of_root_rejected(self):
        link = self.allowed / "link"
        try:
            os.symlink("/etc/hostname", link)
        except (OSError, NotImplementedError):  # quickcheck:ignore - 平台差异
            self.skipTest("当前环境不支持符号链接")
        with self.assertRaises(PathRejected):
            resolve_host_file(str(link), roots=[str(self.allowed)])


class TestHumanBytes(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_bytes(0), "0B")
        self.assertEqual(human_bytes(999), "999B")
        self.assertEqual(human_bytes(1024), "1KB")
        self.assertEqual(human_bytes(1536), "1.5KB")
        self.assertEqual(human_bytes(1024 * 1024), "1MB")


if __name__ == "__main__":
    unittest.main()

class TestHumanBytesFormatting(unittest.TestCase):
    def test_integral_drops_decimal(self):
        from sandboxkit.paths import human_bytes

        self.assertEqual(human_bytes(256 * 1024 * 1024), "256MB")
        self.assertEqual(human_bytes(1024 * 1024 * 1024), "1GB")

    def test_fraction_keeps_one_decimal(self):
        from sandboxkit.paths import human_bytes

        self.assertEqual(human_bytes(int(1.5 * 1024 * 1024)), "1.5MB")

    def test_bytes_have_no_decimal(self):
        from sandboxkit.paths import human_bytes

        self.assertEqual(human_bytes(388), "388B")

