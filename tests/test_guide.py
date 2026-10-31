# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""使用说明必须是「按当前配置动态生成」，不能写死。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.guide import GuideTracker, build_guide  # noqa: E402


def _guide(**overrides):
    base = dict(
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
        caps={},
    )
    base.update(overrides)
    return build_guide(**base)


class TestGuideContent(unittest.TestCase):
    def test_reflects_real_limits(self):
        text = _guide(quota_bytes=64 * 1024 * 1024, timeout_seconds=5, memory_mb=128)
        self.assertIn("64MB", text)
        self.assertIn("5 秒", text)
        self.assertIn("128 MB", text)

    def test_network_state_is_explicit(self):
        self.assertIn("联网：禁止", _guide(allow_network=False))
        self.assertIn("联网：允许", _guide(allow_network=True))

    def test_disabled_capability_is_hidden(self):
        text = _guide(caps={"allow_exec": False, "allow_py": False})
        # 命令清单里不该出现，但「没权限」的说明里可以提到
        self.assertNotIn("/sbx run <命令>", text)
        self.assertNotIn("/sbx py <代码>", text)
        self.assertIn("/sbx ls", text)  # 基础命令仍在
        self.assertIn("没给你 /sbx py 权限", text)

    def test_import_hidden_when_disallowed(self):
        text = _guide(allow_import=False)
        self.assertNotIn("/sbx import", text)
        self.assertIn("从宿主导入文件：禁止", text)

    def test_admin_gets_extra_commands(self):
        text = _guide(is_admin=True)
        self.assertIn("/sbx install", text)
        self.assertIn("/sbx bootstrap", text)

    def test_backend_warning_for_local(self):
        text = _guide(backend="local")
        self.assertIn("local 后端", text)


class TestGuideTracker(unittest.TestCase):
    def test_first_time_then_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = GuideTracker(Path(tmp) / "seen.json")
            self.assertFalse(tracker.seen("u1"))
            self.assertTrue(tracker.mark("u1"))
            self.assertFalse(tracker.mark("u1"))
            self.assertTrue(tracker.seen("u1"))
            self.assertFalse(tracker.seen("u2"))
