"""Tests for the settings table."""

import dataclasses
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from config import APP_VERSION, BY_NAME, Config, SETTINGS, env_names  # noqa: E402


class TestSettingsTable(unittest.TestCase):
    """SETTINGS drives the CLI, the env file and the deploy script. If it
    drifts from the dataclass, options silently stop being configurable."""

    def test_every_setting_has_a_config_field(self):
        fields = {f.name for f in dataclasses.fields(Config)}
        missing = {s.name for s in SETTINGS} - fields
        self.assertEqual(missing, set(), f"SETTINGS entries with no field: {missing}")

    def test_every_persisted_field_has_a_setting(self):
        fields = {f.name for f in dataclasses.fields(Config)}
        # these two are process switches, deliberately not persisted
        fields -= {"run_shairport", "verbose"}
        extra = fields - {s.name for s in SETTINGS}
        self.assertEqual(extra, set(), f"fields with no SETTINGS entry: {extra}")

    def test_defaults_agree_between_table_and_dataclass(self):
        cfg = Config()
        for s in SETTINGS:
            if s.name in ("min_volume", "max_volume"):
                continue        # normalise() may adjust these
            self.assertEqual(getattr(cfg, s.name), s.default,
                             f"default mismatch for {s.name}")

    def test_env_names_are_unique(self):
        names = env_names()
        self.assertEqual(len(names), len(set(names)))

    def test_cli_flags_are_unique(self):
        flags = [s.cli for s in SETTINGS]
        self.assertEqual(len(flags), len(set(flags)))

    def test_lookup_by_name(self):
        self.assertIs(BY_NAME["max_volume"],
                      next(s for s in SETTINGS if s.name == "max_volume"))


class TestParsing(unittest.TestCase):
    def test_cli_overrides_default(self):
        self.assertEqual(Config.from_args(["--max-volume", "30"]).max_volume, 30)

    def test_env_supplies_default(self):
        os.environ["MAX_VOLUME"] = "25"
        self.addCleanup(os.environ.pop, "MAX_VOLUME", None)
        self.assertEqual(Config.from_args([]).max_volume, 25)

    def test_cli_beats_env(self):
        os.environ["MAX_VOLUME"] = "25"
        self.addCleanup(os.environ.pop, "MAX_VOLUME", None)
        self.assertEqual(Config.from_args(["--max-volume", "9"]).max_volume, 9)

    def test_malformed_env_falls_back_to_default(self):
        os.environ["STATUS_PORT"] = "not-a-number"
        self.addCleanup(os.environ.pop, "STATUS_PORT", None)
        self.assertEqual(Config.from_args([]).status_port, 8772)

    def test_empty_env_is_ignored(self):
        os.environ["SOUNDBAR_IP"] = ""
        self.addCleanup(os.environ.pop, "SOUNDBAR_IP", None)
        self.assertEqual(Config.from_args([]).soundbar_ip, "")

    def test_no_shairport_switch(self):
        self.assertFalse(Config.from_args(["--no-shairport"]).run_shairport)
        self.assertTrue(Config.from_args([]).run_shairport)


class TestNormalisation(unittest.TestCase):
    def test_cap_bounded_to_device_scale(self):
        self.assertEqual(Config(max_volume=999).max_volume, 100)
        self.assertEqual(Config(max_volume=0).max_volume, 1)

    def test_floor_cannot_exceed_cap(self):
        c = Config(max_volume=12, min_volume=50)
        self.assertEqual(c.min_volume, 12)

    def test_floor_not_negative(self):
        self.assertEqual(Config(min_volume=-5).min_volume, 0)

    def test_applies_however_built(self):
        """Constructed directly, not only via from_args."""
        self.assertEqual(Config(max_volume=5, min_volume=99).min_volume, 5)


class TestVersion(unittest.TestCase):
    def test_release_looks_like_a_version(self):
        self.assertRegex(APP_VERSION, r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
