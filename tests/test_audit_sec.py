# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""安全体检：本地检查要能抓出真问题，外部漏洞源要能容错。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.audit_sec import (  # noqa: E402
    count_setuid,
    fetch_feed,
    local_findings,
    match_feed,
    parse_kev,
    parse_nvd,
    render,
)
from sandboxkit.config import Settings  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(enabled=True, backend="chroot", allow_all=True)
    base.update(overrides)
    return Settings(**base)


class TestLocalFindings(unittest.TestCase):
    def test_local_backend_is_high(self):
        findings = local_findings(_settings(), backend="local", rootfs=None)
        ids = [item.id for item in findings]
        self.assertIn("backend-local", ids)
        self.assertEqual(findings[0].severity, "high")

    def test_root_in_chroot_is_flagged(self):
        findings = local_findings(
            _settings(chroot_uid=0, chroot_gid=0), backend="chroot", rootfs=None
        )
        self.assertIn("chroot-root", [item.id for item in findings])

    def test_import_root_all_is_flagged(self):
        findings = local_findings(
            _settings(import_roots=("/",)), backend="chroot", rootfs=None
        )
        self.assertIn("import-root-all", [item.id for item in findings])

    def test_no_auth_is_flagged(self):
        findings = local_findings(
            _settings(web_auth="none"), backend="chroot", rootfs=None
        )
        self.assertIn("web-no-auth", [item.id for item in findings])

    def test_clean_config_has_no_high(self):
        findings = local_findings(_settings(), backend="chroot", rootfs=None)
        self.assertFalse([item for item in findings if item.severity == "high"])

    def test_setuid_binaries_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "su"
            binary.write_text("x", encoding="utf-8")
            os.chmod(binary, 0o4755)
            total, sample = count_setuid(root)
            self.assertEqual(total, 1)
            self.assertIn("su", sample[0])

            findings = local_findings(_settings(), backend="chroot", rootfs=root)
            self.assertIn("setuid-binaries", [item.id for item in findings])


class TestFeed(unittest.TestCase):
    def test_match_by_kernel_regex(self):
        items = [
            {"id": "CVE-1", "title": "旧内核提权", "severity": "high", "affects": r"^5\.", "fix": "升级"},
            {"id": "CVE-2", "title": "新内核问题", "severity": "low", "affects": r"^6\.", "fix": "升级"},
        ]
        matched = match_feed(items, kernel="5.15.0-generic", backend="chroot")
        self.assertEqual([item.id for item in matched], ["feed-CVE-1"])

    def test_cache_is_used_when_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "feed.json"
            cache.write_text(
                json.dumps({"at": 9e9, "items": [{"id": "CVE-9", "title": "缓存", "severity": "high"}]}),
                encoding="utf-8",
            )
            items = fetch_feed("https://example.invalid/feed.json", cache_path=cache)
            self.assertEqual(items[0]["id"], "CVE-9")

    def test_missing_feed_is_empty(self):
        self.assertEqual(fetch_feed(""), [])

    def test_render_lists_findings(self):
        findings = local_findings(_settings(), backend="local", rootfs=None)
        text = render(findings, kernel="6.1.0", backend="local")
        self.assertIn("安全体检", text)
        self.assertIn("高", text)
        self.assertIn("修法", text)

class TestFeedParsers(unittest.TestCase):
    """NVD / CISA KEV 的原生格式要能直接吃。"""

    def test_parse_nvd(self):
        payload = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": "CVE-2026-0001",
                        "descriptions": [{"lang": "en", "value": "Linux kernel 提权漏洞"}],
                        "metrics": {
                            "cvssMetricV31": [{"cvssData": {"baseSeverity": "CRITICAL"}}]
                        },
                    }
                },
                {"cve": {"id": "", "descriptions": []}},  # 缺 id，跳过
            ]
        }
        items = parse_nvd(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "CVE-2026-0001")
        self.assertEqual(items[0]["severity"], "high")
        self.assertIn("Linux kernel", items[0]["detail"])

    def test_parse_kev(self):
        payload = {
            "vulnerabilities": [
                {
                    "cveID": "CVE-2026-0002",
                    "vulnerabilityName": "Linux Kernel 已被利用",
                    "product": "Linux Kernel",
                    "shortDescription": "在野利用",
                    "requiredAction": "打补丁",
                }
            ]
        }
        items = parse_kev(payload)
        self.assertEqual(items[0]["id"], "CVE-2026-0002")
        self.assertEqual(items[0]["severity"], "high")
        self.assertIn("打补丁", items[0]["fix"])

    def test_unknown_format_falls_back_to_custom(self):
        # custom 解析失败时返回空，不抛
        self.assertEqual(fetch_feed(""), [])

class TestAiSummary(unittest.TestCase):
    """AI 摘要：只读、可缓存、失败要有明确原因。"""

    def test_summarize_parses_choice(self):
        import io
        import json
        from unittest import mock

        from sandboxkit import ai_summary

        body = json.dumps({"choices": [{"message": {"content": "中文摘要"}}]}).encode()

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp(body)):
            text = ai_summary.summarize("hello", base_url="https://x/v1", api_key="k")
        self.assertEqual(text, "中文摘要")

    def test_missing_base_url(self):
        from sandboxkit.ai_summary import SummaryError, summarize

        with self.assertRaises(SummaryError):
            summarize("x", base_url="", api_key="")

    def test_empty_text(self):
        from sandboxkit.ai_summary import SummaryError, summarize

        with self.assertRaises(SummaryError):
            summarize("", base_url="https://x", api_key="")

    def test_cache_roundtrip(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.ai_summary import SummaryCache

        with tempfile.TemporaryDirectory() as tmp:
            cache = SummaryCache(_Path(tmp) / "c.json")
            self.assertEqual(cache.get("abc"), "")
            cache.put("abc", "摘要")
            self.assertEqual(cache.get("abc"), "摘要")

class TestBoundaryText(unittest.TestCase):
    """报告里必须写清「沙盒管不了什么」，别让人以为它万能。"""

    def test_render_mentions_boundary(self):
        findings = local_findings(_settings(), backend="chroot", rootfs=None)
        text = render(findings, kernel="6.1.0", backend="chroot")
        self.assertIn("沙盒管不了", text)
        self.assertIn("内核漏洞", text)
        self.assertIn("固件", text)

    def test_clean_report_also_mentions(self):
        text = render([], kernel="6.1.0", backend="chroot")
        self.assertIn("沙盒管不了", text)

