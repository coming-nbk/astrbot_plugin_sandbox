# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.limits import Limits  # noqa: E402
from sandboxkit.worker import handle, serve  # noqa: E402
from sandboxkit.worker_client import WorkerClient  # noqa: E402


class TestWorkerProtocol(unittest.TestCase):
    def test_handle_runs_command(self):
        result = handle(
            {
                "argv": ["/bin/sh", "-c", "echo hi"],
                "cwd": None,
                "env": {},
                "limits": {"timeout": 10, "memory_mb": 0, "max_output_bytes": 4096},
            }
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hi", result["stdout"])
        self.assertFalse(result["timed_out"])

    def test_handle_reports_failure(self):
        result = handle(
            {
                "argv": ["/nonexistent/binary"],
                "limits": {"timeout": 5},
            }
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue(result["error"])

    def test_serve_handles_bad_json(self):
        stdin = io.StringIO("not json\n")
        stdout = io.StringIO()
        serve(stdin, stdout)
        self.assertIn("error", json.loads(stdout.getvalue().strip()))

    def test_serve_continues_after_bad_request(self):
        stdin = io.StringIO("not json\n")
        stdout = io.StringIO()
        serve(stdin, stdout)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)


class TestWorkerClient(unittest.TestCase):
    def setUp(self):
        self.client = WorkerClient()

    def tearDown(self):
        self.client.close()

    def test_round_trip(self):
        result = self.client.run(
            ["/bin/sh", "-c", "echo hello"],
            cwd=None,
            limits=Limits(timeout=10, memory_mb=0),
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("hello", result.stdout)

    def test_reuses_process(self):
        for _ in range(3):
            result = self.client.run(
                ["/bin/sh", "-c", "true"],
                cwd=None,
                limits=Limits(timeout=10, memory_mb=0),
            )
            self.assertTrue(result.ok, result.error)
        self.assertIsNotNone(self.client._proc)
        self.assertIsNone(self.client._proc.poll())

    def test_timeout_is_reported_and_worker_survives(self):
        # worker 自己管超时，所以它仍然活着，不用重启
        result = self.client.run(
            ["/bin/sh", "-c", "sleep 10"],
            cwd=None,
            limits=Limits(timeout=0.5, memory_mb=0),
        )
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration, 5)
        self.assertIsNotNone(self.client._proc)

        again = self.client.run(
            ["/bin/sh", "-c", "echo back"],
            cwd=None,
            limits=Limits(timeout=10, memory_mb=0),
        )
        self.assertTrue(again.ok, again.error)
        self.assertIn("back", again.stdout)

    def test_output_truncated(self):
        result = self.client.run(
            ["/bin/sh", "-c", "yes abc"],
            cwd=None,
            limits=Limits(timeout=1, memory_mb=0, max_output_bytes=100),
        )
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode()), 100)

    def test_recovers_after_worker_death(self):
        self.client.run(
            ["/bin/sh", "-c", "true"],
            cwd=None,
            limits=Limits(timeout=10, memory_mb=0),
        )
        proc = self.client._proc
        self.assertIsNotNone(proc)
        proc.kill()
        proc.wait(timeout=5)

        result = self.client.run(
            ["/bin/sh", "-c", "echo x"],
            cwd=None,
            limits=Limits(timeout=5, memory_mb=0),
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("x", result.stdout)

    def test_broken_pipe_returns_error(self):
        class FakeProc:
            class stdin:
                @staticmethod
                def write(_data):
                    raise BrokenPipeError("gone")

                @staticmethod
                def flush():
                    return None

            @staticmethod
            def poll():
                return None

        self.client._ensure = lambda: FakeProc()  # type: ignore[assignment]
        result = self.client.run(
            ["/bin/sh", "-c", "true"],
            cwd=None,
            limits=Limits(timeout=5, memory_mb=0),
        )
        self.assertFalse(result.ok)
        self.assertIn("执行进程", result.error)

    def test_client_deadline_kills_worker(self):
        self.client.run(
            ["/bin/sh", "-c", "true"],
            cwd=None,
            limits=Limits(timeout=10, memory_mb=0),
        )

        def _boom(_proc, _deadline):
            raise TimeoutError("no answer")

        self.client._read_line = _boom  # type: ignore[assignment]
        result = self.client.run(
            ["/bin/sh", "-c", "true"],
            cwd=None,
            limits=Limits(timeout=1, memory_mb=0),
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(self.client._proc)

    def test_cwd_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "here.txt").write_text("x")
            result = self.client.run(
                ["/bin/sh", "-c", "ls"],
                cwd=tmp,
                limits=Limits(timeout=10, memory_mb=0),
            )
            self.assertIn("here.txt", result.stdout)

    def test_env_is_passed(self):
        result = self.client.run(
            ["/bin/sh", "-c", "echo $SANDBOX_TEST"],
            cwd=None,
            limits=Limits(timeout=10, memory_mb=0),
            env={"SANDBOX_TEST": "42", "PATH": "/bin:/usr/bin"},
        )
        self.assertIn("42", result.stdout)


class TestCrashAccounting(unittest.TestCase):
    def test_stats_start_empty(self):
        client = WorkerClient()
        try:
            stats = client.stats()
            self.assertEqual(stats["spawns"], 0)
            self.assertEqual(stats["crashes"], 0)
            self.assertFalse(stats["alive"])
        finally:
            client.close()

    def test_spawn_counted(self):
        client = WorkerClient()
        try:
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=10, memory_mb=0))
            self.assertEqual(client.stats()["spawns"], 1)
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=10, memory_mb=0))
            self.assertEqual(client.stats()["spawns"], 1)
        finally:
            client.close()

    def test_crash_callback_fires(self):
        seen = []
        client = WorkerClient(on_crash=seen.append)
        try:
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=10, memory_mb=0))

            def _boom(_proc, _deadline):
                raise EOFError("gone")

            client._read_line = _boom  # type: ignore[assignment]
            result = client.run(
                ["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=5, memory_mb=0)
            )
            self.assertFalse(result.ok)
            self.assertEqual(client.stats()["crashes"], 1)
            self.assertEqual(len(seen), 1)
        finally:
            client.close()

    def test_consecutive_resets_after_success(self):
        client = WorkerClient()
        try:
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=10, memory_mb=0))

            def _boom(_proc, _deadline):
                raise EOFError("gone")

            original = client._read_line
            client._read_line = _boom  # type: ignore[assignment]
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=5, memory_mb=0))
            self.assertEqual(client.stats()["consecutive_crashes"], 1)

            client._read_line = original  # type: ignore[assignment]
            client.run(["/bin/sh", "-c", "true"], cwd=None, limits=Limits(timeout=10, memory_mb=0))
            self.assertEqual(client.stats()["consecutive_crashes"], 0)
        finally:
            client.close()


class TestServiceWithWorker(unittest.TestCase):
    def test_service_runs_through_worker(self):
        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.service import SandboxService

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                backend="local",
                allow_local_backend=True,
                allow_all=True,
                worker_isolation=True,
                max_runs_per_minute=0,
                timeout_seconds=10,
                memory_mb=0,
            )
            client = WorkerClient()
            try:
                service = SandboxService(
                    settings, LocalBackend(), Path(tmp), worker=client
                )
                out = service.handle(user_id="u1", group_id="", sub="run", arg="echo via-worker")
                self.assertIn("via-worker", out[0].text)
            finally:
                client.close()

    def test_fallback_when_worker_broken(self):
        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.service import SandboxService

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                backend="local",
                allow_local_backend=True,
                allow_all=True,
                worker_isolation=True,
                worker_fallback=True,
                max_runs_per_minute=0,
                timeout_seconds=10,
                memory_mb=0,
            )
            client = WorkerClient(python="/nonexistent/python")
            try:
                service = SandboxService(
                    settings, LocalBackend(), Path(tmp), worker=client
                )
                out = service.handle(user_id="u1", group_id="", sub="run", arg="echo fallback")
                self.assertIn("fallback", out[0].text)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
