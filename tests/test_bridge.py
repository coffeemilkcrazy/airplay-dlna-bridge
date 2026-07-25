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

from bridge import Bridge, ReengagePolicy   # noqa: E402
from config import Config                   # noqa: E402


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
        threading.Thread(target=b._serve_status, daemon=True).start()
        for _ in range(50):
            if b._status_httpd:
                break
            time.sleep(0.05)
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

    def test_control_endpoint_also_guarded(self):
        import urllib.error
        import urllib.request
        _, port = self._start(token="s3cret")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/volume/20",
                                     data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
        self.assertEqual(code, 401)


class TestArtworkAndTransport(unittest.TestCase):
    """Endpoints added for the web panel's cover art and transport buttons."""

    def _start(self, token=""):
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token))
        b.bar = CountingBar()
        threading.Thread(target=b._serve_status, daemon=True).start()
        for _ in range(50):
            if b._status_httpd:
                break
            time.sleep(0.05)

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


class TestWebUi(unittest.TestCase):
    """The control panel is served by the bridge itself so any device on the
    LAN can use it."""

    def _start(self, token=""):
        port = _free_port()
        b = Bridge(Config(soundbar_ip="127.0.0.1", status_port=port,
                          status_bind="127.0.0.1", status_token=token))
        b.bar = CountingBar()
        threading.Thread(target=b._serve_status, daemon=True).start()
        for _ in range(50):
            if b._status_httpd:
                break
            time.sleep(0.05)

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
        threading.Thread(target=b._serve_status, daemon=True).start()
        for _ in range(50):
            if b._status_httpd:
                break
            time.sleep(0.05)

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
