# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""多语言：缺 key 要回落，切换语言要生效，页面要跟着变。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit import i18n  # noqa: E402
from sandboxkit.guide import build_guide  # noqa: E402


def _guide(lang):
    return build_guide(
        lang=lang,
        user_id="10001",
        is_admin=False,
        quota_bytes=256 * 1024 * 1024,
        timeout_seconds=30,
        memory_mb=512,
        max_runs_per_minute=20,
        allow_network=False,
        allow_import=True,
        backend="chroot",
        preset="ubuntu",
        version="24.04",
    )


class TestTranslate(unittest.TestCase):
    def test_basic(self):
        self.assertIn("沙盒用法", i18n.t("guide.title", "zh", user="u1"))
        self.assertIn("Sandbox guide", i18n.t("guide.title", "en", user="u1"))

    def test_locale_normalization(self):
        self.assertEqual(i18n.normalize("zh-CN"), "zh")
        self.assertEqual(i18n.normalize("en_US"), "en")
        self.assertEqual(i18n.normalize("fr"), i18n.DEFAULT)

    def test_missing_key_falls_back(self):
        self.assertEqual(i18n.t("nope.nope", "en"), "nope.nope")
        # 英文缺的 key 回落到中文
        self.assertEqual(i18n.t("guide.title", "en", user="x"),
                         "Sandbox guide (x)")

    def test_bad_placeholder_does_not_raise(self):
        self.assertTrue(i18n.t("guide.title", "en"))  # 不给 user 也不炸


class TestGuideLanguages(unittest.TestCase):
    def test_chinese(self):
        text = _guide("zh")
        self.assertIn("你的限制", text)
        self.assertIn("沙盒里现在有什么", text)

    def test_english(self):
        text = _guide("en")
        self.assertIn("Your limits", text)
        self.assertIn("What's inside", text)
        self.assertNotIn("你的限制", text)

    def test_limits_are_numbers(self):
        text = _guide("en")
        self.assertIn("256MB", text)
        self.assertIn("30s", text)


class TestAuditLanguage(unittest.TestCase):
    def test_english_report(self):
        from sandboxkit.audit_sec import local_findings, render

        findings = local_findings(
            __import__("sandboxkit.config", fromlist=["Settings"]).Settings(
                enabled=True, backend="local"
            ),
            backend="local",
            rootfs=None,
        )
        text = render(findings, kernel="6.1.0", backend="local", lang="en")
        self.assertIn("Security check", text)
        self.assertIn("Sandbox does not cover", text)
        self.assertNotIn("沙盒管不了", text)


class TestPageI18n(unittest.TestCase):
    def test_page_has_dictionary(self):
        js = (Path(__file__).resolve().parents[1] / "pages/sandbox/app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("var TEXT = {", js)
        self.assertIn("function applyLanguage", js)
        self.assertIn("data-i18n", js)
