# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.guard import ConcurrencyGuard, RateLimiter  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = RateLimiter(3, clock=self.clock)

    def test_allows_up_to_limit(self):
        for _ in range(3):
            allowed, _retry = self.limiter.allow("u1")
            self.assertTrue(allowed)

    def test_denies_over_limit(self):
        for _ in range(3):
            self.limiter.allow("u1")
        allowed, retry = self.limiter.allow("u1")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)

    def test_window_slides(self):
        for _ in range(3):
            self.limiter.allow("u1")
        self.clock.advance(61)
        allowed, _retry = self.limiter.allow("u1")
        self.assertTrue(allowed)

    def test_keys_are_independent(self):
        for _ in range(3):
            self.limiter.allow("u1")
        allowed, _retry = self.limiter.allow("u2")
        self.assertTrue(allowed)

    def test_zero_means_unlimited(self):
        limiter = RateLimiter(0)
        for _ in range(100):
            self.assertTrue(limiter.allow("u1")[0])

    def test_reset(self):
        for _ in range(3):
            self.limiter.allow("u1")
        self.limiter.reset("u1")
        self.assertTrue(self.limiter.allow("u1")[0])

    def test_pending_counts(self):
        self.limiter.allow("u1")
        self.assertEqual(self.limiter.pending("u1"), 1)
        self.assertEqual(self.limiter.pending("nobody"), 0)


class TestConcurrencyGuard(unittest.TestCase):
    def test_limit(self):
        guard = ConcurrencyGuard(2)
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.acquire())
        self.assertFalse(guard.acquire())
        self.assertEqual(guard.active, 2)

    def test_release_frees_slot(self):
        guard = ConcurrencyGuard(1)
        self.assertTrue(guard.acquire())
        guard.release()
        self.assertTrue(guard.acquire())

    def test_release_below_zero_is_safe(self):
        guard = ConcurrencyGuard(1)
        guard.release()
        self.assertEqual(guard.active, 0)

    def test_zero_means_unlimited(self):
        guard = ConcurrencyGuard(0)
        for _ in range(50):
            self.assertTrue(guard.acquire())
        self.assertEqual(guard.active, 50)


class TestGuardSharing(unittest.TestCase):
    """with_settings 会克隆 service，闸门必须共享，否则限流形同虚设。"""

    def test_clone_shares_limiter_and_concurrency(self):
        import tempfile
        from pathlib import Path

        from sandboxkit.backends import LocalBackend
        from sandboxkit.config import Settings
        from sandboxkit.service import SandboxService

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                backend="local",
                allow_local_backend=True,
                max_runs_per_minute=2,
                max_concurrent_runs=2,
            )
            service = SandboxService(settings, LocalBackend(), Path(tmp))
            clone = service.with_settings(settings)
            self.assertIs(service.limiter, clone.limiter)
            self.assertIs(service.concurrency, clone.concurrency)


if __name__ == "__main__":
    unittest.main()
