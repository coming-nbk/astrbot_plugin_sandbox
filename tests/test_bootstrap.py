# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import sys
import tarfile
from unittest import mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.bootstrap import (  # noqa: E402
    PRESETS,
    BootstrapError,
    bootstrap,
    bootstrap_with_fallback,
    ensure_dev_nodes,
    from_docker_image,
    has_shell,
    default_url,
    is_ready,
    marker_path,
    package_manager,
    read_marker,
    resolve_rootfs,
    safe_extract,
    sha256_of,
)


from sandboxkit.bootstrap import install_packages as sandboxkit_install_packages  # noqa: E402


def _make_tar(path: Path, members) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for info, payload in members:
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)


class TestSafeExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.dest = self.base / "out"
        self.dest.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_member(self):
        info = tarfile.TarInfo("bin/hello.txt")
        payload = b"hello"
        info.size = len(payload)
        archive = self.base / "ok.tar.gz"
        _make_tar(archive, [(info, payload)])
        count = safe_extract(archive, self.dest)
        self.assertEqual(count, 1)
        self.assertEqual((self.dest / "bin" / "hello.txt").read_bytes(), b"hello")

    def test_dotdot_member_rejected(self):
        info = tarfile.TarInfo("../evil.txt")
        payload = b"evil"
        info.size = len(payload)
        archive = self.base / "bad.tar.gz"
        _make_tar(archive, [(info, payload)])
        with self.assertRaises(BootstrapError):
            safe_extract(archive, self.dest)
        self.assertFalse((self.base / "evil.txt").exists())

    def test_absolute_member_rejected(self):
        info = tarfile.TarInfo("/etc/passwd")
        payload = b"root"
        info.size = len(payload)
        archive = self.base / "abs.tar.gz"
        _make_tar(archive, [(info, payload)])
        with self.assertRaises(BootstrapError):
            safe_extract(archive, self.dest)

    def test_relative_symlink_escape_rejected(self):
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        archive = self.base / "link.tar.gz"
        _make_tar(archive, [(info, None)])
        with self.assertRaises(BootstrapError):
            safe_extract(archive, self.dest)

    def test_absolute_symlink_allowed(self):
        info = tarfile.TarInfo("bin/sh")
        info.type = tarfile.SYMTYPE
        info.linkname = "/bin/busybox"
        archive = self.base / "abslink.tar.gz"
        _make_tar(archive, [(info, None)])
        self.assertEqual(safe_extract(archive, self.dest), 1)
        self.assertTrue((self.dest / "bin" / "sh").is_symlink())

    def test_absolute_hardlink_rejected(self):
        info = tarfile.TarInfo("link")
        info.type = tarfile.LNKTYPE
        info.linkname = "/etc/passwd"
        archive = self.base / "hard.tar.gz"
        _make_tar(archive, [(info, None)])
        with self.assertRaises(BootstrapError):
            safe_extract(archive, self.dest)

    def test_relative_hardlink_inside_ok(self):
        payload = b"data"
        data = tarfile.TarInfo("./a.txt")
        data.size = len(payload)
        link = tarfile.TarInfo("./b.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "./a.txt"
        archive = self.base / "hardok.tar.gz"
        _make_tar(archive, [(data, payload), (link, None)])
        self.assertEqual(safe_extract(archive, self.dest), 2)
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"data")

    def test_device_member_skipped(self):
        info = tarfile.TarInfo("dev/null")
        info.type = tarfile.CHRTYPE
        archive = self.base / "dev.tar.gz"
        _make_tar(archive, [(info, None)])
        self.assertEqual(safe_extract(archive, self.dest), 0)


class TestPresets(unittest.TestCase):
    def test_debian_url_is_slim_rootfs(self):
        url, version = resolve_rootfs("debian", "", "x86_64")
        self.assertEqual(version, "bookworm")
        self.assertIn("dist-amd64/bookworm/slim/rootfs.tar.xz", url)

    def test_debian_arm_uses_arm64v8_branch(self):
        url, _ = resolve_rootfs("debian", "", "aarch64")
        self.assertIn("dist-arm64v8", url)

    def test_ubuntu_url(self):
        url, version = resolve_rootfs("ubuntu", "", "x86_64")
        self.assertEqual(version, "24.04")
        self.assertIn("ubuntu-base-24.04-base-amd64.tar.gz", url)

    def test_alpine_url(self):
        url, version = resolve_rootfs("alpine", "3.20.3", "x86_64")
        self.assertEqual(version, "3.20.3")
        self.assertIn("alpine-minirootfs-3.20.3-x86_64.tar.gz", url)

    def test_version_override(self):
        url, version = resolve_rootfs("ubuntu", "22.04", "aarch64")
        self.assertEqual(version, "22.04")
        self.assertIn("base-arm64.tar.gz", url)

    def test_unknown_preset_rejected(self):
        with self.assertRaises(BootstrapError):
            resolve_rootfs("gentoo", "", "x86_64")

    def test_preset_table_is_complete(self):
        for name, preset in PRESETS.items():
            self.assertEqual(name, preset.name)
            self.assertIn("{arch}", preset.template)
            self.assertTrue(preset.description)


class TestPackageManager(unittest.TestCase):
    def test_apt_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            rootfs = Path(tmp)
            (rootfs / "usr" / "bin").mkdir(parents=True)
            (rootfs / "usr" / "bin" / "apt-get").write_text("")
            self.assertEqual(package_manager(rootfs), "apt")

    def test_apk_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            rootfs = Path(tmp)
            (rootfs / "sbin").mkdir(parents=True)
            (rootfs / "sbin" / "apk").write_text("")
            self.assertEqual(package_manager(rootfs), "apk")

    def test_neither(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(package_manager(Path(tmp)), "")

    def test_install_without_packages_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = sandboxkit_install_packages(Path(tmp), [])
            self.assertTrue(ok)
            self.assertEqual(message, "")


class TestBootstrapFlow(unittest.TestCase):
    """用 file:// 跑完整条链路：下载 -> 校验 -> 解压 -> 打标记。"""

    def _fake_rootfs(self, base: Path) -> Path:
        archive = base / "rootfs.tar.gz"
        payload = b"#!/bin/sh\n"
        sh = tarfile.TarInfo("bin/sh")
        sh.size = len(payload)
        sh.mode = 0o755
        etc = tarfile.TarInfo("etc")
        etc.type = tarfile.DIRTYPE
        etc.mode = 0o755
        _make_tar(archive, [(etc, None), (sh, payload)])
        return archive

    def test_full_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_rootfs(base)
            rootfs = base / "rootfs"
            digest = sha256_of(archive)
            info = bootstrap(
                rootfs,
                preset="custom",
                version="test",
                arch="x86_64",
                url=archive.as_uri(),
                expected_sha256=digest,
                packages=[],
                log=lambda _message: None,
            )
            self.assertTrue(is_ready(rootfs))
            self.assertEqual(info.sha256, digest)
            self.assertEqual(info.preset, "custom")
            marker = read_marker(rootfs)
            self.assertIsNotNone(marker)
            self.assertEqual(marker.version, "test")

    def test_sha_mismatch_leaves_no_rootfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_rootfs(base)
            rootfs = base / "rootfs"
            with self.assertRaises(BootstrapError):
                bootstrap(
                    rootfs,
                    preset="custom",
                    url=archive.as_uri(),
                    expected_sha256="0" * 64,
                    packages=[],
                    log=lambda _message: None,
                )
            self.assertFalse(is_ready(rootfs))

    def test_second_call_reuses_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_rootfs(base)
            rootfs = base / "rootfs"
            first = bootstrap(
                rootfs,
                preset="custom",
                url=archive.as_uri(),
                expected_sha256=sha256_of(archive),
                packages=[],
                log=lambda _message: None,
            )
            archive.unlink()  # 第二次如果还想下载就会失败
            second = bootstrap(
                rootfs,
                preset="custom",
                url=archive.as_uri(),
                packages=[],
                log=lambda _message: None,
            )
            self.assertEqual(first.installed_at, second.installed_at)

    def test_bad_archive_does_not_create_rootfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "bad.tar.gz"
            archive.write_bytes(b"not a tarball")
            rootfs = base / "rootfs"
            with self.assertRaises(Exception):
                bootstrap(
                    rootfs,
                    preset="custom",
                    url=archive.as_uri(),
                    packages=[],
                    log=lambda _message: None,
                )
            self.assertFalse(is_ready(rootfs))


class TestFallback(unittest.TestCase):
    """一个源 404 不该让整个插件不可用。"""

    def setUp(self):
        patcher = mock.patch("sandboxkit.bootstrap.mirrors.resolve_all", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_rootfs_tar(self, base: Path) -> Path:
        archive = base / "good.tar.gz"
        payload = b"#!/bin/sh\n"
        sh = tarfile.TarInfo("bin/sh")
        sh.size = len(payload)
        sh.mode = 0o755
        _make_tar(archive, [(sh, payload)])
        return archive

    def test_static_source_is_tried_before_crawling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            seen: list[str] = []

            def fake_bootstrap(rootfs, *, preset, version, arch, url, **kwargs):
                seen.append(url)
                raise BootstrapError("模拟 404")

            with mock.patch("sandboxkit.bootstrap.bootstrap", side_effect=fake_bootstrap) as _b:
                with self.assertRaises(BootstrapError):
                    bootstrap_with_fallback(
                        base / "rootfs", preset="alpine", packages=[], log=lambda _m: None
                    )
            self.assertIn("dl-cdn.alpinelinux.org", seen[0])
            self.assertTrue(all("tuna" not in url for url in seen))

    def test_crawled_source_is_used_when_static_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            crawled = [("alpine", "https://mirrors.tuna.tsinghua.edu.cn/alpine/x/alpine-minirootfs-9.9.9-x86_64.tar.gz")]

            def fake_bootstrap(rootfs, *, preset, version, arch, url, **kwargs):
                if "tuna" not in url:
                    raise BootstrapError("404")
                from sandboxkit.bootstrap import RootfsInfo

                rootfs.mkdir(parents=True, exist_ok=True)
                (rootfs / "bin").mkdir(exist_ok=True)
                (rootfs / "bin" / "sh").write_text("")
                return RootfsInfo(
                    version="9.9.9",
                    arch="x86_64",
                    url=url,
                    sha256="0" * 64,
                    installed_at="now",
                    packages=[],
                    preset=preset,
                )

            with mock.patch("sandboxkit.bootstrap.bootstrap", side_effect=fake_bootstrap):
                with mock.patch(
                    "sandboxkit.bootstrap.mirrors.resolve_all", return_value=crawled
                ):
                    info = bootstrap_with_fallback(
                        base / "rootfs", preset="alpine", log=lambda _m: None
                    )
            self.assertEqual(info.preset, "alpine")
            self.assertIn("tuna", info.url)

    def test_falls_through_to_other_distros(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            good = self._fake_rootfs_tar(base)
            seen: list[str] = []

            def fake_bootstrap(rootfs, *, preset, version, arch, url, **kwargs):
                seen.append(url)
                if "ubuntu" not in url:
                    raise BootstrapError("404")
                from sandboxkit.bootstrap import RootfsInfo

                rootfs.mkdir(parents=True, exist_ok=True)
                (rootfs / "bin").mkdir(exist_ok=True)
                (rootfs / "bin" / "sh").write_text("")
                return RootfsInfo(
                    version=version,
                    arch="x86_64",
                    url=url,
                    sha256="0" * 64,
                    installed_at="now",
                    packages=[],
                    preset=preset,
                )

            with mock.patch(
                "sandboxkit.bootstrap.bootstrap", side_effect=fake_bootstrap
            ), mock.patch(
                "sandboxkit.bootstrap.mirrors.resolve_all",
                return_value=[("ubuntu", "https://cdimage/ubuntu-base-1.tar.gz")],
            ):
                info = bootstrap_with_fallback(
                    base / "rootfs", preset="alpine", packages=[], log=lambda _m: None
                )
            self.assertTrue(any("alpine" in url for url in seen))
            self.assertTrue(any("ubuntu" in url for url in seen))
            self.assertEqual(info.preset, "ubuntu")

    def test_all_sources_failing_raises_with_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch(
                "sandboxkit.bootstrap.bootstrap",
                side_effect=BootstrapError("404"),
            ):
                with self.assertRaises(BootstrapError) as ctx:
                    bootstrap_with_fallback(
                        base / "rootfs", preset="alpine", log=lambda _m: None
                    )
            message = str(ctx.exception)
            self.assertIn("所有 rootfs 源都失败", message)
            self.assertIn("dl-cdn.alpinelinux.org", message)
            self.assertIn("rootfs_url", message)

    def test_explicit_url_is_the_only_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            calls: list[str] = []

            def fake_bootstrap(rootfs, *, url, **kwargs):
                calls.append(url)
                raise BootstrapError("404")

            with mock.patch("sandboxkit.bootstrap.bootstrap", side_effect=fake_bootstrap):
                with self.assertRaises(BootstrapError):
                    bootstrap_with_fallback(
                        base / "rootfs",
                        preset="custom",
                        url="https://example.invalid/x.tar",
                        log=lambda _m: None,
                    )
            self.assertEqual(len(calls), 1)

    def test_local_tarball_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_rootfs_tar(base)
            info = bootstrap_with_fallback(
                base / "rootfs",
                preset="custom",
                url=str(archive),
                packages=[],
                log=lambda _m: None,
            )
            self.assertTrue(is_ready(base / "rootfs"))
            self.assertTrue(info.url.startswith("file://"))


class TestDevNodes(unittest.TestCase):
    def test_creates_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_mknod(path, mode, device):
                Path(path).write_text("")

            with mock.patch("sandboxkit.bootstrap.os.mknod", side_effect=fake_mknod):
                created = ensure_dev_nodes(root, log=lambda _m: None)
            self.assertIn("null", created)
            self.assertTrue((root / "dev" / "null").exists())

    def test_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dev").mkdir()
            (root / "dev" / "null").write_text("")
            calls = []
            with mock.patch(
                "sandboxkit.bootstrap.os.mknod",
                side_effect=lambda *args: calls.append(str(args[0])),
            ):
                created = ensure_dev_nodes(root, log=lambda _m: None)
            self.assertNotIn("null", created)
            self.assertFalse(any(path.endswith("/null") for path in calls))

    def test_mknod_failure_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "sandboxkit.bootstrap.os.mknod", side_effect=OSError(1, "not permitted")
            ):
                created = ensure_dev_nodes(root, log=lambda _m: None)
            self.assertEqual(created, [])


class TestShellDetection(unittest.TestCase):
    """Alpine 的 /bin/sh 指向 /bin/busybox（绝对路径），不能被 exists() 判死。"""

    def test_absolute_symlink_counts_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "busybox").write_text("")
            (root / "bin" / "sh").symlink_to("/bin/busybox")
            self.assertTrue(has_shell(root))
            self.assertTrue(is_ready(root))

    def test_relative_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "busybox").write_text("")
            (root / "bin" / "sh").symlink_to("busybox")
            self.assertTrue(is_ready(root))

    def test_missing_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            self.assertFalse(is_ready(root))

    def test_broken_absolute_symlink_still_counts(self):
        # 宿主上没有 /bin/busybox 也不影响判断
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "sh").symlink_to("/nonexistent/busybox")
            self.assertTrue(has_shell(root))


class TestReplaceKeepsSessions(unittest.TestCase):
    """换 rootfs 不能把 chroot 的会话目录删掉。"""

    def test_sessions_survive_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rootfs = base / "rootfs"
            (rootfs / "bin").mkdir(parents=True)
            (rootfs / "bin" / "sh").write_text("")
            (rootfs / "sandbox" / "u1").mkdir(parents=True)
            (rootfs / "sandbox" / "u1" / "keep.txt").write_text("keep")

            archive = base / "new.tar.gz"
            payload = b"#!/bin/sh\n"
            info = tarfile.TarInfo("bin/sh")
            info.size = len(payload)
            info.mode = 0o755
            _make_tar(archive, [(info, payload)])

            bootstrap(
                rootfs,
                preset="custom",
                url=str(archive),
                packages=[],
                force=True,
                log=lambda _m: None,
            )
            self.assertTrue(is_ready(rootfs))
            self.assertEqual((rootfs / "sandbox" / "u1" / "keep.txt").read_text(), "keep")


class TestDockerImageRootfs(unittest.TestCase):
    """镜像导出：不依赖任何外网地址。"""

    def _fake_tar(self, base: Path, *, with_sh: bool = True) -> Path:
        archive = base / "export.tar"
        members = []
        if with_sh:
            payload = b"#!/bin/sh\n"
            sh = tarfile.TarInfo("bin/sh")
            sh.size = len(payload)
            sh.mode = 0o755
            members.append((sh, payload))
        _make_tar(archive, members)
        return archive

    def test_export_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_tar(base)
            calls: list[list[str]] = []

            def fake_runner(args, timeout=60.0):
                calls.append(list(args))
                return 0, "cid\n", ""

            def fake_export(name, dest, timeout):
                dest.write_bytes(archive.read_bytes())
                return True, ""

            with mock.patch("sandboxkit.bootstrap._export_container", side_effect=fake_export):
                info = from_docker_image(
                    "debian:bookworm-slim",
                    base / "rootfs",
                    packages=[],
                    runner=fake_runner,
                    log=lambda _m: None,
                )
            self.assertTrue(is_ready(base / "rootfs"))
            self.assertEqual(info.preset, "docker-image")
            self.assertEqual(info.url, "docker:debian:bookworm-slim")
            self.assertEqual(calls[0][0], "create")
            self.assertEqual(calls[-1][:2], ["rm", "-f"])

    def test_create_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_runner(args, timeout=60.0):
                return 1, "", "no such image"

            with self.assertRaises(BootstrapError) as ctx:
                from_docker_image(
                    "missing:latest", base / "rootfs", runner=fake_runner, log=lambda _m: None
                )
            self.assertIn("docker create", str(ctx.exception))

    def test_export_without_shell_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = self._fake_tar(base, with_sh=False)

            def fake_runner(args, timeout=60.0):
                return 0, "cid", ""

            def fake_export(name, dest, timeout):
                dest.write_bytes(archive.read_bytes())
                return True, ""

            with mock.patch("sandboxkit.bootstrap._export_container", side_effect=fake_export):
                with self.assertRaises(BootstrapError):
                    from_docker_image(
                        "weird:image", base / "rootfs", runner=fake_runner, log=lambda _m: None
                    )

    def test_empty_export_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_runner(args, timeout=60.0):
                return 0, "cid", ""

            def fake_export(name, dest, timeout):
                dest.write_bytes(b"")
                return True, ""

            with mock.patch("sandboxkit.bootstrap._export_container", side_effect=fake_export):
                with self.assertRaises(BootstrapError):
                    from_docker_image(
                        "x:1", base / "rootfs", runner=fake_runner, log=lambda _m: None
                    )

    def test_image_failure_falls_back_to_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            good = self._fake_tar(base)

            def fake_runner(args, timeout=60.0):
                return 1, "", "no such image"

            def fake_bootstrap(rootfs, *, preset, version, arch, url, **kwargs):
                from sandboxkit.bootstrap import RootfsInfo

                rootfs.mkdir(parents=True, exist_ok=True)
                (rootfs / "bin").mkdir(exist_ok=True)
                (rootfs / "bin" / "sh").write_text("")
                return RootfsInfo(
                    version=version,
                    arch="x86_64",
                    url=url,
                    sha256="0" * 64,
                    installed_at="now",
                    packages=[],
                    preset=preset,
                )

            with mock.patch("sandboxkit.bootstrap.bootstrap", side_effect=fake_bootstrap):
                with mock.patch("sandboxkit.backends.docker_cli", side_effect=fake_runner):
                    info = bootstrap_with_fallback(
                        base / "rootfs",
                        preset="alpine",
                        image="missing:latest",
                        log=lambda _m: None,
                    )
            self.assertNotEqual(info.preset, "docker-image")


class TestHelpers(unittest.TestCase):
    def test_default_url_contains_version_and_arch(self):
        url = default_url("3.20.3", "aarch64")
        self.assertIn("v3.20", url)
        self.assertIn("alpine-minirootfs-3.20.3-aarch64.tar.gz", url)

    def test_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"abc")
            self.assertEqual(
                sha256_of(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_is_ready_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            rootfs = Path(tmp) / "rootfs"
            self.assertFalse(is_ready(rootfs))
            (rootfs / "bin").mkdir(parents=True)
            (rootfs / "bin" / "sh").write_text("")
            self.assertTrue(is_ready(rootfs))
            self.assertIsNone(read_marker(rootfs))
            self.assertEqual(
                marker_path(rootfs), Path(tmp) / ".rootfs.bootstrap.json"
            )


if __name__ == "__main__":
    unittest.main()

class TestSuidStrip(unittest.TestCase):
    """rootfs 里不能留 setuid/setgid —— 那是 chroot 逃逸的正门。"""

    def test_strip_suid_clears_bits(self):
        import os
        import stat as stat_mod
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.bootstrap import strip_suid

        with tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            binary = root / "su"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(binary, 0o4755)
            script = root / "mount"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(script, 0o2755)

            cleared = strip_suid(root)
            self.assertEqual(cleared, 2)
            for path in (binary, script):
                mode = stat_mod.S_IMODE(os.stat(path).st_mode)
                self.assertFalse(mode & stat_mod.S_ISUID)
                self.assertFalse(mode & stat_mod.S_ISGID)
                self.assertEqual(mode & 0o111, 0o111)  # 可执行位保留

    def test_http_source_requires_sha256(self):
        import tempfile
        from pathlib import Path as _Path

        from sandboxkit.bootstrap import BootstrapError, bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BootstrapError) as ctx:
                bootstrap(
                    _Path(tmp) / "rootfs",
                    preset="alpine",
                    url="http://example.com/rootfs.tar.gz",
                    log=lambda _m: None,
                )
            self.assertIn("明文 HTTP", str(ctx.exception))

