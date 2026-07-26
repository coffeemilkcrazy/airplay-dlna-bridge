"""Tests for the settings table."""

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from config import (APP_VERSION, BY_NAME, Config, EDITABLE,  # noqa: E402
                    SETTINGS, apply_settings, describe_editable, env_names,
                    read_env_file, write_env_file)


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

    def test_auto_off_from_env(self):
        os.environ["AUTO_OFF"] = "45"
        self.addCleanup(os.environ.pop, "AUTO_OFF", None)
        self.assertEqual(Config.from_args([]).auto_off_minutes, 45.0)

    def test_power_commands_from_env(self):
        os.environ["POWER_OFF_COMMAND"] = "curl -X POST http://plug/off"
        self.addCleanup(os.environ.pop, "POWER_OFF_COMMAND", None)
        self.assertEqual(Config.from_args([]).power_off_command,
                         "curl -X POST http://plug/off")

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

    def test_auto_off_disabled_by_default(self):
        self.assertEqual(Config().auto_off_minutes, 0.0)
        self.assertEqual(Config().auto_off_seconds, 0.0)

    def test_auto_off_cannot_undercut_the_release(self):
        """Both timers measure the same silence, so an auto-off at or below
        idle_stop would fire while the release is still in flight."""
        c = Config(idle_stop_seconds=20.0, auto_off_minutes=0.1)   # 6s
        self.assertGreater(c.auto_off_seconds, c.idle_stop_seconds)

    def test_auto_off_left_alone_when_clear_of_the_release(self):
        self.assertEqual(Config(auto_off_minutes=30).auto_off_minutes, 30.0)
        self.assertEqual(Config(auto_off_minutes=30).auto_off_seconds, 1800.0)

    def test_auto_off_not_negative(self):
        self.assertEqual(Config(auto_off_minutes=-5).auto_off_minutes, 0.0)


class TestEditableSet(unittest.TestCase):
    """The panel can only change what this table says it can. These are the
    ones deliberately kept out of reach, and each for its own reason."""

    def test_volume_cap_and_floor_are_not_editable(self):
        """The cap is enforced server-side precisely so no client can raise
        it; offering it in the panel would hand that back."""
        for name in ("max_volume", "min_volume"):
            self.assertFalse(BY_NAME[name].editable, name)

    def test_power_commands_are_not_editable(self):
        """They run a shell as the service user, and the panel binds every
        interface with no token by default."""
        for name in ("power_off_command", "power_on_command"):
            self.assertFalse(BY_NAME[name].editable, name)

    def test_api_settings_are_not_editable(self):
        """A wrong bind or port would make the panel unreachable from itself."""
        for name in ("status_bind", "status_port", "status_token"):
            self.assertFalse(BY_NAME[name].editable, name)

    def test_live_settings_are_a_subset_of_editable(self):
        for s in SETTINGS:
            if s.live:
                self.assertTrue(s.editable, s.name)

    def test_something_is_editable(self):
        self.assertTrue(EDITABLE)


class TestApplySettings(unittest.TestCase):
    def test_coerces_and_returns_env_keys(self):
        applied, errors = apply_settings(Config(), {"AUTO_OFF": "30"})
        self.assertEqual(errors, {})
        self.assertEqual(applied, {"AUTO_OFF": 30.0})

    def test_refuses_anything_not_editable(self):
        applied, errors = apply_settings(Config(), {"MAX_VOLUME": "99"})
        self.assertEqual(applied, {})
        self.assertIn("MAX_VOLUME", errors)

    def test_nothing_applies_when_one_field_is_bad(self):
        """A half-saved form is worse than one that refused."""
        applied, errors = apply_settings(
            Config(), {"AUTO_OFF": "30", "IDLE_STOP": "soon"})
        self.assertEqual(applied, {})
        self.assertIn("IDLE_STOP", errors)

    def test_normalise_still_has_the_last_word(self):
        """The panel must not be able to set an auto-off that fires during
        the release."""
        applied, _ = apply_settings(Config(idle_stop_seconds=20.0),
                                    {"AUTO_OFF": "0.1"})
        self.assertGreater(applied["AUTO_OFF"] * 60, 20.0)

    def test_ports_are_refused_not_clamped(self):
        _, errors = apply_settings(Config(), {"STREAM_PORT": "99999"})
        self.assertIn("STREAM_PORT", errors)

    def test_negative_duration_refused(self):
        _, errors = apply_settings(Config(), {"IDLE_STOP": "-1"})
        self.assertIn("IDLE_STOP", errors)

    def test_empty_value_restores_the_default(self):
        applied, _ = apply_settings(Config(airplay_name="Kitchen"),
                                    {"AIRPLAY_NAME": ""})
        self.assertEqual(applied["AIRPLAY_NAME"], "Soundbar")

    def test_a_name_may_contain_spaces_but_an_address_may_not(self):
        applied, errors = apply_settings(Config(),
                                         {"AIRPLAY_NAME": "Living Room"})
        self.assertEqual(applied["AIRPLAY_NAME"], "Living Room")
        _, errors = apply_settings(Config(), {"SOUNDBAR_IP": "192.0.2.1 0"})
        self.assertIn("SOUNDBAR_IP", errors)


class TestEnvFile(unittest.TestCase):
    """bridge.env is the one place a setting is persisted, and the installer
    carries its contents forward, so a panel edit survives a re-deploy."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(read_env_file(self.dir), {})

    def test_round_trip(self):
        ok, _ = write_env_file(self.dir, {"AUTO_OFF": "30"})
        self.assertTrue(ok)
        self.assertEqual(read_env_file(self.dir)["AUTO_OFF"], "30")

    def test_existing_values_are_preserved(self):
        write_env_file(self.dir, {"AUTO_OFF": "30", "MAX_VOLUME": "20"})
        write_env_file(self.dir, {"AUTO_OFF": "45"})
        stored = read_env_file(self.dir)
        self.assertEqual(stored["AUTO_OFF"], "45")
        self.assertEqual(stored["MAX_VOLUME"], "20")

    def test_comments_and_blank_lines_ignored(self):
        path = Path(self.dir, "bridge.env")
        path.write_text("# a comment\n\nAUTO_OFF=30\nnonsense\n")
        self.assertEqual(read_env_file(self.dir), {"AUTO_OFF": "30"})

    def test_unwritable_directory_reports_rather_than_raises(self):
        ok, detail = write_env_file("/proc/nope/nowhere", {"AUTO_OFF": "1"})
        self.assertFalse(ok)
        self.assertIn("could not write", detail)

    def test_file_wins_over_the_environment(self):
        """macOS gets its environment from a plist only install.sh rewrites,
        so a panel edit would otherwise be invisible until the next deploy."""
        write_env_file(self.dir, {"AUTO_OFF": "45"})
        os.environ["AUTO_OFF"] = "5"
        self.addCleanup(os.environ.pop, "AUTO_OFF", None)
        cfg = Config.from_args(["--config-dir", self.dir])
        self.assertEqual(cfg.auto_off_minutes, 45.0)

    def test_command_line_still_wins_over_the_file(self):
        write_env_file(self.dir, {"AUTO_OFF": "45"})
        cfg = Config.from_args(["--config-dir", self.dir, "--auto-off", "60"])
        self.assertEqual(cfg.auto_off_minutes, 60.0)


class TestDescribeEditable(unittest.TestCase):
    def test_reports_saved_and_running_separately(self):
        cfg = Config(airplay_name="Soundbar")
        items = {i["env"]: i for i in
                 describe_editable(cfg, {"AIRPLAY_NAME": "Kitchen"})}
        self.assertEqual(items["AIRPLAY_NAME"]["value"], "Kitchen")
        self.assertEqual(items["AIRPLAY_NAME"]["running"], "Soundbar")
        self.assertTrue(items["AIRPLAY_NAME"]["pending"])

    def test_nothing_pending_when_they_agree(self):
        cfg = Config(airplay_name="Kitchen")
        items = {i["env"]: i for i in
                 describe_editable(cfg, {"AIRPLAY_NAME": "Kitchen"})}
        self.assertFalse(items["AIRPLAY_NAME"]["pending"])

    def test_unparseable_stored_value_falls_back_to_running(self):
        items = {i["env"]: i for i in
                 describe_editable(Config(), {"AUTO_OFF": "soon"})}
        self.assertEqual(items["AUTO_OFF"]["value"], 0.0)
        self.assertFalse(items["AUTO_OFF"]["pending"])


class TestVersion(unittest.TestCase):
    def test_release_looks_like_a_version(self):
        self.assertRegex(APP_VERSION, r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
