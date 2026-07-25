#!/usr/bin/env python3
"""Serve the web panel against a fake bridge, for screenshots and UI work.

The page and the API responses are the genuine article - this imports api.py
and webui.py directly - but the values behind them are invented, so a
published screenshot leaks no real addresses, device names or tracks.

    python3 tools/demo-panel.py              # playing, with artwork
    python3 tools/demo-panel.py --idle       # nothing playing
    python3 tools/demo-panel.py --port 9000

Then open the printed URL. Ctrl-C to stop.
"""
import argparse
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import api  # noqa: E402


def demo_artwork(size: int = 320) -> bytes:
    """A plausible album cover: a diagonal gradient, written as a real PNG so
    the panel decodes it exactly as it would decode Apple's."""
    rows = []
    for y in range(size):
        row = bytearray([0])                       # filter byte
        for x in range(size):
            t = (x + y) / (2 * size)
            row += bytes((int(40 + 150 * t),       # deep blue -> warm violet
                          int(50 + 40 * t),
                          int(120 + 110 * (1 - t))))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


ART = demo_artwork()


class FakeMetadata:
    def artwork_bytes(self):
        return ART, "image/png"

    def remote(self):
        return "DEMO", "DEMO"


class FakeDacp:
    def available(self, *_):
        return True

    def send(self, *_):
        return True, "demo"


ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--idle", action="store_true",
                help="render the nothing-playing state instead")
ap.add_argument("--port", type=int, default=8795)
ARGS = ap.parse_args()
PLAYING = not ARGS.idle


class FakeBridge:
    """Only the surface api.py actually touches."""

    class _Cfg:
        status_bind = "127.0.0.1"
        status_port = ARGS.port
        status_token = ""
        max_volume = 12

    cfg = _Cfg()
    bar = None
    metadata = FakeMetadata()
    dacp = FakeDacp()

    def invalidate_soundbar_cache(self):
        pass

    def snapshot(self):
        return {
            "airplay_name": "Living Room Soundbar",
            "version": "1.1",
            "revision": "a1b2c3d",
            "session_active": PLAYING,
            "now_playing": ({"title": "Midnight Signal", "artist": "The Quiet Hours",
                             "album": "Low Orbit"} if PLAYING else
                            {"title": "", "artist": "", "album": ""}),
            "artwork": {"available": PLAYING, "version": 1 if PLAYING else 0},
            "transport": {"available": PLAYING},
            "soundbar": {"ip": "192.0.2.10", "model": "DLNA Renderer",
                         "state": "PLAYING" if PLAYING else "STOPPED", "volume": 7, "muted": False,
                         "elapsed": "0:02:14" if PLAYING else "", "max_volume": 12},
            "stream": {"url": "http://192.0.2.5:8770/airplay.wav",
                       "connections": 1, "active": 1, "bytes": 23_068_672},
            "last_error": "",
        }


httpd = api.make_server(FakeBridge())
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print(f"demo panel on http://127.0.0.1:{ARGS.port}/  "
      f"({'playing' if PLAYING else 'idle'})", flush=True)
print("Ctrl-C to stop.", flush=True)
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
