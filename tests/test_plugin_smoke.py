# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""插件冒烟测试：把 main.py 真正 import 进来跑一遍。

用 tests/stubs 里的 astrbot / quart 桩。它能证明「插件自己的代码路径通」，
不能证明「真实 AstrBot 的行为一致」—— 桩是照官方文档的签名写的。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STUBS = Path(__file__).resolve().parent / "stubs"
sys.path.insert(0, str(STUBS))
sys.path.insert(0, str(PLUGIN_ROOT))


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "sandbox_plugin_main", PLUGIN_ROOT / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPluginSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SANDBOX_TEST_DATA"] = self.tmp.name
        self.main = _load_plugin_module()
        self.star = sys.modules["astrbot.api.star"]
        self.events = sys.modules["astrbot.api.event"]
        self.web = sys.modules["astrbot.api.web"]

        # 桩里的 request 是模块级单例，每个用例前复位
        self.web.request.method = "GET"
        self.web.request.query.clear()
        self.web.request.headers = {}
        self.web.request._json = None

        self.context = self.star.Context()
        self.plugin = self.main.SandboxPlugin(
            self.context,
            {
                "enabled": True,
                "backend": "local",
                "allow_local_backend": True,
                "allow_all": True,
                "auto_bootstrap": False,
                "worker_isolation": False,
                "max_runs_per_minute": 0,
                "timeout_seconds": 10,
                "memory_mb": 0,
                "audit_enabled": True,
            },
        )
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    def _event(self, message: str, sender: str = "u1", group: str = ""):
        return self.events.AstrMessageEvent(message, sender, group)

    # ---------- 启动与路由 ----------

    async def test_initialised_and_registered(self):
        self.assertTrue(self.plugin.ready)
        self.assertIsNotNone(self.plugin.service)
        self.assertEqual(self.plugin.backend.name, "local")
        routes = {item["route"] for item in self.context.web_apis}
        self.assertIn("/astrbot_plugin_sandbox/overview", routes)
        self.assertIn("/astrbot_plugin_sandbox/tree", routes)
        self.assertIn("/astrbot_plugin_sandbox/setup/probe", routes)
        self.assertFalse(
            any("/api/" in route for route in routes),
            "路由不应该再带 /api 前缀（文档要求 /{插件名}/...）",
        )

    # ---------- 指令 ----------

    async def test_command_run(self):
        results = [item async for item in self.plugin._dispatch(self._event("/sbx run echo hi"))]
        self.assertTrue(results)
        self.assertIn("hi", results[0].text)

    async def test_command_help(self):
        results = [item async for item in self.plugin._dispatch(self._event("/sbx"))]
        self.assertIn("run", results[0].text)

    async def test_command_alias(self):
        results = [item async for item in self.plugin._dispatch(self._event("/沙盒 ls"))]
        self.assertIn("imports", results[0].text)

    # ---------- LLM 工具 ----------

    async def test_llm_tool_returns_text(self):
        """llm_tool 必须 return 字符串（回灌给模型），不能 yield 到聊天。"""
        self.assertFalse(
            inspect.isasyncgenfunction(self.plugin.sandbox_exec),
            "sandbox_exec 不能是异步生成器，否则结果会发到聊天而不是给模型",
        )
        text = await self.plugin.sandbox_exec(self._event(""), "echo tool")
        self.assertIsInstance(text, str)
        self.assertIn("tool", text)

    async def test_llm_tool_denied_when_disabled(self):
        self.plugin.settings = self.plugin.settings.__class__(
            **{**self.plugin.settings.as_dict(), "enabled": False}
        )
        self.plugin.service.settings = self.plugin.settings
        text = await self.plugin.sandbox_exec(self._event(""), "echo x")
        self.assertIn("关闭", text)

    # ---------- Web 接口 ----------

    async def test_web_overview(self):
        response = await self.plugin._api_overview()
        payload = response.body
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "local")
        self.assertIn("health", payload)

    async def test_web_tree_and_preview(self):
        self.plugin.service.handle(
            user_id="u1", group_id="", sub="run", arg="echo hello > a.txt"
        )
        self.web.request.query.update({"user": "u1", "path": ".", "depth": "2"})
        tree = (await self.plugin._api_tree()).body
        self.assertTrue(tree["ok"])
        self.assertIn("a.txt", [row["name"] for row in tree["entries"]])

        self.web.request.query.clear()
        self.web.request.query.update({"user": "u1", "path": "a.txt"})
        preview = (await self.plugin._api_file()).body
        self.assertTrue(preview["ok"])
        self.assertIn("hello", preview["content"])

    async def test_web_write_requires_json_body(self):
        self.web.request.method = "POST"
        self.web.request._json = {"user_id": "42", "note": "测试"}
        response = await self.plugin._api_user_save()
        self.assertTrue(response.body["ok"])
        self.assertEqual(response.body["user"]["user_id"], "42")

    async def test_config_save_applies_immediately(self):
        saved = self.plugin._config_save({"timeout_seconds": 45, "chroot_uid": 0})
        self.assertTrue(saved["ok"])
        self.assertEqual(self.plugin.settings.timeout_seconds, 45)
        self.assertEqual(self.plugin.settings.chroot_uid, 0)
        self.assertEqual(self.plugin.service.settings.timeout_seconds, 45)
        self.assertIn("chroot_uid", saved["restart_required"])

    async def test_config_snapshot_lists_fields(self):
        data = (await self.plugin._api_config()).body
        self.assertTrue(data["ok"])
        keys = [row["key"] for row in data["fields"]]
        self.assertIn("chroot_uid", keys)
        self.assertIn("backend", keys)

    async def test_preferred_profile_persists(self):
        self.plugin._write_preferred_profile("full")
        self.assertEqual(self.plugin._read_preferred_profile(), "full")
        data = (await self.plugin._api_setup_probe()).body
        self.assertEqual(data["preferred"], "full")

    async def test_setup_probe(self):
        data = (await self.plugin._api_setup_probe()).body
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "local")
        self.assertIn("paths", data)

    async def test_setup_apply_and_test(self):
        self.web.request.method = "POST"
        self.web.request._json = {"user_id": "777", "quota_mb": 0}
        applied = (await self.plugin._api_setup_apply()).body
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["user"]["user_id"], "777")

        self.web.request._json = {"deep": False}
        tested = (await self.plugin._api_setup_test()).body
        self.assertTrue(tested["ok"], [c for c in tested["checks"] if not c["ok"]])

    async def test_token_mode(self):
        self.plugin.settings = self.plugin.settings.__class__(
            **{
                **self.plugin.settings.as_dict(),
                "web_auth": "token",
                "web_token": "s3cret",
            }
        )
        self.plugin.service.settings = self.plugin.settings

        self.web.request.headers = {}
        denied = await self.plugin._api_overview()
        self.assertEqual(getattr(denied, "status", None), 401)

        self.web.request.headers = {"X-Sandbox-Token": "s3cret"}
        allowed = await self.plugin._api_overview()
        self.assertTrue(allowed.body["ok"])

    async def test_health_circuit_blocks(self):
        for _ in range(5):
            self.plugin.health.record_failure("test")
        results = [item async for item in self.plugin._dispatch(self._event("/sbx ls"))]
        self.assertIn("暂停", results[0].text)


class TestStaleModuleCache(unittest.TestCase):
    """卸载重装后，进程里可能还挂着旧 sandboxkit（路径相同），必须被清掉。"""

    def test_stale_modules_are_evicted(self):
        script = r"""
import importlib.util, os, sys, tempfile, types
ROOT = os.environ["ROOT"]
sys.path.insert(0, os.path.join(ROOT, "tests", "stubs"))
os.environ["SANDBOX_TEST_DATA"] = tempfile.mkdtemp()

fake = types.ModuleType("sandboxkit")
fake.__file__ = os.path.join(ROOT, "sandboxkit", "__init__.py")
fake_config = types.ModuleType("sandboxkit.config")
fake_config.__file__ = os.path.join(ROOT, "sandboxkit", "config.py")

class OldSettings:
    pass

fake_config.Settings = OldSettings
sys.modules["sandboxkit"] = fake
sys.modules["sandboxkit.config"] = fake_config

spec = importlib.util.spec_from_file_location("m", os.path.join(ROOT, "main.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from sandboxkit.config import Settings
assert hasattr(Settings, "rootfs_image"), "旧模块没有被清掉"
assert module._MISSING_FIELDS == "", module._MISSING_FIELDS
print("OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "ROOT": str(PLUGIN_ROOT)},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn("OK", result.stdout)


class TestWithoutWebLayer(unittest.IsolatedAsyncioTestCase):
    """没有 astrbot.api.web 也不该在导入阶段炸。"""

    async def test_import_survives_missing_web_layer(self):
        saved = sys.modules.pop("astrbot.api.web", None)
        try:
            module = _load_plugin_module()
            self.assertTrue(hasattr(module, "SandboxPlugin"))
        finally:
            if saved is not None:
                sys.modules["astrbot.api.web"] = saved


if __name__ == "__main__":
    unittest.main()

class TestHostConfigSync(unittest.TestCase):
    """设置保存必须回写 AstrBot 的配置对象，否则重启被覆盖。"""

    def test_save_writes_back_to_host_config(self):
        import json
        import tempfile
        from pathlib import Path as _Path

        class FakeHost(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.saved = 0

            def save_config(self):
                self.saved += 1

        host = FakeHost({"enabled": True})
        module = _load_plugin_module()
        plugin = module.SandboxPlugin.__new__(module.SandboxPlugin)
        plugin._host_config = host
        plugin.raw_config = {}
        tmp = _Path(tempfile.mkdtemp())
        plugin._config_path = tmp / "cfg.json"
        plugin._config_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        plugin.settings = None
        plugin.service = None
        plugin.wizard = None
        plugin.data_dir = tmp

        class _Log:
            def info(self, *a, **k):
                pass

        plugin.logbook = _Log()

        plugin._config_save({"enabled": False})
        self.assertFalse(host["enabled"])
        self.assertEqual(host.saved, 1)



class TestSyncIntoHost(unittest.TestCase):
    """配置文件是准：启动时不一致就改宿主，一致则不动。"""

    def _write(self, tmp, payload):
        import json
        from pathlib import Path as _Path

        path = _Path(tmp) / "cfg.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_diff_writes_back_and_saves(self):
        import tempfile

        from sandboxkit import pluginconf

        class Host(dict):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.saved = 0

            def save_config(self):
                self.saved += 1

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"enabled": False, "timeout_seconds": 60})
            host = Host({"enabled": True, "timeout_seconds": 60})
            changed = pluginconf.sync_into_host(path, host)
            self.assertEqual(sorted(changed), ["enabled"])
            self.assertFalse(host["enabled"])
            self.assertEqual(host.saved, 1)

    def test_same_values_no_write(self):
        import tempfile

        from sandboxkit import pluginconf

        class Host(dict):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.saved = 0

            def save_config(self):
                self.saved += 1

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"enabled": True})
            host = Host({"enabled": True})
            self.assertEqual(pluginconf.sync_into_host(path, host), [])
            self.assertEqual(host.saved, 0)

    def test_missing_file_is_noop(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit import pluginconf

        with tempfile.TemporaryDirectory() as tmp:
            host = {"enabled": True}
            self.assertEqual(
                pluginconf.sync_into_host(_Path(tmp) / "nope.json", host), []
            )

class TestSecurityWatch(unittest.TestCase):
    """巡检只推新的高风险，推过一次就不再重复。"""

    def _plugin(self, findings):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.audit_sec import Finding
        from sandboxkit.config import Settings

        module = _load_plugin_module()
        plugin = module.SandboxPlugin.__new__(module.SandboxPlugin)
        tmp = tempfile.TemporaryDirectory()
        plugin.base_dir = _Path(tmp.name)
        plugin.settings = Settings(enabled=True, security_notify=True)
        plugin._pool = None
        plugin._tmp = tmp

        class FakeService:
            def security_findings(self):
                return [
                    Finding(id=key, title=title, severity="high", detail="", fix="升级")
                    for key, title in findings
                ]

        plugin.service = FakeService()
        return plugin

    def test_first_run_reports_then_dedups(self):
        import asyncio

        plugin = self._plugin([("local-1", "rootfs 有 setuid")])
        (plugin.base_dir / "notify_target.json").write_text(
            '{"target": "aiocqhttp:FriendMessage:1"}', encoding="utf-8"
        )

        first = asyncio.run(plugin._security_once())
        self.assertIn("1 项新的高风险", first)
        self.assertIn("rootfs 有 setuid", first)

        second = asyncio.run(plugin._security_once())
        self.assertEqual(second, "")
        plugin._tmp.cleanup()

    def test_no_target_no_message(self):
        import asyncio

        plugin = self._plugin([("local-1", "x")])
        self.assertEqual(asyncio.run(plugin._security_once()), "")
        plugin._tmp.cleanup()

    def test_disabled_does_nothing(self):
        import asyncio

        plugin = self._plugin([("local-1", "x")])
        plugin.settings = type(plugin.settings)(enabled=True, security_notify=False)
        (plugin.base_dir / "notify_target.json").write_text(
            '{"target": "u"}', encoding="utf-8"
        )
        self.assertEqual(asyncio.run(plugin._security_once()), "")
        plugin._tmp.cleanup()

    def test_remember_target(self):
        plugin = self._plugin([])

        class Event:
            unified_msg_origin = "aiocqhttp:FriendMessage:42"

        plugin._remember_target(Event())
        self.assertEqual(plugin._load_target(), "aiocqhttp:FriendMessage:42")
        plugin._tmp.cleanup()

class TestAiSummaryProvider(unittest.TestCase):
    """默认直接复用 AstrBot 当前对话模型，不用另配 key。"""

    def _plugin(self, provider, **settings_kwargs):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.config import Settings

        module = _load_plugin_module()
        plugin = module.SandboxPlugin.__new__(module.SandboxPlugin)
        tmp = tempfile.TemporaryDirectory()
        plugin.base_dir = _Path(tmp.name)
        plugin._tmp = tmp
        base = dict(enabled=True, ai_summary_enabled=True, ai_summary_source="current")
        base.update(settings_kwargs)
        plugin.settings = Settings(**base)

        class FakeContext:
            def get_using_provider(self):
                return provider

        plugin.context = FakeContext()

        class FakeLog:
            def info(self, *a, **k):
                pass

        plugin.logbook = FakeLog()
        return plugin

    def test_uses_current_provider(self):
        import asyncio
        from types import SimpleNamespace

        calls = []

        class Provider:
            async def text_chat(self, prompt, session_id=None, **kwargs):
                calls.append(prompt)
                return SimpleNamespace(completion_text="中文摘要")

        plugin = self._plugin(Provider())
        result = asyncio.run(plugin._summary_payload("CVE-1", "原文"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "中文摘要")
        self.assertIn("原文", calls[0])
        plugin._tmp.cleanup()

    def test_cache_hits_second_time(self):
        import asyncio
        from types import SimpleNamespace

        calls = []

        class Provider:
            async def text_chat(self, prompt, session_id=None, **kwargs):
                calls.append(1)
                return SimpleNamespace(completion_text="摘要")

        plugin = self._plugin(Provider())
        asyncio.run(plugin._summary_payload("CVE-1", "同一段"))
        second = asyncio.run(plugin._summary_payload("CVE-1", "同一段"))
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)
        plugin._tmp.cleanup()

    def test_no_provider_reports(self):
        import asyncio

        plugin = self._plugin(None)
        result = asyncio.run(plugin._summary_payload("CVE-1", "x"))
        self.assertFalse(result["ok"])
        self.assertIn("没有可用的对话模型", result["error"])
        plugin._tmp.cleanup()

    def test_disabled_reports(self):
        import asyncio

        plugin = self._plugin(None, ai_summary_enabled=False)
        result = asyncio.run(plugin._summary_payload("CVE-1", "x"))
        self.assertFalse(result["ok"])
        self.assertIn("没有打开", result["error"])
        plugin._tmp.cleanup()

class TestPlatformGate(unittest.TestCase):
    """平台白名单：留空全放，填了就只放名单里的。"""

    def _plugin(self, allowed=()):
        from sandboxkit.config import Settings

        module = _load_plugin_module()
        plugin = module.SandboxPlugin.__new__(module.SandboxPlugin)
        plugin.settings = Settings(
            enabled=True, allowed_platforms=tuple(allowed), language="zh"
        )
        return plugin

    def test_platform_from_getter(self):
        class Event:
            def get_platform_name(self):
                return "telegram"

        self.assertEqual(
            _load_plugin_module().SandboxPlugin._platform_of(Event()), "telegram"
        )

    def test_platform_falls_back_to_umo(self):
        class Event:
            unified_msg_origin = "discord:GroupMessage:123"

        self.assertEqual(
            _load_plugin_module().SandboxPlugin._platform_of(Event()), "discord"
        )

    def test_platform_unknown(self):
        class Event:
            pass

        self.assertEqual(
            _load_plugin_module().SandboxPlugin._platform_of(Event()), ""
        )

class TestExportMode(unittest.TestCase):
    """发不了文件的平台可以把 export_as_file 关掉。"""

    def test_text_only_mode(self):
        from pathlib import Path as _Path

        from sandboxkit.config import Settings

        module = _load_plugin_module()
        plugin = module.SandboxPlugin.__new__(module.SandboxPlugin)
        plugin.settings = Settings(enabled=True, export_as_file=False)
        out = module.FileOut(path=_Path("/tmp/x.txt"), name="x.txt")

        class Event:
            def plain_result(self, text):
                return ("plain", text)

            def chain_result(self, chain):
                return ("chain", chain)

        kind, text = plugin._file_result(Event(), out)
        self.assertEqual(kind, "plain")
        self.assertIn("/tmp/x.txt", text)

class TestBootstrapMarkerPath(unittest.TestCase):
    """last_bootstrap.json 必须写在 plugin_data 里，和读取方一致。

    之前写在 AstrBot 的 data/ 根目录，webapi 却从 plugin_data 读，
    结果这个文件永远读不到 —— 上架审核也是卡在这条。
    """

    def test_writer_uses_base_dir(self):
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")
        self.assertIn('Path(self.base_dir) / "last_bootstrap.json"', source)
        self.assertNotIn('Path(self.data_dir) / "last_bootstrap.json"', source)

    def test_reader_uses_base_dir(self):
        source = (Path(__file__).resolve().parent.parent / "sandboxkit" / "webapi.py").read_text("utf-8")
        self.assertIn('Path(base_dir) / "last_bootstrap.json"', source)

