# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""插件元信息一致性：版本号、名字必须和目录对得上。

AstrBot 安装时读 metadata.yaml 存进自己的插件库，覆盖文件不会刷新显示 ——
所以两处版本号必须自己保持一致，否则用户看到的是旧版本号。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "metadata.yaml"
MAIN = ROOT / "main.py"


class TestMetadata(unittest.TestCase):
    def setUp(self):
        self.meta = META.read_text("utf-8")
        self.main = MAIN.read_text("utf-8")

    def test_name_matches_directory(self):
        name = re.search(r"^name:\s*(\S+)", self.meta, re.M).group(1)
        self.assertEqual(name, ROOT.name)

    def test_versions_match(self):
        meta_version = re.search(r"^version:\s*v?([\d.]+)", self.meta, re.M).group(1)
        main_version = re.search(r'"(\d+\.\d+\.\d+)"', self.main).group(1)
        self.assertEqual(
            meta_version,
            main_version,
            "metadata.yaml 和 main.py 的版本号必须一致（否则插件卡片显示旧版本）",
        )

    def test_has_display_name_and_logo(self):
        self.assertIn("display_name:", self.meta)
        self.assertTrue((ROOT / "logo.png").is_file(), "logo.png 缺失")
        self.assertIn("logo: logo.png", self.meta)


if __name__ == "__main__":
    unittest.main()
