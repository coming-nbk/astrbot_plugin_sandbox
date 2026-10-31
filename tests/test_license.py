# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""协议声明必须三处都在：LICENSE 文件、README、每个源码文件头。

AGPL 要求分发时保留声明，漏一处就等于没声明。这条测试盯着。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestLicensePresence(unittest.TestCase):
    def test_license_file_is_agpl(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3", text)
        self.assertGreater(len(text.splitlines()), 500, "LICENSE 像是被截断了")

    def test_metadata_declares_license(self):
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("license: AGPL-3.0", meta)

    def test_readme_states_license_at_top(self):
        head = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:30])
        # 开头有协议徽章 + 白话说一段，徽章里是 AGPL--3.0（shields 的转义写法）
        self.assertIn("AGPL", head, "README 开头要写协议，不能只在末尾")
        self.assertIn("LICENSE", head)

    def test_every_python_file_has_spdx(self):
        missing = []
        for path in sorted(ROOT.rglob("*.py")):
            if any(part in {"__pycache__"} for part in path.parts):
                continue
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:4])
            if "SPDX-License-Identifier: AGPL-3.0-or-later" not in head:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], "这些文件缺协议头：" + "、".join(missing))

    def test_web_assets_have_notice(self):
        for name in ("pages/sandbox/index.html", "pages/sandbox/app.js", "pages/sandbox/app.css"):
            head = "\n".join((ROOT / name).read_text(encoding="utf-8").splitlines()[:6])
            self.assertIn("AGPL-3.0-or-later", head, f"{name} 缺协议头")
