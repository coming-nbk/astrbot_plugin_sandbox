# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.limits import (  # noqa: E402
    Limits,
    build_env,
    kill_process_group,
    process_group_cpu_seconds,
    run,
)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_echo(self):
        result = run(
            ["/bin/sh", "-c", "echo hello"],
            cwd=self.cwd,
            limits=Limits(timeout=5),
        )
        self.assertTrue(result.ok)
        self.assertIn("hello", result.stdout)

    def test_exit_code(self):
        result = run(
            ["/bin/sh", "-c", "exit 3"], cwd=self.cwd, limits=Limits(timeout=5)
        )
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.ok)

    def test_timeout_is_killed(self):
        result = run(
            ["/bin/sh", "-c", "sleep 10"], cwd=self.cwd, limits=Limits(timeout=0.5)
        )
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration, 5)

    def test_output_truncated(self):
        result = run(
            ["/bin/sh", "-c", "yes 0123456789"],
            cwd=self.cwd,
            limits=Limits(timeout=1.0, max_output_bytes=200),
        )
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode()), 200)

    def test_missing_binary(self):
        result = run(
            ["/nonexistent/binary"], cwd=self.cwd, limits=Limits(timeout=5)
        )
        self.assertFalse(result.ok)
        self.assertIn("无法启动", result.error)

    def test_cwd_is_respected(self):
        (self.cwd / "here.txt").write_text("x")
        result = run(
            ["/bin/sh", "-c", "ls"], cwd=self.cwd, limits=Limits(timeout=5)
        )
        self.assertIn("here.txt", result.stdout)

    def test_env_is_whitelisted(self):
        env = build_env(str(self.cwd), {"FOO": "bar"})  # quickcheck:ignore - 演示用变量名
        self.assertEqual(env["HOME"], str(self.cwd))
        self.assertEqual(env["FOO"], "bar")  # quickcheck:ignore - 演示用变量名
        self.assertNotIn("ASTBOT_TOKEN", env)

    def test_env_does_not_leak_host_secrets(self):
        import os

        os.environ["SECRET_KEY_FOR_TEST"] = "leak"
        try:
            env = build_env(str(self.cwd))
            self.assertNotIn("SECRET_KEY_FOR_TEST", env)
        finally:
            os.environ.pop("SECRET_KEY_FOR_TEST", None)

    def test_max_cores_limits_affinity(self):
        result = run(
            [sys.executable, "-c", "import os; print(len(os.sched_getaffinity(0)))"],
            cwd=self.cwd,
            limits=Limits(timeout=10, max_cores=1),
        )
        if not result.ok:
            self.skipTest("当前环境不支持设置亲和性")
        self.assertEqual(result.stdout.strip(), "1")

    def test_memory_watchdog_kills_group(self):
        code = "x = bytearray(200 * 1024 * 1024); import time; time.sleep(5)"
        result = run(
            [sys.executable, "-c", code],
            cwd=self.cwd,
            limits=Limits(
                timeout=15,
                memory_mb=0,
                memory_watchdog_mb=48,
                watchdog_interval=0.05,
            ),
        )
        if not result.memory_killed and result.exit_code not in (0, -9, 137):
            self.skipTest(f"当前环境无法分配测试内存：{result.stderr[:80]}")
        self.assertTrue(result.memory_killed)
        self.assertLess(result.duration, 10)

    def test_memory_rlimit_alone(self):
        result = run(
            [sys.executable, "-c", "x = bytearray(200 * 1024 * 1024)"],
            cwd=self.cwd,
            limits=Limits(timeout=15, memory_mb=64),
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.memory_killed)

    def test_cpu_watchdog_kills_busy_loop(self):
        probe = subprocess.Popen(
            ["/bin/sh", "-c", "while :; do :; done"], start_new_session=True
        )
        try:
            time.sleep(0.6)
            used = process_group_cpu_seconds(probe.pid)
        finally:
            probe.kill()
            probe.wait(timeout=5)
        if used < 0.3:
            self.skipTest("当前环境把 CPU 记在 tracer 上，测不出进程组占用")

        result = run(
            [sys.executable, "-c", "while True: pass"],
            cwd=self.cwd,
            limits=Limits(
                timeout=30,
                memory_mb=0,
                cpu_percent_limit=30,
                cpu_burst_seconds=1,
                watchdog_interval=0.05,
            ),
        )
        self.assertTrue(result.cpu_killed)
        self.assertLess(result.duration, 15)

    def test_cpu_watchdog_can_be_bypassed(self):
        result = run(
            [
                sys.executable,
                "-c",
                "import time\nt = time.time()\nwhile time.time() - t < 2: pass",
            ],
            cwd=self.cwd,
            limits=Limits(
                timeout=15,
                memory_mb=0,
                cpu_percent_limit=10,
                cpu_burst_seconds=1,
                bypass_cpu=True,
            ),
        )
        self.assertFalse(result.cpu_killed)
        self.assertTrue(result.ok)

    def test_cpu_watchdog_ignores_idle_process(self):
        result = run(
            ["/bin/sh", "-c", "sleep 1.5; echo done"],
            cwd=self.cwd,
            limits=Limits(
                timeout=10,
                memory_mb=0,
                cpu_percent_limit=90,
                cpu_burst_seconds=1.0,
            ),
        )
        self.assertFalse(result.cpu_killed)
        self.assertIn("done", result.stdout)

    def test_kill_refuses_own_process_group(self):
        # 自己的进程组绝对不能杀，否则宿主跟着一起走
        self.assertFalse(kill_process_group(os.getpid()))

    def test_kill_child_group(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
        try:
            self.assertTrue(kill_process_group(proc.pid))
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_kill_missing_process_is_false(self):
        self.assertFalse(kill_process_group(999999))

    def test_watchdog_cpu_logic(self):
        """不依赖真实 CPU 计数：把采样函数换成递增序列，验证判定与杀进程。"""
        import sandboxkit.limits as limits_module

        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
        original_cpu = limits_module.process_group_cpu_seconds
        original_rss = limits_module.process_group_rss_kb
        counter = {"n": 0.0}

        def fake_cpu(_pgid):
            counter["n"] += 0.25
            return counter["n"]

        try:
            limits_module.process_group_cpu_seconds = fake_cpu
            limits_module.process_group_rss_kb = lambda _pgid: 0
            watchdog = limits_module._ResourceWatchdog(
                proc.pid,
                memory_limit_kb=0,
                cpu_percent_limit=50,
                cpu_burst_seconds=0.2,
                interval=0.05,
            )
            watchdog.start()
            deadline = time.time() + 5
            while time.time() < deadline and not watchdog.triggered_cpu:
                time.sleep(0.05)
            watchdog.stop()
            watchdog.join(timeout=2)
            self.assertTrue(watchdog.triggered_cpu)
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            limits_module.process_group_cpu_seconds = original_cpu
            limits_module.process_group_rss_kb = original_rss
            if proc.poll() is None:
                proc.kill()

    def test_to_text_marks_truncation(self):
        result = run(
            ["/bin/sh", "-c", "echo hi"], cwd=self.cwd, limits=Limits(timeout=5)
        )
        text = result.to_text(1000)
        self.assertIn("exit=0", text)
        self.assertIn("hi", text)


if __name__ == "__main__":
    unittest.main()

class TestChildLock(unittest.TestCase):
    """子进程必须无条件锁住提权和 ptrace。"""

    def test_lock_child_always_called(self):
        from unittest import mock

        from sandboxkit import limits as L

        called = []
        with mock.patch.object(L, "lock_child", lambda: called.append("lock")):
            setup = L._make_preexec(L.Limits(harden=False), None)
            with mock.patch.object(L.os, "umask", lambda _m: None):
                setup()
        self.assertEqual(called, ["lock"])

    def test_drop_caps_only_in_harden_mode(self):
        from unittest import mock

        from sandboxkit import limits as L

        caps = []
        with mock.patch.object(L, "lock_child", lambda: None), \
                mock.patch.object(L, "drop_caps", lambda: caps.append("drop")):
            with mock.patch.object(L.os, "umask", lambda _m: None):
                L._make_preexec(L.Limits(harden=False), None)()
                self.assertEqual(caps, [])
                L._make_preexec(L.Limits(harden=True), None)()
        self.assertEqual(caps, ["drop"])


class TestEnvIsolation(unittest.TestCase):
    """不能把宿主环境变量带进沙盒。"""

    def test_minimal_env_when_none(self):
        from sandboxkit import limits as L

        env = L._BASE_ENV
        self.assertIn("PATH", env)
        self.assertNotIn("HOME", env)
        self.assertNotIn("ASTRBOT_TOKEN", env)

    def test_build_env_whitelist(self):
        import os
        from unittest import mock

        from sandboxkit import limits as L

        with mock.patch.dict(os.environ, {"SECRET_KEY": "xxx", "PATH": "/bin"}, clear=False):
            env = L.build_env("/home/sandbox")
        self.assertNotIn("SECRET_KEY", env)
        self.assertEqual(env["HOME"], "/home/sandbox")

