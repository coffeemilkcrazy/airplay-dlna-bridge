"""Tests for the bridge service: format detection, metadata, volume policy."""

import base64
import math
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import bridge as bridge_mod                 # noqa: E402
from bridge import AutoOffPolicy, Bridge, ReengagePolicy   # noqa: E402
from config import Config                   # noqa: E402
from soundbar import SoundbarError          # noqa: E402


def sine_pcm(frames: int, bits: int, rate: int = 44100, amp: float = 0.25) -> bytes:
    """Coherent stereo tone in the given sample width, little-endian."""
    out = bytearray()
    phase, step = 0.0, 2 * math.pi * 440.0 / rate
    for _ in range(frames):
        v = amp * math.sin(phase)
        phase = (phase + step) % (2 * math.pi)
        if bits == 16:
            s = struct.pack("<h", int(v * 32767))
        elif bits == 32:
            s = struct.pack("<i", int(v * 2147483647))
        else:
            raise ValueError(bits)
        out += s + s
    return bytes(out)


class FakeBar:
    def __init__(self, volume=10):
        self.volume = volume
        self.sets = []
        self.ip = "127.0.0.1"
        self.model = "FAKE-1"

    def get_volume(self):
        return self.volume

    def set_volume(self, n):
        self.sets.append(n)
        self.volume = n


class TestCorrelation(unittest.TestCase):
    def test_tone_is_highly_correlated(self):
        pcm = sine_pcm(20000, 16)
        import array
        a = array.array("h")
        a.frombytes(pcm)
        self.assertGreater(Bridge._lag1_correlation(a[0::2]), 0.9)

    def test_noise_is_uncorrelated(self):
        import array
        import random
        rnd = random.Random(1234)
        a = array.array("h", [rnd.randint(-20000, 20000) for _ in range(20000)])
        self.assertLess(abs(Bridge._lag1_correlation(a)), 0.2)

    def test_short_input_is_safe(self):
        import array
        self.assertEqual(Bridge._lag1_correlation(array.array("h", [1, 2, 3])), 0.0)


class TestInputFormatProbe(unittest.TestCase):
    """Regression cover for the S32-vs-S16 bug that made the soundbar hiss."""

    def _bridge(self):
        return Bridge(Config(soundbar_ip="127.0.0.1"))

    def test_accepts_correct_s16(self):
        b = self._bridge()
        b._probe_input_format(sine_pcm(60000, 16))
        self.assertTrue(b._fmt_checked)
        self.assertEqual(b.last_error, "")

    def test_detects_s32_mismatch(self):
        b = self._bridge()
        b._probe_input_format(sine_pcm(60000, 32))
        self.assertIn("mismatch", b.last_error.lower())
        self.assertIn("S32", b.last_error)

    def test_silence_does_not_false_alarm(self):
        b = self._bridge()
        b._probe_input_format(b"\x00" * 300000)
        self.assertEqual(b.last_error, "")

    def test_probe_waits_for_enough_audio(self):
        b = self._bridge()
        b._probe_input_format(b"\x00" * 1000)      # well under half a second
        self.assertFalse(b._fmt_checked)

    def test_probe_runs_once_per_session(self):
        b = self._bridge()
        b._probe_input_format(sine_pcm(60000, 16))
        self.assertTrue(b._fmt_checked)
        b._probe_input_format(sine_pcm(60000, 32))   # ignored while checked
        self.assertEqual(b.last_error, "")


class TestMinVolume(unittest.TestCase):
    """Floor behaviour in isolation - max_volume is set explicitly so these
    do not move when the default cap changes. The floor-vs-cap interaction is
    covered by TestMaxVolume.test_floor_respects_cap_when_applied."""

    def test_raises_when_below_floor(self):
        b = Bridge(Config(min_volume=15, max_volume=100))
        b.bar = FakeBar(4)
        b._apply_min_volume()
        self.assertEqual(b.bar.volume, 15)

    def test_leaves_higher_volume_alone(self):
        b = Bridge(Config(min_volume=15, max_volume=100))
        b.bar = FakeBar(30)
        b._apply_min_volume()
        self.assertEqual(b.bar.volume, 30)
        self.assertEqual(b.bar.sets, [])

    def test_disabled_by_default(self):
        """Default must never touch volume - it would fight the remote."""
        b = Bridge(Config())
        self.assertEqual(b.cfg.min_volume, 0)
        b.bar = FakeBar(2)
        b._apply_min_volume()
        self.assertEqual(b.bar.volume, 2)
        self.assertEqual(b.bar.sets, [])

    def test_equal_to_floor_is_untouched(self):
        b = Bridge(Config(min_volume=15, max_volume=100))
        b.bar = FakeBar(15)
        b._apply_min_volume()
        self.assertEqual(b.bar.sets, [])


class TestReengagePolicy(unittest.TestCase):
    """Retry transient dropouts, but stop fighting a deliberate input change."""

    def test_engages_initially(self):
        p = ReengagePolicy()
        self.assertTrue(p.may_engage())

    def test_retries_up_to_the_limit(self):
        p = ReengagePolicy(max_attempts=3)
        for _ in range(3):
            p.record_engage()
            self.assertTrue(p.may_engage())
        # first two dropouts are worth retrying
        p2 = ReengagePolicy(max_attempts=3)
        p2.record_engage()
        self.assertTrue(p2.record_left())
        p2.record_engage()
        self.assertTrue(p2.record_left())

    def test_backs_off_after_max_attempts(self):
        p = ReengagePolicy(max_attempts=2)
        p.record_engage()
        self.assertTrue(p.record_left())
        p.record_engage()
        self.assertFalse(p.record_left())      # hit the limit
        self.assertFalse(p.may_engage())       # and stays stood down

    def test_reset_clears_backoff_for_next_session(self):
        p = ReengagePolicy(max_attempts=1)
        p.record_engage()
        self.assertFalse(p.record_left())
        self.assertFalse(p.may_engage())
        p.reset()
        self.assertTrue(p.may_engage())
        self.assertEqual(p.attempts, 0)


class TestAutoOffPolicy(unittest.TestCase):
    """Powering the speaker off is the one action here the user cannot undo
    from the sending device, so the arming rules matter more than the timer."""

    def test_disabled_when_zero(self):
        p = AutoOffPolicy(0)
        self.assertFalse(p.enabled)
        p.session_ended()
        self.assertFalse(p.should_fire(1e9))

    def test_never_fires_before_a_session(self):
        """seconds_since_audio is infinite until something plays. Without this
        rule a restart would power the speaker off underneath whoever is
        watching television on it."""
        p = AutoOffPolicy(60)
        self.assertFalse(p.should_fire(float("inf")))
        self.assertIsNone(p.seconds_remaining(float("inf")))

    def test_fires_once_the_idle_period_passes(self):
        p = AutoOffPolicy(60)
        p.session_ended()
        self.assertFalse(p.should_fire(59))
        self.assertTrue(p.should_fire(60))

    def test_only_one_attempt_per_idle_period(self):
        """A method that does not work must not retry every second."""
        p = AutoOffPolicy(60)
        p.session_ended()
        self.assertTrue(p.should_fire(90))
        p.record_fired()
        self.assertFalse(p.should_fire(9000))

    def test_a_new_session_rearms_it(self):
        p = AutoOffPolicy(60)
        p.session_ended()
        p.record_fired()
        p.session_started()
        self.assertFalse(p.should_fire(1e9))     # live session, no countdown
        p.session_ended()
        self.assertTrue(p.should_fire(60))

    def test_countdown_only_while_armed(self):
        p = AutoOffPolicy(600)
        self.assertIsNone(p.seconds_remaining(10))
        p.session_ended()
        self.assertEqual(p.seconds_remaining(100), 500)
        p.record_fired()
        self.assertIsNone(p.seconds_remaining(100))

    def test_countdown_never_negative(self):
        p = AutoOffPolicy(60)
        p.session_ended()
        self.assertEqual(p.seconds_remaining(999), 0.0)

    def test_disable_is_permanent(self):
        p = AutoOffPolicy(60)
        p.session_ended()
        p.disable("nothing can power this off")
        self.assertFalse(p.enabled)
        self.assertFalse(p.should_fire(1e9))
        p.session_ended()                        # even after another session
        self.assertFalse(p.should_fire(1e9))


class CountingBar(FakeBar):
    """Records how often the soundbar is actually queried."""

    def __init__(self, volume=10, fail=False):
        super().__init__(volume)
        self.calls = 0
        self.fail = fail
        self.muted = False

    def set_mute(self, on):
        self.muted = on

    def transport_state(self, timeout=8):
        self.calls += 1
        if self.fail:
            from soundbar import SoundbarError
            raise SoundbarError("unreachable")
        return "PLAYING"

    def get_volume(self, timeout=8):
        return self.volume

    def get_mute(self, timeout=8):
        return self.muted

    def position(self, timeout=8):
        return {"elapsed": "0:00:42", "duration": "", "uri": "", "track": "1"}


class TestStatusCaching(unittest.TestCase):
    """/status is polled every few seconds; querying the device on every
    request makes an offline soundbar stall each poll for the full timeout."""

    def test_repeat_calls_are_served_from_cache(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        b.snapshot()
        b.snapshot()
        b.snapshot()
        self.assertEqual(b.bar.calls, 1)

    def test_cache_expires(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        b.snapshot()
        b._bar_cache_at = 0.0            # force expiry
        b.snapshot()
        self.assertEqual(b.bar.calls, 2)

    def test_reports_error_without_raising(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar(fail=True)
        snap = b.snapshot()
        self.assertIn("error", snap["soundbar"]["state"])
        self.assertIsNone(snap["soundbar"]["volume"])

    def test_snapshot_exposes_active_and_version(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        snap = b.snapshot()
        # 'connections' is a lifetime total; 'active' is the current count.
        self.assertIn("active", snap["stream"])
        self.assertEqual(snap["stream"]["active"], 0)
        self.assertIn("version", snap)
        self.assertIn("revision", snap)


class PowerBar(CountingBar):
    """A renderer that may or may not accept WAM power commands, and may or
    may not report which input it is on."""

    def __init__(self, wam_works=True, function="wifi"):
        super().__init__()
        self.wam_works = wam_works
        self.function = function        # None means GetFunc does not answer
        self.power_calls = []
        self.transport = "STOPPED"

    def wam_power(self, on, timeout=6):
        self.power_calls.append(on)
        if not self.wam_works:
            raise SoundbarError("no answer")
        return "<UIC/>"

    def wam_function(self):
        if self.function is None:
            raise SoundbarError("no answer")
        return self.function, "dlna"

    def transport_state(self, timeout=8):
        if self.fail:
            raise SoundbarError("unreachable")
        return self.transport

    def is_reachable(self, port=None, timeout=3):
        return True


class TestPowerControl(unittest.TestCase):
    """WAM first, the configured command second, and whatever worked is what
    has to undo it."""

    def _bridge(self, **cfg):
        return Bridge(Config(soundbar_ip="127.0.0.1", **cfg))

    def _fake_commands(self):
        """Replace the shell runner: the suite must never spawn one."""
        ran = []

        def runner(command):
            ran.append(command)
            return True, "command succeeded"

        original = bridge_mod.run_command
        bridge_mod.run_command = runner
        self.addCleanup(setattr, bridge_mod, "run_command", original)
        return ran

    def test_wam_is_tried_first(self):
        b = self._bridge()
        b.bar = PowerBar()
        ran = self._fake_commands()
        ok, _ = b.power_off("test")
        self.assertTrue(ok)
        self.assertEqual(b.bar.power_calls, [False])
        self.assertEqual(ran, [])                # command not needed
        self.assertTrue(b.powered_off)

    def test_falls_back_to_the_command_when_wam_is_silent(self):
        b = self._bridge(power_off_command="plug off")
        b.bar = PowerBar(wam_works=False)
        ran = self._fake_commands()
        ok, _ = b.power_off("test")
        self.assertTrue(ok)
        self.assertEqual(ran, ["plug off"])
        self.assertTrue(b.powered_off)

    def test_no_method_at_all_disarms_auto_off(self):
        """Otherwise it fails identically every second for the rest of the
        night, and the log is useless."""
        b = self._bridge(auto_off_minutes=30)
        b.bar = PowerBar(wam_works=False)
        ok, detail = b.power_off("test")
        self.assertFalse(ok)
        self.assertFalse(b.powered_off)
        self.assertFalse(b.auto_off.enabled)
        self.assertIn("POWER_OFF_COMMAND", detail)

    def test_wake_uses_wam_when_wam_powered_it_off(self):
        b = self._bridge()
        b.bar = PowerBar()
        b.power_off("test")
        ok, _ = b.power_on(wait=False)
        self.assertTrue(ok)
        self.assertEqual(b.bar.power_calls, [False, True])
        self.assertFalse(b.powered_off)

    def test_wake_uses_the_command_when_the_command_powered_it_off(self):
        """A plug that cut the power leaves nothing on the network to answer
        WAM, so the inverse command is the only thing that can work."""
        b = self._bridge(power_off_command="plug off",
                         power_on_command="plug on")
        b.bar = PowerBar(wam_works=False)
        ran = self._fake_commands()
        b.power_off("test")
        b.power_on(wait=False)
        self.assertEqual(ran, ["plug off", "plug on"])
        self.assertNotIn(True, b.bar.power_calls)   # never went near WAM

    def test_command_off_without_a_command_on_is_reported(self):
        b = self._bridge(power_off_command="plug off")
        b.bar = PowerBar(wam_works=False)
        self._fake_commands()
        b.power_off("test")
        ok, detail = b.power_on(wait=False)
        self.assertFalse(ok)
        self.assertIn("POWER_ON_COMMAND", detail)
        self.assertTrue(b.powered_off)             # still off, and says so

    def test_failure_is_visible_in_status(self):
        b = self._bridge()
        b.bar = PowerBar(wam_works=False)
        b.power_off("test")
        self.assertIn("cannot power", b.snapshot()["power"]["last_result"])

    def test_off_is_idempotent(self):
        b = self._bridge()
        b.bar = PowerBar()
        b.power_off("first")
        b.power_off("second")
        self.assertEqual(b.bar.power_calls, [False])

    def test_busy_renderer_is_left_alone(self):
        b = self._bridge()
        b.bar = PowerBar()
        b.bar.transport = "PLAYING"
        self.assertIn("playing", b._renderer_in_use())
        b.bar.transport = "STOPPED"
        self.assertEqual(b._renderer_in_use(), "")

    def test_unreachable_renderer_does_not_count_as_in_use(self):
        b = self._bridge()
        b.bar = PowerBar(function=None)     # answers nothing at all
        b.bar.fail = True
        self.assertEqual(b._renderer_in_use(), "")


    def test_manual_off_outranks_a_live_session(self):
        """The session loop wakes a speaker it powered off. An explicit off
        from the panel must survive that, or the button does nothing while
        anything is playing."""
        b = self._bridge()
        b.bar = PowerBar()
        b.power_off("requested", manual=True)
        self.assertTrue(b._suppress_wake)
        b.power_on(wait=False)
        self.assertFalse(b._suppress_wake)

    def test_auto_off_still_allows_the_next_wake(self):
        b = self._bridge()
        b.bar = PowerBar()
        b.power_off("idle for 30 min")
        self.assertFalse(b._suppress_wake)


class TestInputGuard(unittest.TestCase):
    """A soundbar playing a film through HDMI-ARC looks exactly like an idle
    one over UPnP. Only the WAM input tells them apart, and switching it off
    mid-film is the worst thing this feature could do."""

    def _bridge(self, **cfg):
        return Bridge(Config(soundbar_ip="127.0.0.1", **cfg))

    def test_wifi_input_may_be_powered_off(self):
        b = self._bridge()
        b.bar = PowerBar(function="wifi")
        self.assertEqual(b._renderer_in_use(), "")

    def test_hdmi_input_is_left_alone(self):
        b = self._bridge()
        b.bar = PowerBar(function="hdmi")
        self.assertIn("hdmi", b._renderer_in_use())

    def test_any_other_input_is_left_alone(self):
        for function in ("bt", "optical", "aux", "soundshare", "HDMI"):
            b = self._bridge()
            b.bar = PowerBar(function=function)
            self.assertNotEqual(b._renderer_in_use(), "", function)

    def test_renderer_without_the_wam_api_still_powers_off(self):
        """Otherwise auto-off would never work on anything but a Samsung."""
        b = self._bridge()
        b.bar = PowerBar(function=None)
        self.assertEqual(b._renderer_in_use(), "")

    def test_a_speaker_that_stops_answering_is_left_alone(self):
        """It answered before, so silence is a fault rather than an absence -
        and guessing wrong means cutting the power to a speaker in use."""
        b = self._bridge()
        b.bar = PowerBar(function="wifi")
        self.assertEqual(b._renderer_in_use(), "")     # teaches it GetFunc works
        b.bar.function = None
        self.assertIn("could not be checked", b._renderer_in_use())

    def test_input_is_checked_even_when_upnp_is_unreachable(self):
        """The two are separate services on separate ports."""
        b = self._bridge()
        b.bar = PowerBar(function="hdmi")
        b.bar.fail = True                              # transport_state raises
        self.assertIn("hdmi", b._renderer_in_use())

    def test_auto_off_records_why_it_did_not_fire(self):
        """'Why didn't it turn off?' must be answerable from /status."""
        b = self._bridge(auto_off_minutes=30)
        b.bar = PowerBar(function="hdmi")
        b.auto_off.session_ended()
        in_use = b._renderer_in_use()
        b.power_result = f"auto power-off skipped - {in_use}"
        self.assertIn("hdmi", b.snapshot()["power"]["last_result"])

    def test_a_manual_power_off_ignores_the_input(self):
        """An explicit press is an instruction, not a guess."""
        b = self._bridge()
        b.bar = PowerBar(function="hdmi")
        ok, _ = b.power_off("requested", manual=True)
        self.assertTrue(ok)
        self.assertTrue(b.powered_off)

class TestPowerStatus(unittest.TestCase):
    def test_powered_off_is_reported_without_querying_the_device(self):
        """Polling a speaker we switched off stalls every request for the full
        timeout to learn something already known."""
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        b.powered_off = True
        snap = b.snapshot()
        self.assertEqual(snap["soundbar"]["state"], "off")
        self.assertTrue(snap["power"]["off"])
        self.assertEqual(b.bar.calls, 0)

    def test_countdown_absent_until_a_session_has_ended(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1", auto_off_minutes=30))
        b.bar = CountingBar()
        self.assertIsNone(b.snapshot()["power"]["seconds_until_off"])
        b.auto_off.session_ended()
        b.broadcaster.write(b"\x00\x00\x00\x00")
        remaining = b.snapshot()["power"]["seconds_until_off"]
        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, 1800)

    def test_auto_off_minutes_exposed_for_the_panel(self):
        b = Bridge(Config(soundbar_ip="127.0.0.1", auto_off_minutes=30))
        b.bar = CountingBar()
        self.assertEqual(b.snapshot()["power"]["auto_off_minutes"], 30.0)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestStatusApiAuth(unittest.TestCase):
    """The status API also accepts volume/mute commands, so when it is exposed
    on the LAN a token must actually be enforced."""

    def _start(self, token=""):
        import urllib.error
        import urllib.request
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token))
        b.bar = CountingBar()
        # Bind synchronously, then serve. Starting the thread and waiting for
        # it to bind races thread startup, which on a slow machine surfaces as
        # a connection timeout rather than an obvious failure.
        b.create_status_server()
        threading.Thread(target=b._status_httpd.serve_forever,
                         daemon=True).start()
        def cleanup():
            if b._status_httpd:
                b._status_httpd.shutdown()
                # shutdown() stops serve_forever; server_close() releases the
                # listening socket, without which the test leaks descriptors.
                b._status_httpd.server_close()

        self.addCleanup(cleanup)
        return b, port

    def _get(self, port, path, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            return code

    def test_open_when_no_token_configured(self):
        _, port = self._start()
        self.assertEqual(self._get(port, "/status"), 200)

    def test_rejects_missing_token(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(self._get(port, "/status"), 401)

    def test_rejects_wrong_token(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(
            self._get(port, "/status", {"X-Bridge-Token": "nope"}), 401)

    def test_accepts_token_header(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(
            self._get(port, "/status", {"X-Bridge-Token": "s3cret"}), 200)

    def test_accepts_token_query_param(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(self._get(port, "/status?token=s3cret"), 200)

    def _post(self, port, path):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            return code

    def test_control_endpoint_also_guarded(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(self._post(port, "/volume/20"), 401)

    def test_power_endpoint_also_guarded(self):
        """Powering the speaker off is the most disruptive thing this API can
        do, so it must not be the route that forgets the token."""
        _, port = self._start(token="s3cret")
        self.assertEqual(self._post(port, "/power/off"), 401)


class TestArtworkAndTransport(unittest.TestCase):
    """Endpoints added for the web panel's cover art and transport buttons."""

    def _start(self, token=""):
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token))
        b.bar = CountingBar()
        # Bind synchronously, then serve. Starting the thread and waiting for
        # it to bind races thread startup, which on a slow machine surfaces as
        # a connection timeout rather than an obvious failure.
        b.create_status_server()
        threading.Thread(target=b._status_httpd.serve_forever,
                         daemon=True).start()

        def cleanup():
            if b._status_httpd:
                b._status_httpd.shutdown()
                b._status_httpd.server_close()

        self.addCleanup(cleanup)
        return b, port

    def _req(self, port, path, method="GET", token=None):
        import json as _json
        import urllib.error
        import urllib.request
        data = b"" if method == "POST" else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, method=method)
        if token:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "")
                return r.status, ctype, body
        except urllib.error.HTTPError as e:
            body = e.read()
            e.close()
            return e.code, "", body

    # -- artwork -------------------------------------------------------- #
    def test_artwork_404_when_absent(self):
        _, port = self._start()
        self.assertEqual(self._req(port, "/artwork")[0], 404)

    def test_artwork_served_with_sniffed_mime(self):
        b, port = self._start()
        b.metadata.artwork.data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        b.metadata.artwork.mime = "image/png"
        code, ctype, body = self._req(port, "/artwork")
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, b.metadata.artwork.data)

    def test_artwork_requires_token_when_set(self):
        b, port = self._start(token="s3cret")
        b.metadata.artwork.data = b"x"
        b.metadata.artwork.mime = "image/png"
        self.assertEqual(self._req(port, "/artwork")[0], 401)
        self.assertEqual(self._req(port, "/artwork", token="s3cret")[0], 200)

    def test_status_reports_artwork_availability(self):
        b, port = self._start()
        import json as _json
        before = _json.loads(self._req(port, "/status")[2])["artwork"]
        self.assertFalse(before["available"])
        b.metadata.artwork.data = b"x"
        b.metadata.artwork.version = 3
        after = _json.loads(self._req(port, "/status")[2])["artwork"]
        self.assertTrue(after["available"])
        self.assertEqual(after["version"], 3)

    # -- transport ------------------------------------------------------ #
    def test_transport_without_sender_explains_why(self):
        """Credentials only arrive once something has played, so the panel
        must get a usable reason rather than a bare failure."""
        import json as _json
        _, port = self._start()
        code, _, body = self._req(port, "/transport/playpause", "POST")
        self.assertEqual(code, 503)
        payload = _json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("no AirPlay sender", payload["detail"])

    # -- power ---------------------------------------------------------- #
    def test_power_off_explains_when_nothing_can_do_it(self):
        import json as _json
        b, port = self._start()
        b.bar = PowerBar(wam_works=False)
        code, _, body = self._req(port, "/power/off", "POST")
        self.assertEqual(code, 503)
        payload = _json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("POWER_OFF_COMMAND", payload["detail"])

    def test_power_off_then_on(self):
        import json as _json
        b, port = self._start()
        b.bar = PowerBar()
        self.assertEqual(self._req(port, "/power/off", "POST")[0], 200)
        self.assertTrue(b.powered_off)
        body = _json.loads(self._req(port, "/status")[2])
        self.assertTrue(body["power"]["off"])
        self.assertEqual(body["soundbar"]["state"], "off")
        self.assertEqual(self._req(port, "/power/on", "POST")[0], 200)
        self.assertFalse(b.powered_off)

    def test_unknown_power_command_rejected(self):
        _, port = self._start()
        self.assertEqual(self._req(port, "/power/sideways", "POST")[0], 404)

    def test_unknown_transport_command_rejected(self):
        import json as _json
        _, port = self._start()
        code, _, body = self._req(port, "/transport/destroy", "POST")
        self.assertEqual(code, 400)
        self.assertIn("supported", _json.loads(body))

    def test_transport_requires_token_when_set(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(
            self._req(port, "/transport/playpause", "POST")[0], 401)

    def test_status_reports_transport_availability(self):
        import json as _json
        b, port = self._start()
        self.assertFalse(
            _json.loads(self._req(port, "/status")[2])["transport"]["available"])
        b.metadata.dacp_id = "ABC123"
        b.metadata.active_remote = "998877"
        self.assertTrue(
            _json.loads(self._req(port, "/status")[2])["transport"]["available"])


class TestSettingsApi(unittest.TestCase):
    """Saving and applying are deliberately separate: everything is persisted,
    but only what the session loop re-reads takes effect without a restart."""

    def _start(self, token="", **cfg):
        port = _free_port()
        self.dir = tempfile.mkdtemp()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token,
                          config_dir=self.dir, **cfg))
        b.bar = CountingBar()
        b.create_status_server()
        threading.Thread(target=b._status_httpd.serve_forever,
                         daemon=True).start()

        def cleanup():
            if b._status_httpd:
                b._status_httpd.shutdown()
                b._status_httpd.server_close()

        self.addCleanup(cleanup)
        return b, port

    def _req(self, port, path, method="GET", body=None, token=None):
        import json as _json
        import urllib.error
        import urllib.request
        data = None
        if body is not None:
            data = _json.dumps(body).encode()
        elif method == "POST":
            data = b""
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            payload = e.read()
            e.close()
            return e.code, _json.loads(payload) if payload else {}

    # -- what is on offer ----------------------------------------------- #
    def test_only_editable_settings_are_listed(self):
        _, port = self._start()
        _, body = self._req(port, "/settings")
        offered = {item["env"] for item in body["settings"]}
        self.assertIn("AUTO_OFF", offered)
        for withheld in ("MAX_VOLUME", "MIN_VOLUME", "POWER_OFF_COMMAND",
                         "STATUS_TOKEN", "STATUS_BIND"):
            self.assertNotIn(withheld, offered)

    # -- saving ---------------------------------------------------------- #
    def test_live_setting_applies_without_a_restart(self):
        b, port = self._start()
        code, body = self._req(port, "/settings", "POST", {"AUTO_OFF": "30"})
        self.assertEqual(code, 200)
        self.assertEqual(body["restart_required"], [])
        self.assertEqual(b.cfg.auto_off_minutes, 30.0)
        # The policy holds its own copy of the threshold.
        self.assertEqual(b.auto_off.seconds, 1800.0)

    def test_restart_setting_is_saved_but_not_applied(self):
        """The running value must keep telling the truth until the restart."""
        b, port = self._start()
        code, body = self._req(port, "/settings", "POST",
                               {"AIRPLAY_NAME": "Kitchen"})
        self.assertEqual(code, 200)
        self.assertEqual(body["restart_required"], ["AIRPLAY_NAME"])
        self.assertEqual(b.cfg.airplay_name, "Soundbar")

        _, listing = self._req(port, "/settings")
        item = next(i for i in listing["settings"]
                    if i["env"] == "AIRPLAY_NAME")
        self.assertEqual(item["value"], "Kitchen")
        self.assertEqual(item["running"], "Soundbar")
        self.assertTrue(item["pending"])
        self.assertTrue(listing["restart_pending"])

    def test_unchanged_fields_are_not_reported_as_needing_a_restart(self):
        """The panel posts the whole form, so most of what arrives is already
        in force. Listing all of it would make 'needs a restart' meaningless."""
        _, port = self._start()
        _, body = self._req(port, "/settings", "POST", {
            "AIRPLAY_NAME": "Kitchen",     # changed
            "STREAM_PORT": "8770",         # already the running value
            "ADVERTISE_IP": "",
        })
        self.assertEqual(body["restart_required"], ["AIRPLAY_NAME"])
        self.assertEqual(list(body["applied"]), ["AIRPLAY_NAME"])

    def test_saving_nothing_new_is_a_no_op(self):
        _, port = self._start()
        _, body = self._req(port, "/settings", "POST", {"STREAM_PORT": "8770"})
        self.assertEqual(body["applied"], {})
        from config import read_env_file
        self.assertEqual(read_env_file(self.dir), {})

    def test_resaving_a_pending_value_still_asks_for_the_restart(self):
        """It is saved but not running, so it is still pending."""
        _, port = self._start()
        self._req(port, "/settings", "POST", {"AIRPLAY_NAME": "Kitchen"})
        _, body = self._req(port, "/settings", "POST",
                            {"AIRPLAY_NAME": "Kitchen"})
        self.assertEqual(body["restart_required"], ["AIRPLAY_NAME"])

    def test_value_is_persisted_where_the_installer_carries_it_forward(self):
        _, port = self._start()
        self._req(port, "/settings", "POST", {"AUTO_OFF": "30"})
        from config import read_env_file
        self.assertEqual(read_env_file(self.dir)["AUTO_OFF"], "30")

    def test_saving_survives_a_restart(self):
        _, port = self._start()
        self._req(port, "/settings", "POST", {"AIRPLAY_NAME": "Kitchen"})
        reborn = Config.from_args(["--config-dir", self.dir])
        self.assertEqual(reborn.airplay_name, "Kitchen")

    # -- refusals -------------------------------------------------------- #
    def test_uneditable_setting_is_refused(self):
        b, port = self._start()
        code, body = self._req(port, "/settings", "POST", {"MAX_VOLUME": "99"})
        self.assertEqual(code, 400)
        self.assertIn("MAX_VOLUME", body["errors"])
        self.assertEqual(b.cfg.max_volume, 12)

    def test_bad_value_names_the_field(self):
        _, port = self._start()
        code, body = self._req(port, "/settings", "POST",
                               {"STREAM_PORT": "99999"})
        self.assertEqual(code, 400)
        self.assertIn("STREAM_PORT", body["errors"])

    def test_one_bad_field_saves_nothing(self):
        b, port = self._start()
        self._req(port, "/settings", "POST",
                  {"AUTO_OFF": "30", "IDLE_STOP": "soon"})
        self.assertEqual(b.cfg.auto_off_minutes, 0.0)
        from config import read_env_file
        self.assertEqual(read_env_file(self.dir), {})

    def test_non_object_body_rejected(self):
        _, port = self._start()
        self.assertEqual(
            self._req(port, "/settings", "POST", ["AUTO_OFF"])[0], 400)

    def test_settings_require_the_token_when_set(self):
        _, port = self._start(token="s3cret")
        self.assertEqual(self._req(port, "/settings")[0], 401)
        self.assertEqual(
            self._req(port, "/settings", "POST", {"AUTO_OFF": "30"})[0], 401)
        self.assertEqual(self._req(port, "/restart", "POST")[0], 401)
        self.assertEqual(
            self._req(port, "/settings", token="s3cret")[0], 200)


class TestRestart(unittest.TestCase):
    def test_restart_exits_for_the_service_manager(self):
        """Exiting is the restart: systemd is Restart=always and the
        LaunchAgent is KeepAlive, so both bring the bridge back."""
        exited = threading.Event()
        original = bridge_mod.restart_process
        bridge_mod.restart_process = exited.set
        self.addCleanup(setattr, bridge_mod, "restart_process", original)

        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        ok, detail = b.request_restart()
        self.assertTrue(ok)
        self.assertIn("restart", detail)
        self.assertTrue(exited.wait(5), "the process never exited")

    def test_a_failed_shutdown_does_not_block_the_restart(self):
        exited = threading.Event()
        original = bridge_mod.restart_process
        bridge_mod.restart_process = exited.set
        self.addCleanup(setattr, bridge_mod, "restart_process", original)

        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.stop = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        b.request_restart()
        self.assertTrue(exited.wait(5), "a shutdown error stopped the restart")


class TestWebUi(unittest.TestCase):
    """The control panel is served by the bridge itself so any device on the
    LAN can use it."""

    def _start(self, token=""):
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token))
        b.bar = CountingBar()
        # Bind synchronously, then serve. Starting the thread and waiting for
        # it to bind races thread startup, which on a slow machine surfaces as
        # a connection timeout rather than an obvious failure.
        b.create_status_server()
        threading.Thread(target=b._status_httpd.serve_forever,
                         daemon=True).start()

        def cleanup():
            if b._status_httpd:
                b._status_httpd.shutdown()
                b._status_httpd.server_close()

        self.addCleanup(cleanup)
        return port

    def _fetch(self, port, path, token=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("X-Bridge-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            e.close()
            return e.code, "", body

    def test_serves_html_at_root(self):
        port = self._start()
        code, ctype, body = self._fetch(port, "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"<title>Soundbar</title>", body)

    def test_index_html_alias(self):
        port = self._start()
        self.assertEqual(self._fetch(port, "/index.html")[0], 200)

    def test_page_is_self_contained(self):
        """No CDN or external asset: the Pi may have no internet access, and
        the page must work regardless."""
        port = self._start()
        body = self._fetch(port, "/")[2].decode()
        for marker in ("src=\"http", "href=\"http", "@import"):
            self.assertNotIn(marker, body)

    def test_playing_indicator_is_css_only_and_accessible(self):
        """The equaliser is decorative: it must not be announced to screen
        readers, must be driven by CSS rather than a JS timer, and must have a
        reduced-motion fallback."""
        port = self._start()
        body = self._fetch(port, "/")[2].decode()
        self.assertIn('id="eq"', body)
        self.assertIn('aria-hidden="true"', body)
        self.assertIn("@keyframes eq", body)
        self.assertIn("prefers-reduced-motion", body)
        # toggled from the same signal as the status dot
        self.assertIn('$("eq").classList.toggle("on", active)', body)

    def test_page_drives_the_real_endpoints(self):
        port = self._start()
        body = self._fetch(port, "/")[2].decode()
        self.assertIn("/status", body)
        self.assertIn("/volume/", body)
        self.assertIn("/mute/", body)

    def test_page_served_even_when_token_required(self):
        """It must load so it can tell the user a token is needed; the data
        endpoints stay guarded."""
        port = self._start(token="s3cret")
        self.assertEqual(self._fetch(port, "/")[0], 200)
        self.assertEqual(self._fetch(port, "/status")[0], 401)
        self.assertEqual(self._fetch(port, "/status", "s3cret")[0], 200)

    def test_unknown_path_still_404s(self):
        port = self._start()
        self.assertEqual(self._fetch(port, "/nope")[0], 404)


class TestMaxVolume(unittest.TestCase):
    """A safety cap enforced on the bridge, so no client can exceed it."""

    def test_default_cap(self):
        self.assertEqual(Config().max_volume, 12)

    def test_cap_configurable(self):
        self.assertEqual(Config.from_args(["--max-volume", "35"]).max_volume, 35)

    def test_cap_is_bounded_to_the_device_scale(self):
        self.assertEqual(Config.from_args(["--max-volume", "999"]).max_volume, 100)
        self.assertEqual(Config.from_args(["--max-volume", "0"]).max_volume, 1)

    def test_floor_cannot_exceed_cap(self):
        c = Config.from_args(["--max-volume", "20", "--min-volume", "50"])
        self.assertLessEqual(c.min_volume, c.max_volume)

    def test_floor_respects_cap_when_applied(self):
        b = Bridge(Config(min_volume=50, max_volume=20))
        b.bar = FakeBar(2)
        b._apply_min_volume()
        self.assertEqual(b.bar.volume, 20)

    def test_snapshot_publishes_cap(self):
        b = Bridge(Config(max_volume=20, soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        self.assertEqual(b.snapshot()["soundbar"]["max_volume"], 20)


class TestVolumeEndpointCapping(unittest.TestCase):
    def _start(self, max_volume=20):
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", max_volume=max_volume))
        b.bar = CountingBar()
        # Bind synchronously, then serve. Starting the thread and waiting for
        # it to bind races thread startup, which on a slow machine surfaces as
        # a connection timeout rather than an obvious failure.
        b.create_status_server()
        threading.Thread(target=b._status_httpd.serve_forever,
                         daemon=True).start()

        def cleanup():
            if b._status_httpd:
                b._status_httpd.shutdown()
                b._status_httpd.server_close()

        self.addCleanup(cleanup)
        return b, port

    def _post(self, port, path):
        import json as _json
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return _json.loads(r.read())

    def test_request_above_cap_is_clamped(self):
        b, port = self._start(max_volume=20)
        body = self._post(port, "/volume/80")
        self.assertEqual(body["volume"], 20)
        self.assertEqual(body["requested"], 80)
        self.assertTrue(body["capped"])
        self.assertEqual(b.bar.sets[-1], 20)      # device never saw 80

    def _status(self, port):
        import json as _json
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status",
                                    timeout=5) as r:
            return _json.loads(r.read())

    def test_write_invalidates_the_status_cache(self):
        """Without this the slider snaps backwards: the client sets a value,
        polls immediately, and gets the pre-change cached one back."""
        b, port = self._start(max_volume=100)
        self.assertEqual(self._status(port)["soundbar"]["volume"], 10)  # cached
        self._post(port, "/volume/17")
        # No delay at all - exactly what the web panel does.
        self.assertEqual(self._status(port)["soundbar"]["volume"], 17)

    def test_mute_write_invalidates_cache(self):
        b, port = self._start()
        self._status(port)
        self._post(port, "/mute/on")
        self.assertEqual(b._bar_cache_at, 0.0)

    def test_request_below_cap_passes_through(self):
        b, port = self._start(max_volume=20)
        body = self._post(port, "/volume/12")
        self.assertEqual(body["volume"], 12)
        self.assertFalse(body["capped"])
        self.assertEqual(b.bar.sets[-1], 12)


class TestVersionReporting(unittest.TestCase):
    """The panel shows a release version; the installer checks the revision."""

    def test_release_version_is_human_readable(self):
        import bridge as bmod
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        snap = b.snapshot()
        self.assertEqual(snap["version"], bmod.APP_VERSION)
        self.assertRegex(snap["version"], r"^\d+\.\d+")

    def test_revision_is_separate_from_release(self):
        """A stale deploy is only detectable via the revision - the release
        string does not change unless someone bumps it."""
        b = Bridge(Config(soundbar_ip="127.0.0.1"))
        b.bar = CountingBar()
        snap = b.snapshot()
        self.assertIn("revision", snap)
        self.assertNotEqual(snap["version"], snap["revision"])


if __name__ == "__main__":
    unittest.main()
