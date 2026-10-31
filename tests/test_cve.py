# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CVE 知识库 + 搜索桥接。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.cve_store import CveError, CveStore  # noqa: E402
from sandboxkit import search_bridge  # noqa: E402


class TestCveStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CveStore(Path(self.tmp.name) / "cve.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_list(self):
        self.store.upsert(cve_id="CVE-2024-1086", title="nf_tables 提权", severity="high")
        items = self.store.all()
        self.assertEqual(len(items), 1)
        self.assertIn("CVE-2024-1086", self.store.render())

    def test_upsert_replaces(self):
        self.store.upsert(cve_id="CVE-1", severity="low")
        self.store.upsert(cve_id="CVE-1", severity="high")
        items = self.store.all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["severity"], "high")

    def test_rejects_bad_severity(self):
        with self.assertRaises(CveError):
            self.store.upsert(cve_id="CVE-1", severity="critical")

    def test_rejects_bad_regex(self):
        with self.assertRaises(CveError):
            self.store.upsert(cve_id="CVE-1", affects="^5.15(")

    def test_rejects_empty_id(self):
        with self.assertRaises(CveError):
            self.store.upsert(cve_id="")

    def test_matching_by_kernel(self):
        self.store.upsert(cve_id="CVE-OLD", affects=r"^5\.", severity="high")
        self.store.upsert(cve_id="CVE-NEW", affects=r"^6\.", severity="high")
        self.store.upsert(cve_id="CVE-ANY", affects="", severity="low")
        matched = self.store.matching(kernel="5.15.0-generic", backend="chroot")
        ids = [item["id"] for item in matched]
        self.assertIn("CVE-OLD", ids)
        self.assertIn("CVE-ANY", ids)
        self.assertNotIn("CVE-NEW", ids)

    def test_remove(self):
        self.store.upsert(cve_id="CVE-1")
        self.assertTrue(self.store.remove("CVE-1"))
        self.assertFalse(self.store.remove("CVE-1"))

    def test_broken_file_is_empty(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(CveStore(path).all(), [])


class TestSearchBridge(unittest.TestCase):
    def _write_crawler(self, body: str) -> Path:
        path = Path(self.tmp.name) / "crawler.py"
        path.write_text(body, encoding="utf-8")
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_explicit_path(self):
        path = self._write_crawler("async def crawl(*a, **k):\n    return []\n")
        self.assertEqual(search_bridge.find_crawler(str(path)), path)
        self.assertEqual(search_bridge.find_crawler(str(path.parent)), path)

    def test_find_missing_returns_none(self):
        self.assertIsNone(search_bridge.find_crawler(str(Path(self.tmp.name) / "nope")))

    def test_search_filters_and_limits(self):
        path = self._write_crawler(
            "async def crawl(keyword, **kwargs):\n"
            "    return [\n"
            "        {'title': 'a', 'url': 'https://a', 'snippet': 'x'},\n"
            "        {'title': 'b', 'url': 'javascript:alert(1)', 'snippet': ''},\n"
            "        {'title': 'c', 'url': 'https://c'},\n"
            "    ]\n"
        )
        results, error = asyncio.run(
            search_bridge.search("cve", crawler_path=path, limit=5)
        )
        self.assertEqual(error, "")
        self.assertEqual([item["url"] for item in results], ["https://a", "https://c"])

    def test_search_without_crawl_reports(self):
        path = self._write_crawler("VALUE = 1\n")
        results, error = asyncio.run(search_bridge.search("x", crawler_path=path))
        self.assertEqual(results, [])
        self.assertIn("crawl", error)

    def test_render(self):
        text = search_bridge.render(
            [{"title": "T", "url": "https://u", "snippet": "S"}], "kw"
        )
        self.assertIn("https://u", text)
        self.assertIn("kw", text)

class TestGatewayConfig(unittest.TestCase):
    """读搜索插件的代理配置，不改它的文件。"""

    def test_load_gateway(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps({
                "proxy_gateway": "gw.example.com:8080",
                "proxy_username": "u-{country}-{session}",
                "proxy_password": "p",
                "proxy_country": "US",
                "proxy_scheme": "http",
                "proxy_rotate_per_request": True,
            }), encoding="utf-8")
            gw = search_bridge.load_gateway(path)
            self.assertEqual(gw["gateway"], "gw.example.com:8080")
            self.assertTrue(gw["rotate"])
            self.assertEqual(gw["country"], "US")

    def test_empty_gateway_returns_none(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps({"proxy_gateway": ""}), encoding="utf-8")
            self.assertIsNone(search_bridge.load_gateway(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(search_bridge.load_gateway(None))
        self.assertIsNone(search_bridge.load_gateway("/nope/cfg.json"))

class TestCrawlerRelativeImport(unittest.TestCase):
    """那个插件的 crawler.py 用的是绝对导入 `from fingerprint import ...`。"""

    def test_loads_when_sibling_module_exists(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fingerprint.py").write_text(
                "def build_headers():\n    return {'ua': 'x'}\n", encoding="utf-8"
            )
            (root / "crawler.py").write_text(
                "from fingerprint import build_headers\n"
                "async def crawl(keyword, **kwargs):\n"
                "    return [{'title': build_headers()['ua'], 'url': 'https://a'}]\n",
                encoding="utf-8",
            )
            results, error = asyncio.run(
                search_bridge.search("x", crawler_path=root / "crawler.py")
            )
            self.assertEqual(error, "")
            self.assertEqual(results[0]["title"], "x")

    def test_sys_path_not_polluted(self):
        import tempfile
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crawler.py").write_text("async def crawl(*a, **k):\n    return []\n", encoding="utf-8")
            before = list(sys.path)
            search_bridge.load_crawler(root / "crawler.py")
            self.assertEqual(sys.path, before)

class TestPackageVersionMatching(unittest.TestCase):
    """升级完沙盒包之后，pkg: 条件的条目应该自动消失。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.store = CveStore(Path(self.tmp.name) / "cve.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_vulnerable_version_matches(self):
        self.store.upsert(cve_id="CVE-1", affects="pkg:openssl<3.0.13")
        hit = self.store.matching(
            kernel="6.1.0", backend="chroot", packages={"openssl": "3.0.2-0ubuntu1"}
        )
        self.assertEqual([item["id"] for item in hit], ["CVE-1"])

    def test_fixed_version_drops_out(self):
        self.store.upsert(cve_id="CVE-1", affects="pkg:openssl<3.0.13")
        hit = self.store.matching(
            kernel="6.1.0", backend="chroot", packages={"openssl": "3.0.13-0ubuntu3"}
        )
        self.assertEqual(hit, [])

    def test_missing_package_does_not_match(self):
        self.store.upsert(cve_id="CVE-1", affects="pkg:openssl<3.0.13")
        self.assertEqual(
            self.store.matching(kernel="6.1.0", backend="chroot", packages={}), []
        )

    def test_kernel_regex_still_works(self):
        self.store.upsert(cve_id="CVE-2", affects=r"^5\.15")
        self.assertEqual(
            self.store.matching(kernel="5.15.0-generic", backend="chroot"), 
            [self.store.all()[0]],
        )


class TestPkgInfo(unittest.TestCase):
    def test_parse_dpkg_status(self):
        import tempfile

        from sandboxkit.pkginfo import installed_versions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "var/lib/dpkg").mkdir(parents=True)
            (root / "var/lib/dpkg/status").write_text(
                "Package: openssl\nVersion: 3.0.2-0ubuntu1\n\n"
                "Package: libc6\nVersion: 2.35-0ubuntu3\n\n",
                encoding="utf-8",
            )
            versions = installed_versions(root)
            self.assertEqual(versions["openssl"], "3.0.2-0ubuntu1")
            self.assertEqual(versions["libc6"], "2.35-0ubuntu3")

    def test_compare_and_satisfies(self):
        from sandboxkit.pkginfo import compare, satisfies

        self.assertEqual(compare("1.2.3", "1.2.10"), -1)
        self.assertEqual(compare("2.0", "2.0"), 0)
        self.assertTrue(satisfies("1.2.3", "<1.2.10"))
        self.assertFalse(satisfies("1.2.10", "<1.2.10"))
        self.assertTrue(satisfies("3.0.13", ">=3.0.13"))

