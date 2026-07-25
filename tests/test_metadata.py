"""Tests for the shairport-sync metadata reader."""

import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from metadata import MetadataReader, NowPlaying, sniff_image  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def item(code: str, value: bytes | str = b"") -> bytes:
    if isinstance(value, str):
        value = value.encode()
    payload = base64.b64encode(value).decode()
    body = f'<data encoding="base64">\n{payload}</data>' if value else ""
    return (f"<item><type>636f7265</type><code>{code.encode().hex()}</code>"
            f"<length>{len(value)}</length>\n{body}</item>").encode()


class TestNowPlaying(unittest.TestCase):
    def test_truthiness_and_clear(self):
        n = NowPlaying()
        self.assertFalse(n)
        n.title = "x"
        self.assertTrue(n)
        n.clear()
        self.assertFalse(n)


class TestSniffImage(unittest.TestCase):
    def test_jpeg(self):
        self.assertEqual(sniff_image(JPEG), "image/jpeg")

    def test_png(self):
        self.assertEqual(sniff_image(PNG), "image/png")

    def test_unknown(self):
        self.assertEqual(sniff_image(b"garbage"), "application/octet-stream")


class TestPipe(unittest.TestCase):
    """Regression cover for read() vs read1() blocking on a FIFO: read(n)
    waits for all n bytes, so a few hundred bytes per track never arrived."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pipe = os.path.join(self.dir, "metadata")
        os.mkfifo(self.pipe)
        self.reader = MetadataReader(self.pipe)
        threading.Thread(target=self.reader.run, daemon=True).start()
        time.sleep(0.3)
        self.fd = os.open(self.pipe, os.O_WRONLY)

    def tearDown(self):
        self.reader.stop()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _wait(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_parses_a_small_burst(self):
        os.write(self.fd, item("minm", "Bohemian Rhapsody") + item("asar", "Queen"))
        self.assertTrue(self._wait(
            lambda: self.reader.now_playing.title == "Bohemian Rhapsody"))
        self.assertEqual(self.reader.now_playing.artist, "Queen")

    def test_bundle_start_clears_stale_fields(self):
        os.write(self.fd, item("mdst") + item("minm", "One") + item("asal", "Album A"))
        self.assertTrue(self._wait(
            lambda: self.reader.now_playing.album == "Album A"))
        os.write(self.fd, item("mdst") + item("minm", "Two"))
        self.assertTrue(self._wait(lambda: self.reader.now_playing.title == "Two"))
        self.assertEqual(self.reader.now_playing.album, "")

    def test_unicode_survives(self):
        os.write(self.fd, item("minm", "Björk — Jóga"))
        self.assertTrue(self._wait(
            lambda: self.reader.now_playing.title == "Björk — Jóga"))

    def test_unknown_codes_ignored(self):
        os.write(self.fd, item("zzzz", "junk") + item("minm", "Real"))
        self.assertTrue(self._wait(lambda: self.reader.now_playing.title == "Real"))

    def test_dacp_credentials_captured(self):
        """Without these, transport control is impossible."""
        os.write(self.fd, item("daid", "ABC123DEF") + item("acre", "998877"))
        self.assertTrue(self._wait(lambda: self.reader.dacp_id == "ABC123DEF"))
        self.assertEqual(self.reader.active_remote, "998877")
        self.assertEqual(self.reader.remote(), ("ABC123DEF", "998877"))

    def test_artwork_captured_with_mime(self):
        os.write(self.fd, item("PICT", JPEG))
        self.assertTrue(self._wait(lambda: self.reader.artwork.data == JPEG))
        data, mime = self.reader.artwork_bytes()
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data, JPEG)

    def test_artwork_version_bumps_on_change(self):
        os.write(self.fd, item("PICT", JPEG))
        self.assertTrue(self._wait(lambda: self.reader.artwork.version == 1))
        os.write(self.fd, item("PICT", PNG))
        self.assertTrue(self._wait(lambda: self.reader.artwork.version == 2))
        self.assertEqual(self.reader.artwork.mime, "image/png")

    def test_identical_artwork_does_not_bump_version(self):
        """The page reloads the image whenever the version changes, so a
        repeat of the same art must not force a needless refetch."""
        os.write(self.fd, item("PICT", JPEG))
        self.assertTrue(self._wait(lambda: self.reader.artwork.version == 1))
        os.write(self.fd, item("PICT", JPEG))
        time.sleep(0.5)
        self.assertEqual(self.reader.artwork.version, 1)

    def test_reset_track_clears_art_and_text(self):
        os.write(self.fd, item("minm", "Song") + item("PICT", JPEG))
        self.assertTrue(self._wait(lambda: bool(self.reader.artwork.data)))
        self.reader.reset_track()
        self.assertFalse(self.reader.artwork.data)
        self.assertEqual(self.reader.now_playing.title, "")

    def test_snapshot_shape(self):
        os.write(self.fd, item("minm", "Song") + item("PICT", PNG))
        self.assertTrue(self._wait(lambda: self.reader.snapshot()["artwork"]))
        snap = self.reader.snapshot()
        self.assertEqual(snap["title"], "Song")
        self.assertTrue(snap["artwork"])
        self.assertGreater(snap["artwork_version"], 0)


if __name__ == "__main__":
    unittest.main()
