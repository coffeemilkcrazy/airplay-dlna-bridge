"""Tests for the live PCM fan-out and WAV streaming server."""

import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from streamer import (LiveWavServer, PcmBroadcaster, WAV_MIME,  # noqa: E402
                      wav_header)


class TestWavHeader(unittest.TestCase):
    def test_riff_structure(self):
        h = wav_header(44100, 2, 16, 1000)
        self.assertEqual(h[:4], b"RIFF")
        self.assertEqual(h[8:12], b"WAVE")
        self.assertEqual(h[12:16], b"fmt ")
        self.assertEqual(h[36:40], b"data")
        self.assertEqual(len(h), 44)

    def test_declared_sizes(self):
        h = wav_header(44100, 2, 16, 1000)
        self.assertEqual(struct.unpack("<I", h[4:8])[0], 36 + 1000)
        self.assertEqual(struct.unpack("<I", h[40:44])[0], 1000)

    def test_format_fields(self):
        # 44.1 kHz, stereo, 16-bit -> 176400 B/s, block align 4
        fmt = struct.unpack("<IHHIIHH", wav_header(44100, 2, 16, 0)[16:36])
        _, audio_fmt, channels, rate, byte_rate, align, bits = fmt
        self.assertEqual(audio_fmt, 1)          # PCM
        self.assertEqual(channels, 2)
        self.assertEqual(rate, 44100)
        self.assertEqual(byte_rate, 176400)
        self.assertEqual(align, 4)
        self.assertEqual(bits, 16)

    def test_other_geometries(self):
        fmt = struct.unpack("<IHHIIHH", wav_header(48000, 2, 24, 0)[16:36])
        self.assertEqual(fmt[4], 48000 * 2 * 3)   # byte rate
        self.assertEqual(fmt[5], 6)               # block align


class TestPcmBroadcaster(unittest.TestCase):
    def test_fans_out_to_every_client(self):
        b = PcmBroadcaster()
        c1, c2 = b.add_client(), b.add_client()
        b.write(b"\x01\x02\x03\x04")
        self.assertEqual(c1.read(timeout=0.1), b"\x01\x02\x03\x04")
        self.assertEqual(c2.read(timeout=0.1), b"\x01\x02\x03\x04")

    def test_read_drains_buffer(self):
        b = PcmBroadcaster()
        c = b.add_client()
        b.write(b"abcd")
        self.assertEqual(c.read(timeout=0.1), b"abcd")
        self.assertEqual(c.read(timeout=0.05), b"")

    def test_removed_client_stops_receiving(self):
        b = PcmBroadcaster()
        c = b.add_client()
        b.remove_client(c)
        b.write(b"xyz")
        self.assertEqual(c.read(timeout=0.05), b"")

    def test_backlog_drops_oldest_to_stay_live(self):
        # 0.1s hard backlog at 44.1k/16/stereo = 17640 bytes.
        # soft_backlog is clamped to half of that, so keep writes below it
        # until we deliberately overflow.
        b = PcmBroadcaster(max_backlog_seconds=0.1, soft_backlog_seconds=0.1)
        c = b.add_client()
        limit = int(b.bytes_per_second * 0.1)
        b.write(b"\x00" * limit)          # at the hard limit: no bulk drop
        self.assertEqual(c.dropped, 0)
        b.write(b"\xff" * 1000)           # overflow
        self.assertGreater(c.dropped, 0)
        data = c.read(timeout=0.1)
        # the newest bytes must survive, the oldest must be discarded
        self.assertTrue(data.endswith(b"\xff" * 1000))
        self.assertLessEqual(len(data), limit)

    def test_removals_are_always_frame_aligned(self):
        """Removing a partial frame shifts the channel interleave and swaps
        left/right for the rest of the stream. The buffer may legitimately
        hold a partial frame in flight - what must never happen is discarding
        a non-multiple of the frame size."""
        b = PcmBroadcaster(max_backlog_seconds=0.1, soft_backlog_seconds=0.05)
        c = b.add_client()
        b.write(b"\x00" * b._max_backlog)
        b.write(b"\xff" * 1001)           # deliberately not a frame multiple
        b.write(b"\xee" * 997)            # nor is this
        self.assertEqual(c.dropped % b.bytes_per_frame, 0)
        self.assertEqual(c.trimmed % b.bytes_per_frame, 0)
        self.assertGreater(c.dropped + c.trimmed, 0)

    def test_micro_trim_between_soft_and_hard_thresholds(self):
        """Clock drift is bled off one frame at a time rather than allowed to
        build into an audible bulk drop."""
        b = PcmBroadcaster(max_backlog_seconds=1.0, soft_backlog_seconds=0.1)
        c = b.add_client()
        # push past the soft threshold but nowhere near the hard one
        b.write(b"\x00" * (b._soft_backlog + 400))
        b.write(b"\x00" * 400)
        self.assertEqual(c.dropped, 0)                     # no audible drop
        self.assertGreater(c.trimmed, 0)                   # but drift corrected
        self.assertEqual(c.trimmed % b.bytes_per_frame, 0)

    def test_trim_is_proportional_to_overshoot(self):
        """A fixed one-frame trim can be outrun by fast drift; the correction
        must scale so the hard limit is never reached."""
        b = PcmBroadcaster(max_backlog_seconds=2.0, soft_backlog_seconds=0.2)
        small = b.add_client()
        big = b.add_client()
        # nudge one client just past the threshold, the other far past it
        small.push(b"\x00" * (b._soft_backlog + 64), b._max_backlog,
                   b._soft_backlog, b.bytes_per_frame, b._max_trim)
        big.push(b"\x00" * (b._soft_backlog + 40000), b._max_backlog,
                 b._soft_backlog, b.bytes_per_frame, b._max_trim)
        self.assertGreater(big.trimmed, small.trimmed)
        self.assertEqual(big.dropped, 0)          # corrected without a bulk drop

    def test_trim_is_capped_per_correction(self):
        """No single splice should be long enough to hear."""
        b = PcmBroadcaster(max_backlog_seconds=5.0, soft_backlog_seconds=0.1)
        c = b.add_client()
        c.push(b"\x00" * (b._soft_backlog + 500000), b._max_backlog,
               b._soft_backlog, b.bytes_per_frame, b._max_trim)
        self.assertLessEqual(c.trimmed, b._max_trim)
        self.assertEqual(c.trimmed % b.bytes_per_frame, 0)

    def test_drift_converges_without_bulk_drop(self):
        """Simulate a renderer consuming slightly slower than realtime and
        confirm the backlog stabilises instead of reaching the hard limit."""
        b = PcmBroadcaster(max_backlog_seconds=2.0, soft_backlog_seconds=0.05)
        c = b.add_client()
        chunk = b"\x00" * 4096
        for _ in range(400):
            c.push(chunk, b._max_backlog, b._soft_backlog,
                   b.bytes_per_frame, b._max_trim)
            # renderer drains a little less than was written, so the backlog
            # creeps upward exactly as clock drift makes it
            drained = min(len(c._buf), 3900)
            del c._buf[:drained]
        self.assertEqual(c.dropped, 0)            # never hit the audible path
        self.assertGreater(c.trimmed, 0)          # drift was absorbed

    def test_no_trim_below_soft_threshold(self):
        b = PcmBroadcaster(max_backlog_seconds=1.0, soft_backlog_seconds=0.5)
        c = b.add_client()
        b.write(b"\x00" * 1000)
        self.assertEqual(c.trimmed, 0)
        self.assertEqual(c.dropped, 0)

    def test_soft_threshold_clamped_below_hard(self):
        b = PcmBroadcaster(max_backlog_seconds=0.2, soft_backlog_seconds=5.0)
        self.assertLess(b._soft_backlog, b._max_backlog)

    def test_client_count_tracks_lifecycle(self):
        b = PcmBroadcaster()
        self.assertEqual(b.client_count, 0)
        c = b.add_client()
        self.assertEqual(b.client_count, 1)
        b.remove_client(c)
        self.assertEqual(b.client_count, 0)

    def test_silence_is_frame_aligned_and_silent(self):
        b = PcmBroadcaster()
        s = b.silence(0.25)
        self.assertEqual(set(s), {0})
        self.assertEqual(len(s) % 4, 0)                 # whole stereo frames
        self.assertAlmostEqual(len(s), b.bytes_per_second * 0.25, delta=4)

    def test_seconds_since_audio(self):
        b = PcmBroadcaster()
        self.assertEqual(b.seconds_since_audio, float("inf"))
        b.write(b"\x00\x00\x00\x00")
        self.assertLess(b.seconds_since_audio, 1.0)

    def test_empty_write_is_ignored(self):
        b = PcmBroadcaster()
        b.write(b"")
        self.assertEqual(b.seconds_since_audio, float("inf"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestLiveWavServer(unittest.TestCase):
    def setUp(self):
        self.b = PcmBroadcaster()
        self.port = _free_port()
        self.srv = LiveWavServer(self.b, host="127.0.0.1", port=self.port,
                                 path="/t.wav")
        self.srv.start()
        time.sleep(0.2)

    def tearDown(self):
        self.srv.stop()

    def _request(self, method: str, path: str = "/t.wav"):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        # Registered up front so the socket closes even if an assertion fails.
        self.addCleanup(s.close)
        s.sendall(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        return s

    def _read_headers(self, s):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(1)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="ignore")

    def test_url_for(self):
        self.assertEqual(self.srv.url_for("192.0.2.30"),
                         f"http://192.0.2.30:{self.port}/t.wav")

    def test_head_advertises_dlna_streaming(self):
        s = self._request("HEAD")
        head = self._read_headers(s)
        s.close()
        self.assertIn("200 OK", head)
        self.assertIn(WAV_MIME, head)
        self.assertIn("transferMode.dlna.org: Streaming", head)
        self.assertIn("contentFeatures.dlna.org:", head)
        self.assertIn("Content-Length:", head)

    def test_unknown_path_404s(self):
        s = self._request("GET", "/nope")
        head = self._read_headers(s)
        s.close()
        self.assertIn("404", head)

    def test_get_streams_wav_header_then_audio(self):
        s = self._request("GET")
        self._read_headers(s)
        payload = b""
        deadline = time.time() + 3
        while len(payload) < 44 and time.time() < deadline:
            payload += s.recv(4096)
        self.assertEqual(payload[:4], b"RIFF")
        self.assertEqual(payload[8:12], b"WAVE")

        # audio written now must reach the socket
        self.b.write(b"\xAA\xBB" * 2000)
        got = b""
        deadline = time.time() + 3
        while b"\xAA\xBB" not in got and time.time() < deadline:
            got += s.recv(8192)
        s.close()
        self.assertIn(b"\xAA\xBB", got)

    def test_counts_connection_and_bytes(self):
        s = self._request("GET")
        self._read_headers(s)
        time.sleep(0.3)
        self.b.write(b"\x11\x22" * 1000)
        deadline = time.time() + 3
        while self.srv.bytes_streamed == 0 and time.time() < deadline:
            s.recv(8192)
        s.close()
        self.assertEqual(self.srv.connections, 1)
        self.assertGreater(self.srv.bytes_streamed, 0)

    def test_disconnect_clients_hangs_up(self):
        """Once a session ends the renderer must be let go, or we feed it
        silence forever and it stays unavailable to other controllers."""
        s = self._request("GET")
        self._read_headers(s)
        deadline = time.time() + 3
        while self.srv.connections == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.srv.connections, 1)

        closed = self.srv.disconnect_clients()
        self.assertEqual(closed, 1)

        # the client side must now see EOF rather than an endless silence feed
        s.settimeout(3)
        got_eof = False
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                if s.recv(65536) == b"":
                    got_eof = True
                    break
            except OSError:
                got_eof = True
                break
        self.assertTrue(got_eof)

    def test_disconnect_with_no_clients_is_noop(self):
        self.assertEqual(self.srv.disconnect_clients(), 0)

    def test_disconnect_is_idempotent(self):
        """Release and shutdown both call this; the second must not report
        closing a connection that is already gone."""
        s = self._request("GET")
        self._read_headers(s)
        deadline = time.time() + 3
        while self.srv.connections == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.srv.disconnect_clients(), 1)
        self.assertEqual(self.srv.disconnect_clients(), 0)

    def test_silence_keepalive_when_no_audio(self):
        """With no audio at all the server must still emit data, or the
        renderer tears the session down between tracks."""
        s = self._request("GET")
        self._read_headers(s)
        got = b""
        deadline = time.time() + 3
        while len(got) < 8000 and time.time() < deadline:
            got += s.recv(8192)
        s.close()
        self.assertGreater(len(got), 4000)


if __name__ == "__main__":
    unittest.main()
