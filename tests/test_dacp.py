"""Tests for transport control of the AirPlay sender."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import dacp  # noqa: E402
from dacp import COMMANDS, DacpRemote, resolve_dacp  # noqa: E402

# avahi-browse -rptk output; fields are ;-separated, host at 7, port at 8.
AVAHI_OUT = (
    "+;eth0;IPv4;iTunes_Ctrl_ABC123;_dacp._tcp;local\n"
    "=;eth0;IPv4;iTunes_Ctrl_ABC123;_dacp._tcp;local;mac.local;192.0.2.10;3689;\n"
    "=;eth0;IPv4;iTunes_Ctrl_OTHER;_dacp._tcp;local;other.local;192.0.2.20;3690;\n"
)


class TestCommandWhitelist(unittest.TestCase):
    """Only known commands are forwarded, so the endpoint cannot be used to
    poke arbitrary DACP paths on the sender."""

    def test_expected_commands_present(self):
        for c in ("playpause", "play", "pause", "stop", "next", "previous"):
            self.assertIn(c, COMMANDS)

    def test_unknown_command_rejected(self):
        ok, detail = DacpRemote().send("rm -rf", "ABC", "tok")
        self.assertFalse(ok)
        self.assertIn("unknown command", detail)

    def test_next_maps_to_apple_spelling(self):
        self.assertEqual(COMMANDS["next"], "nextitem")
        self.assertEqual(COMMANDS["previous"], "previtem")


class TestAvailability(unittest.TestCase):
    def test_requires_both_credentials(self):
        r = DacpRemote()
        self.assertFalse(r.available("", ""))
        self.assertFalse(r.available("ABC", ""))
        self.assertFalse(r.available("", "tok"))
        self.assertTrue(r.available("ABC", "tok"))

    def test_send_without_credentials_explains_why(self):
        ok, detail = DacpRemote().send("playpause", "", "")
        self.assertFalse(ok)
        self.assertIn("no AirPlay sender", detail)


class TestResolve(unittest.TestCase):
    def _run(self, stdout="", side_effect=None):
        m = mock.Mock()
        m.stdout = stdout
        if side_effect:
            return mock.patch.object(dacp.subprocess, "run", side_effect=side_effect)
        return mock.patch.object(dacp.subprocess, "run", return_value=m)

    def test_finds_matching_service(self):
        with self._run(AVAHI_OUT):
            self.assertEqual(resolve_dacp("ABC123"), ("192.0.2.10", 3689))

    def test_ignores_other_senders(self):
        with self._run(AVAHI_OUT):
            self.assertEqual(resolve_dacp("OTHER"), ("192.0.2.20", 3690))

    def test_absent_service_returns_none(self):
        with self._run(AVAHI_OUT):
            self.assertIsNone(resolve_dacp("NOPE"))

    def test_missing_avahi_is_handled(self):
        """The Pi always has avahi for AirPlay, but do not crash if not."""
        with self._run(side_effect=FileNotFoundError()):
            self.assertIsNone(resolve_dacp("ABC123"))

    def test_timeout_is_handled(self):
        import subprocess as sp
        with self._run(side_effect=sp.TimeoutExpired("avahi-browse", 6)):
            self.assertIsNone(resolve_dacp("ABC123"))

    def test_malformed_line_skipped(self):
        with self._run("=;eth0;IPv4;iTunes_Ctrl_ABC123;_dacp._tcp\n"):
            self.assertIsNone(resolve_dacp("ABC123"))


class TestSend(unittest.TestCase):
    def test_successful_command_uses_active_remote_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["token"] = req.get_header("Active-remote")
            return mock.MagicMock(__enter__=lambda s: s,
                                  __exit__=lambda *a: False,
                                  read=lambda: b"")

        with mock.patch.object(dacp, "resolve_dacp",
                               return_value=("192.0.2.10", 3689)), \
             mock.patch.object(dacp.urllib.request, "urlopen", fake_urlopen):
            ok, detail = DacpRemote().send("playpause", "ABC123", "998877")

        self.assertTrue(ok, detail)
        self.assertEqual(captured["url"],
                         "http://192.0.2.10:3689/ctrl-int/1/playpause")
        self.assertEqual(captured["token"], "998877")

    def test_unresolvable_sender_reported(self):
        with mock.patch.object(dacp, "resolve_dacp", return_value=None):
            ok, detail = DacpRemote().send("next", "ABC123", "tok")
        self.assertFalse(ok)
        self.assertIn("could not resolve", detail)

    def test_unreachable_sender_forgets_endpoint(self):
        """A stale endpoint must not be retried forever."""
        r = DacpRemote()
        with mock.patch.object(dacp, "resolve_dacp",
                               return_value=("10.0.0.1", 3689)), \
             mock.patch.object(dacp.urllib.request, "urlopen",
                               side_effect=OSError("refused")):
            ok, detail = r.send("play", "ABC123", "tok")
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)
        self.assertIsNone(r._endpoint)

    def test_endpoint_is_cached_between_commands(self):
        with mock.patch.object(dacp, "resolve_dacp",
                               return_value=("10.0.0.1", 3689)) as resolver, \
             mock.patch.object(dacp.urllib.request, "urlopen",
                               return_value=mock.MagicMock(
                                   __enter__=lambda s: s,
                                   __exit__=lambda *a: False,
                                   read=lambda: b"")):
            r = DacpRemote()
            r.send("play", "ABC123", "tok")
            r.send("pause", "ABC123", "tok")
        self.assertEqual(resolver.call_count, 1)


if __name__ == "__main__":
    unittest.main()
