# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.audit import AuditLog  # noqa: E402
from sandboxkit.backends import LocalBackend  # noqa: E402
from sandboxkit.bootstrap import safe_extract  # noqa: E402
from sandboxkit.config import Settings  # noqa: E402
from sandboxkit.service import SandboxService  # noqa: E402
from sandboxkit.session import SandboxError, Session  # noqa: E402
from sandboxkit.webapi import is_json_request, same_origin  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(
        enabled=True,
        backend="local",
        allow_all=True,
        timeout_seconds=5,
        quota_bytes=1024 * 1024,
        import_max_bytes=64 * 1024,
        max_output_bytes=4096,
        reply_max_chars=2000,
        audit_enabled=False,
        max_procs=0,
        max_runs_per_minute=0,
    )
    base.update(overrides)
    return Settings(**base)


class TestLocalBackendFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_refused_without_opt_in(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        out = service.handle(user_id="u1", group_id="", sub="run", arg="echo hi")
        self.assertIn("拒绝", out[0].text)
        self.assertNotIn("hi", out[0].text)

    def test_python_refused_without_opt_in(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        out = service.handle(user_id="u1", group_id="", sub="py", arg="print(1)")
        self.assertIn("拒绝", out[0].text)

    def test_listing_still_works(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        out = service.handle(user_id="u1", group_id="", sub="ls", arg="")
        self.assertNotIn("拒绝", out[0].text)

    def test_execute_reports_error(self):
        service = SandboxService(_settings(), LocalBackend(), self.sessions)
        session = service.session("u1")
        result = service.execute(session, ["/bin/sh", "-c", "echo hi"])
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.error)


class TestQuotaGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_over_quota_blocks_exec(self):
        settings = _settings(allow_local_backend=True, quota_bytes=100)
        service = SandboxService(settings, LocalBackend(), self.sessions)
        session = service.session("u1")
        session.ensure()
        (session.dir / "big.bin").write_bytes(b"0" * 500)
        result = service.execute(session, ["/bin/sh", "-c", "echo hi"])
        self.assertFalse(result.ok)
        self.assertIn("配额", result.error)

    def test_growth_is_caught_on_next_run(self):
        settings = _settings(
            allow_local_backend=True, quota_bytes=1024, quota_check_seconds=0
        )
        service = SandboxService(settings, LocalBackend(), self.sessions)
        session = service.session("u1")
        first = service.execute(
            session,
            [
                "/bin/sh",
                "-c",
                "i=0; while [ $i -lt 10 ]; do "
                "dd if=/dev/zero of=f$i bs=400 count=1 2>/dev/null; "
                "i=$((i+1)); done",
            ],
        )
        if not first.ok and "dd" in (first.stderr or ""):
            self.skipTest("环境里没有 dd")
        self.assertTrue(first.ok, first.stderr)
        second = service.execute(session, ["/bin/sh", "-c", "echo again"])
        self.assertFalse(second.ok)
        self.assertIn("配额", second.error)


class TestRateLimitIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_third_run_refused(self):
        settings = _settings(allow_local_backend=True, max_runs_per_minute=2)
        service = SandboxService(settings, LocalBackend(), self.sessions)
        for _ in range(2):
            service.handle(user_id="u1", group_id="", sub="run", arg="echo hi")
        out = service.handle(user_id="u1", group_id="", sub="run", arg="echo hi")
        self.assertIn("频繁", out[0].text)


class TestFilePermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_dirs_are_private(self):
        session = Session(self.root, "u1")
        session.ensure()
        for path in (session.dir, session.dir / "imports", session.dir / "tmp"):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700, f"{path} 权限是 {oct(mode)}")

    def test_imported_file_is_private(self):
        session = Session(self.root, "u1")
        session.ensure()
        source = self.root / "a.txt"
        source.write_text("x")
        target = session.import_host_file(source, max_bytes=100, quota=10 ** 6)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_script_is_private(self):
        session = Session(self.root, "u1")
        rel = session.prepare_script("print(1)")
        mode = stat.S_IMODE((session.dir / rel).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_audit_log_is_private(self):
        log = AuditLog(self.root / "audit.jsonl", max_bytes=1024, enabled=True)
        log.write("test", user="u1")
        mode = stat.S_IMODE((self.root / "audit.jsonl").stat().st_mode)
        self.assertEqual(mode, 0o600)


class TestDirImportCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = Session(self.root, "u1")
        self.session.ensure()
        self.source = self.root / "many"
        self.source.mkdir()
        for index in range(5):
            (self.source / f"f{index}.txt").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_count_cap(self):
        with self.assertRaises(SandboxError):
            self.session.import_host_file(
                self.source, max_bytes=10 ** 6, quota=10 ** 6, max_files=3
            )

    def test_within_cap(self):
        target = self.session.import_host_file(
            self.source, max_bytes=10 ** 6, quota=10 ** 6, max_files=10
        )
        self.assertTrue(target.is_dir())

    def test_deny_globs_applied_inside_dir(self):
        (self.source / ".env").write_text("TOKEN=1")
        target = self.session.import_host_file(
            self.source,
            max_bytes=10 ** 6,
            quota=10 ** 6,
            deny_globs=[".env"],
        )
        self.assertFalse((target / ".env").exists())


class TestExtractStripsSuid(unittest.TestCase):
    def test_setuid_bit_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "suid.tar.gz"
            payload = b"#!/bin/sh\n"
            info = tarfile.TarInfo("bin/su")
            info.size = len(payload)
            info.mode = 0o4755
            with tarfile.open(archive, "w:gz") as tar:
                tar.addfile(info, io.BytesIO(payload))
            dest = base / "out"
            dest.mkdir()
            safe_extract(archive, dest)
            mode = stat.S_IMODE((dest / "bin" / "su").stat().st_mode)
            self.assertEqual(mode & 0o4000, 0)
            self.assertEqual(mode & 0o2000, 0)


class TestWebRequestChecks(unittest.TestCase):
    def test_same_origin(self):
        self.assertTrue(same_origin("http://127.0.0.1:6185", "127.0.0.1:6185"))
        # quickcheck:ignore - 测试用的假域名
        self.assertTrue(same_origin("https://bot.example.com", "bot.example.com"))  # quickcheck:ignore - 测试用假域名
        self.assertFalse(same_origin("https://evil.example.com", "bot.example.com"))  # quickcheck:ignore - 测试用假域名
        self.assertFalse(same_origin("null", "bot.example.com"))  # quickcheck:ignore - 测试用假域名
        self.assertFalse(same_origin("", "bot.example.com"))  # quickcheck:ignore - 测试用假域名
        self.assertFalse(same_origin("http://bot.example.com", ""))  # quickcheck:ignore - 测试用假域名

    def test_json_content_type(self):
        self.assertTrue(is_json_request("application/json"))
        self.assertTrue(is_json_request("application/json; charset=utf-8"))
        self.assertFalse(is_json_request("text/plain"))
        self.assertFalse(is_json_request("application/x-www-form-urlencoded"))
        self.assertFalse(is_json_request(None))


if __name__ == "__main__":
    unittest.main()

class TestNoHostEval(unittest.TestCase):
    """绝对不能在宿主 Python 里 eval/exec 用户输入。

    聊天里那种「e<python_code> 直接一手 eval 沙盒逃逸」的玩法，
    前提就是插件把代码 eval 在自己的进程里。我们不允许出现这种写法：
    所有代码都是写进沙盒文件、在 chroot 里用 python3 跑的。
    """

    FORBIDDEN = (
        (r"(?<![\w.$])eval\s*\(", "eval()"),
        (r"(?<![\w.$])exec\s*\(", "exec()"),
        (r"os\.system\s*\(", "os.system()"),
        (r"shell\s*=\s*True", "shell=True"),
        (r"pickle\.loads\s*\(", "pickle.loads()"),
    )

    def test_sources_have_no_dangerous_calls(self):
        import re

        root = Path(__file__).resolve().parents[1]
        files = [root / "main.py"] + sorted((root / "sandboxkit").rglob("*.py"))
        hits = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern, label in self.FORBIDDEN:
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    hits.append(f"{path.name}:{line} {label}")
        self.assertEqual(hits, [], "宿主进程里出现了危险调用：" + "；".join(hits))

    def test_python_runs_from_file_inside_sandbox(self):
        """确认 /sbx py 是写文件 + 沙盒内执行，不是宿主 eval。"""
        import re

        root = Path(__file__).resolve().parents[1]
        service = (root / "sandboxkit" / "service.py").read_text(encoding="utf-8")
        block = re.search(r"def _run_python.*?return text", service, re.S).group(0)
        self.assertIn("prepare_script", block)
        self.assertIn("self.execute(", block)
        self.assertNotIn("eval(", block)

