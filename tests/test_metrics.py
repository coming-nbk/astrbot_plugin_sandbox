# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest

from sandboxkit import metrics


class TestMetrics(unittest.TestCase):
    def test_snapshot_shape(self):
        data = metrics.snapshot("/")
        for key in (
            "cpu_count",
            "load1",
            "load_ratio",
            "mem_used_mb",
            "mem_total_mb",
            "mem_ratio",
            "disk_total_gb",
            "disk_free_gb",
            "disk_ratio",
        ):
            self.assertIn(key, data)

    def test_ratios_are_clamped(self):
        data = metrics.snapshot("/")
        for key in ("load_ratio", "mem_ratio", "disk_ratio"):
            self.assertGreaterEqual(data[key], 0.0)
            self.assertLessEqual(data[key], 1.0)

    def test_cpu_count_positive(self):
        self.assertGreaterEqual(metrics.snapshot("/")["cpu_count"], 1)

    def test_bad_path_does_not_raise(self):
        data = metrics.snapshot("/definitely/not/here")
        self.assertEqual(data["disk_total_gb"], 0.0)

    def test_load_may_be_none_but_never_fake_zero(self):
        """读不到负载时必须是 None，不能伪装成 0。"""
        from unittest import mock

        with mock.patch.object(metrics, "_load1", return_value=None):
            data = metrics.snapshot("/")
        self.assertIsNone(data["load1"])
        self.assertEqual(data["load_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
