# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.health import HealthGuard  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestCircuit(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.guard = HealthGuard(
            failure_threshold=3,
            failure_window=60,
            cooldown=120,
            clock=self.clock,
            rss_reader=lambda: 10.0,
            thread_reader=lambda: 5,
        )

    def test_starts_ok(self):
        ok, reason = self.guard.allow()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_opens_after_threshold(self):
        self.assertFalse(self.guard.record_failure("boom"))
        self.assertFalse(self.guard.record_failure("boom"))
        self.assertTrue(self.guard.record_failure("boom"))
        ok, reason = self.guard.allow()
        self.assertFalse(ok)
        self.assertIn("暂停", reason)

    def test_closes_after_cooldown(self):
        for _ in range(3):
            self.guard.record_failure("boom")
        self.clock.advance(121)
        ok, _reason = self.guard.allow()
        self.assertTrue(ok)

    def test_window_forgets_old_failures(self):
        self.guard.record_failure("boom")
        self.guard.record_failure("boom")
        self.clock.advance(61)
        self.assertFalse(self.guard.record_failure("boom"))
        ok, _reason = self.guard.allow()
        self.assertTrue(ok)

    def test_success_prunes_but_keeps_recent(self):
        self.guard.record_failure("boom")
        self.guard.record_success()
        self.assertEqual(self.guard.status().failures, 1)
        self.clock.advance(61)
        self.guard.record_success()
        self.assertEqual(self.guard.status().failures, 0)

    def test_reset(self):
        for _ in range(3):
            self.guard.record_failure("boom")
        self.guard.reset()
        self.assertTrue(self.guard.allow()[0])
        self.assertEqual(self.guard.status().failures, 0)

    def test_status_reports_circuit(self):
        for _ in range(3):
            self.guard.record_failure("boom")
        status = self.guard.status()
        self.assertFalse(status.ok)
        self.assertTrue(status.circuit_open)
        self.assertEqual(status.rss_mb, 10.0)
        self.assertEqual(status.threads, 5)


class TestSelfChecks(unittest.TestCase):
    def test_rss_limit_blocks(self):
        guard = HealthGuard(rss_limit_mb=100, rss_reader=lambda: 250.0)
        ok, reason = guard.allow()
        self.assertFalse(ok)
        self.assertIn("内存", reason)

    def test_rss_under_limit_allows(self):
        guard = HealthGuard(rss_limit_mb=100, rss_reader=lambda: 50.0)
        self.assertTrue(guard.allow()[0])

    def test_rss_limit_disabled_by_default(self):
        guard = HealthGuard(rss_reader=lambda: 99999.0)
        self.assertTrue(guard.allow()[0])

    def test_thread_limit_blocks(self):
        guard = HealthGuard(thread_limit=10, thread_reader=lambda: 99)
        ok, reason = guard.allow()
        self.assertFalse(ok)
        self.assertIn("线程", reason)

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            switch = Path(tmp) / "DISABLED"
            guard = HealthGuard(kill_switch=switch)
            self.assertTrue(guard.allow()[0])
            switch.write_text("stop")
            ok, reason = guard.allow()
            self.assertFalse(ok)
            self.assertIn("DISABLED", reason)
            self.assertTrue(guard.status().disabled_file)


if __name__ == "__main__":
    unittest.main()
