# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.parse import split_command  # noqa: E402


class TestSplitCommand(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(split_command("/sbx run ls -l"), ("run", "ls -l"))

    def test_without_slash(self):
        self.assertEqual(split_command("sbx run echo hi"), ("run", "echo hi"))

    def test_command_only(self):
        self.assertEqual(split_command("/sbx"), ("", ""))

    def test_at_suffix(self):
        self.assertEqual(split_command("/sbx@bot ls tmp"), ("ls", "tmp"))

    def test_chinese_alias(self):
        self.assertEqual(split_command("/沙盒 run pwd"), ("run", "pwd"))

    def test_multiline_python(self):
        sub, arg = split_command("/sbx py import os\nprint(1)")
        self.assertEqual(sub, "py")
        self.assertIn("print(1)", arg)
        self.assertIn("import os", arg)

    def test_extra_spaces(self):
        self.assertEqual(split_command("  /sbx   run   ls  "), ("run", "ls"))

    def test_empty(self):
        self.assertEqual(split_command(""), ("", ""))


if __name__ == "__main__":
    unittest.main()
