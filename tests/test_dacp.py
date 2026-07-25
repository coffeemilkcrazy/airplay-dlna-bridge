"""Tests for transport control of the AirPlay sender."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import dacp  # noqa: E402
from dacp import (COMMANDS, DacpRemote, parse_avahi,  # noqa: E402
                  parse_dnssd, resolve_dacp)

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
    """Pins the host to a Linux/avahi machine so these exercise the avahi
    path regardless of where the suite runs."""

    def setUp(self):
        which = mock.patch.object(
            dacp.shutil, "which",
            side_effect=lambda c: "/usr/bin/avahi-browse"
            if c == "avahi-browse" else None)
        which.start()
        self.addCleanup(which.stop)

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


DNSSD_OUT = """Lookup iTunes_Ctrl_ABC123._dacp._tcp.local
DATE: ---Sat 25 Jul 2026---
13:50:25.463  ...STARTING...
13:50:25.464  iTunes_Ctrl_ABC123._dacp._tcp.local. can be reached at mac.local.:3689 (interface 15)
"""


class TestCrossPlatformResolution(unittest.TestCase):
    """Linux has avahi-browse, macOS has dns-sd. Neither is present on both,
    so the resolver must handle whichever it finds."""

    def test_parses_avahi_output(self):
        self.assertEqual(parse_avahi(AVAHI_OUT, "iTunes_Ctrl_ABC123"),
                         ("192.0.2.10", 3689))

    def test_parses_dnssd_output(self):
        self.assertEqual(parse_dnssd(DNSSD_OUT), ("mac.local", 3689))

    def test_dnssd_trailing_dot_stripped(self):
        """'mac.local.:3689' must not become a hostname ending in a dot."""
        host, _ = parse_dnssd(DNSSD_OUT)
        self.assertFalse(host.endswith("."))

    def test_dnssd_no_match(self):
        self.assertIsNone(parse_dnssd("Lookup failed\nnothing here\n"))

    def test_prefers_avahi_when_present(self):
        with mock.patch.object(dacp.shutil, "which",
                               side_effect=lambda c: "/usr/bin/" + c
                               if c == "avahi-browse" else None), \
             mock.patch.object(dacp, "_resolve_avahi",
                               return_value=("10.0.0.1", 1)) as a, \
             mock.patch.object(dacp, "_resolve_dnssd") as d:
            resolve_dacp("ABC123")
        a.assert_called_once()
        d.assert_not_called()

    def test_falls_back_to_dnssd(self):
        with mock.patch.object(dacp.shutil, "which",
                               side_effect=lambda c: "/usr/bin/dns-sd"
                               if c == "dns-sd" else None), \
             mock.patch.object(dacp, "_resolve_dnssd",
                               return_value=("mac.local", 3689)) as d:
            self.assertEqual(resolve_dacp("ABC123"), ("mac.local", 3689))
        d.assert_called_once()

    def test_no_mdns_tool_at_all(self):
        with mock.patch.object(dacp.shutil, "which", return_value=None):
            self.assertIsNone(resolve_dacp("ABC123"))

    def test_dnssd_timeout_uses_partial_output(self):
        """dns-sd never exits, so the timeout IS the stop condition and its
        partial output is the result - not an error."""
        import subprocess as sp
        exc = sp.TimeoutExpired("dns-sd", 3)
        exc.stdout = DNSSD_OUT
        with mock.patch.object(dacp.subprocess, "run", side_effect=exc):
            self.assertEqual(dacp._resolve_dnssd("iTunes_Ctrl_ABC123"),
                             ("mac.local", 3689))

    def test_dnssd_timeout_with_bytes_output(self):
        import subprocess as sp
        exc = sp.TimeoutExpired("dns-sd", 3)
        exc.stdout = DNSSD_OUT.encode()
        with mock.patch.object(dacp.subprocess, "run", side_effect=exc):
            self.assertEqual(dacp._resolve_dnssd("iTunes_Ctrl_ABC123"),
                             ("mac.local", 3689))


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
