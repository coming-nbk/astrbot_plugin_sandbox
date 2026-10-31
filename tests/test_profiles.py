# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxkit.config import load_settings  # noqa: E402
from sandboxkit.profiles import DEFAULT_PROFILE, PROFILES, get_profile, resolve  # noqa: E402


class TestProfiles(unittest.TestCase):
    def test_three_profiles(self):
        self.assertEqual(set(PROFILES), {"minimal", "standard", "full"})

    def test_unknown_falls_back(self):
        self.assertEqual(get_profile("gentoo").name, DEFAULT_PROFILE)
        self.assertEqual(get_profile("").name, DEFAULT_PROFILE)

    def test_each_profile_is_complete(self):
        for profile in PROFILES.values():
            self.assertTrue(profile.preset)
            self.assertTrue(profile.image)
            self.assertTrue(profile.packages)
            self.assertGreater(profile.approx_mb, 0)
            self.assertTrue(profile.description)

    def test_full_is_superset_of_standard(self):
        standard = set(PROFILES["standard"].packages)
        full = set(PROFILES["full"].packages)
        self.assertTrue(standard <= full)
        self.assertGreater(PROFILES["full"].approx_mb, PROFILES["standard"].approx_mb)

    def test_resolve_prefers_explicit(self):
        preset, image, packages = resolve(
            "full", preset="alpine", image="alpine:3.20", packages=("bash",)
        )
        self.assertEqual(preset, "alpine")
        self.assertEqual(image, "alpine:3.20")
        self.assertEqual(packages, ("bash",))

    def test_resolve_uses_profile_defaults(self):
        preset, image, packages = resolve("standard")
        self.assertEqual(preset, "ubuntu")
        self.assertEqual(image, "ubuntu:24.04")
        self.assertIn("python3", packages)


class TestConfigIntegration(unittest.TestCase):
    def test_default_is_standard(self):
        settings = load_settings({})
        self.assertEqual(settings.rootfs_profile, "standard")
        self.assertEqual(settings.rootfs_preset, "ubuntu")
        self.assertIn("python3", settings.bootstrap_packages)
        self.assertIn("git", settings.bootstrap_packages)

    def test_minimal_profile(self):
        settings = load_settings({"rootfs_profile": "minimal"})
        self.assertEqual(settings.rootfs_preset, "alpine")
        self.assertEqual(settings.bootstrap_packages, ("python3",))

    def test_full_profile_has_build_tools(self):
        settings = load_settings({"rootfs_profile": "full"})
        self.assertIn("build-essential", settings.bootstrap_packages)
        self.assertIn("nodejs", settings.bootstrap_packages)

    def test_explicit_preset_beats_profile(self):
        settings = load_settings(
            {"rootfs_profile": "full", "rootfs_preset": "alpine"}
        )
        self.assertEqual(settings.rootfs_preset, "alpine")

    def test_explicit_packages_beat_profile(self):
        settings = load_settings(
            {"rootfs_profile": "full", "bootstrap_packages": ["bash"]}
        )
        self.assertEqual(settings.bootstrap_packages, ("bash",))

    def test_unknown_profile_warns(self):
        settings = load_settings({"rootfs_profile": "gentoo"})
        self.assertEqual(settings.rootfs_profile, "standard")
        self.assertTrue(any("gentoo" in item for item in settings.warnings))


if __name__ == "__main__":
    unittest.main()
