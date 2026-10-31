# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.env import Runtime, advice, detect  # noqa: E402


class TestDetect(unittest.TestCase):
    def test_returns_runtime(self):
        runtime = detect()
        self.assertIsInstance(runtime, Runtime)
        self.assertIsInstance(runtime.in_container, bool)
        self.assertIsInstance(runtime.container_kind, str)
        self.assertIsInstance(runtime.notes, tuple)

    def test_to_dict_is_json_safe(self):
        runtime = detect()
        data = runtime.to_dict()
        self.assertIn("in_container", data)
        self.assertIsInstance(data["notes"], list)


class TestAdvice(unittest.TestCase):
    def _runtime(self, **overrides) -> Runtime:
        base = dict(
            in_container=True,
            container_kind="docker",
            is_root=True,
            has_bwrap=False,
            has_proot=False,
            has_docker_cli=True,
            docker_socket="/var/run/docker.sock",
            notes=(),
        )
        base.update(overrides)
        return Runtime(**base)

    def test_container_with_bwrap(self):
        tips = advice(self._runtime(has_bwrap=True))
        self.assertTrue(any("bwrap" in tip for tip in tips))

    def test_container_root_without_bwrap(self):
        tips = advice(self._runtime())
        self.assertTrue(any("chroot" in tip for tip in tips))

    def test_socket_without_cli(self):
        tips = advice(self._runtime(has_docker_cli=False))
        self.assertTrue(any("docker CLI" in tip for tip in tips))

    def test_socket_with_cli_mentions_reuse(self):
        tips = advice(self._runtime())
        self.assertTrue(any("docker_reuse" in tip for tip in tips))

    def test_no_isolation_at_all(self):
        tips = advice(
            self._runtime(
                is_root=False, has_bwrap=False, has_proot=False, has_docker_cli=False
            )
        )
        self.assertTrue(any("local" in tip for tip in tips))

    def test_outside_container_has_no_noise(self):
        tips = advice(
            self._runtime(
                in_container=False, container_kind="none", docker_socket=""
            )
        )
        self.assertEqual(tips, [])


if __name__ == "__main__":
    unittest.main()
