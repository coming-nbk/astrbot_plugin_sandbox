# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.backends import (  # noqa: E402
    BackendError,
    ChrootBackend,
    DockerBackend,
)
from sandboxkit.config import Settings  # noqa: E402


class FakeDocker:
    """假装是 docker CLI，记录每次调用。"""

    def __init__(self, running: bool = False, fail_run: bool = False):
        self.calls: list[list[str]] = []
        self.running = running
        self.fail_run = fail_run

    def __call__(self, args, timeout=60.0):
        self.calls.append(list(args))
        head = args[0]
        if head == "inspect":
            if self.running:
                return 0, "true\n", ""
            return 1, "", "Error: No such object"
        if head == "run":
            if self.fail_run:
                return 125, "", "docker: image not found"
            self.running = True
            return 0, "container-id\n", ""
        if head == "rm":
            self.running = False
            return 0, "", ""
        if head == "ps":
            return 0, "abc123\n", ""
        if head == "exec":
            return 0, "ok\n", ""
        return 0, "", ""

    def heads(self) -> list[str]:
        return [call[0] for call in self.calls]

    def call(self, head: str) -> list[str] | None:
        for item in self.calls:
            if item[0] == head:
                return item
        return None


def _settings(**overrides) -> Settings:
    base = dict(backend="docker", docker_reuse=True, docker_idle_minutes=30)
    base.update(overrides)
    return Settings(**base)


class TestDockerReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.tmp.name) / "u1-abc12345"
        self.session_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_call_creates_container_then_execs(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        prepared = backend.prepare(
            ["echo", "hi"],
            session_dir=self.session_dir,
            settings=_settings(),
            rootfs=None,
        )
        self.assertEqual(fake.heads(), ["inspect", "rm", "run"])
        self.assertEqual(prepared.argv[0:4], ["docker", "exec", "-i", "-w"])
        self.assertIn("echo", prepared.argv)

    def test_second_call_skips_inspect(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        settings = _settings()
        backend.prepare(
            ["true"], session_dir=self.session_dir, settings=settings, rootfs=None
        )
        backend.prepare(
            ["true"], session_dir=self.session_dir, settings=settings, rootfs=None
        )
        self.assertEqual(fake.heads().count("inspect"), 1)
        self.assertEqual(fake.heads().count("run"), 1)
        second = backend.prepare(
            ["true"], session_dir=self.session_dir, settings=settings, rootfs=None
        )
        self.assertEqual(second.argv[0], "docker")
        self.assertEqual(second.argv[1], "exec")

    def test_existing_running_container_is_reused(self):
        fake = FakeDocker(running=True)
        backend = DockerBackend(runner=fake)
        backend.prepare(
            ["true"], session_dir=self.session_dir, settings=_settings(), rootfs=None
        )
        self.assertNotIn("run", fake.heads())

    def test_stateless_mode_uses_run(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        prepared = backend.prepare(
            ["echo", "hi"],
            session_dir=self.session_dir,
            settings=_settings(docker_reuse=False),
            rootfs=None,
        )
        self.assertIn("--rm", prepared.argv)
        self.assertEqual(fake.calls, [])

    def test_run_failure_raises(self):
        fake = FakeDocker(fail_run=True)
        backend = DockerBackend(runner=fake)
        with self.assertRaises(BackendError):
            backend.prepare(
                ["true"],
                session_dir=self.session_dir,
                settings=_settings(),
                rootfs=None,
            )

    def test_flags_are_hardened(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        backend.prepare(
            ["true"],
            session_dir=self.session_dir,
            settings=_settings(docker_user="65534:65534", max_cores=2),
            rootfs=None,
        )
        run_call = fake.call("run")
        self.assertIsNotNone(run_call)
        joined = " ".join(run_call or [])
        self.assertIn("--read-only", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--network none", joined)
        self.assertIn("--cpus 2", joined)
        self.assertIn("--user 65534:65534", joined)
        self.assertIn(f"{self.session_dir}:/sandbox", joined)

    def test_network_allowed(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        backend.prepare(
            ["true"],
            session_dir=self.session_dir,
            settings=_settings(allow_network=True),
            rootfs=None,
        )
        self.assertIn("--network bridge", " ".join(fake.call("run") or []))

    def test_container_name_is_sanitized(self):
        weird = Path(self.tmp.name) / "u$1 weird"
        weird.mkdir()
        backend = DockerBackend(runner=FakeDocker())
        name = backend._container_name(weird)
        self.assertTrue(name.startswith("astrbot-sbx-"))
        self.assertNotIn("$", name)
        self.assertNotIn(" ", name)


class TestChrootNeedsSandboxDir(unittest.TestCase):
    """chroot 之后看不到宿主路径，会话目录必须在 rootfs 里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rootfs = Path(self.tmp.name) / "rootfs"
        (self.rootfs / "bin").mkdir(parents=True)
        (self.rootfs / "bin" / "sh").write_text("")
        self.session = Path(self.tmp.name) / "sessions" / "u1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_sandbox_dir_raises_clear_error(self):
        backend = ChrootBackend(self.rootfs)
        with self.assertRaises(BackendError) as ctx:
            backend.prepare(
                ["/bin/sh", "-c", "true"],
                session_dir=self.session,
                settings=_settings(),
                rootfs=self.rootfs,
            )
        self.assertIn("sandbox", str(ctx.exception))

    def test_with_sandbox_dir_it_prepares(self):
        (self.rootfs / "sandbox" / self.session.name).mkdir(parents=True)
        backend = ChrootBackend(self.rootfs)
        prepared = backend.prepare(
            ["/bin/sh", "-c", "true"],
            session_dir=self.session,
            settings=_settings(),
            rootfs=self.rootfs,
        )
        self.assertIsNotNone(prepared.preexec)
        self.assertEqual(prepared.home, f"/sandbox/{self.session.name}")
        self.assertEqual(prepared.preexec_spec["kind"], "chroot")
        self.assertEqual(
            prepared.preexec_spec["cwd"], f"/sandbox/{self.session.name}"
        )

    def test_session_dir_missing_inside_rootfs(self):
        (self.rootfs / "sandbox").mkdir()
        backend = ChrootBackend(self.rootfs)
        with self.assertRaises(BackendError) as ctx:
            backend.prepare(
                ["/bin/sh", "-c", "true"],
                session_dir=self.session,
                settings=_settings(),
                rootfs=self.rootfs,
            )
        self.assertIn("找不到会话目录", str(ctx.exception))


class TestDockerCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.tmp.name) / "u1-abc12345"
        self.session_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_prune_removes_idle(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        backend.prepare(
            ["true"], session_dir=self.session_dir, settings=_settings(), rootfs=None
        )
        removed = backend.prune(idle_seconds=0)
        self.assertEqual(len(removed), 1)
        self.assertIn("rm", fake.heads())

    def test_prune_keeps_fresh(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        backend.prepare(
            ["true"], session_dir=self.session_dir, settings=_settings(), rootfs=None
        )
        self.assertEqual(backend.prune(idle_seconds=3600), [])

    def test_cleanup_orphans(self):
        fake = FakeDocker()
        backend = DockerBackend(runner=fake)
        removed = backend.cleanup_orphans()
        self.assertEqual(removed, ["abc123"])
        self.assertIn("ps", fake.heads())
        self.assertIn("rm", fake.heads())


if __name__ == "__main__":
    unittest.main()
