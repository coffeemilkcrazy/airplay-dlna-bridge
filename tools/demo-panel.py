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
from config import (BY_ENV, Config, apply_settings,  # noqa: E402
                    describe_editable, env_text)


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
DEMO_NAME = "Living Room Soundbar"
DEMO_CFG = Config(airplay_name=DEMO_NAME)


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
            "airplay_name": DEMO_NAME,
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
            # Playing shows the default configuration, where auto-off is off.
            # A countdown only exists once a session has ended, so the idle
            # state is the only place one can honestly be shown.
            "power": {"auto_off_minutes": 0.0 if PLAYING else 30.0,
                      "off": False,
                      "seconds_until_off": None if PLAYING else 1080.0,
                      "last_result": ""},
            "stream": {"url": "http://192.0.2.5:8770/airplay.wav",
                       "connections": 1, "active": 1, "bytes": 23_068_672},
            "last_error": "",
        }

    def power_on(self, wait: bool = True):
        return True, "demo"

    def power_off(self, reason: str, manual: bool = False):
        return True, "demo"

    # The settings form is genuinely driven and genuinely validated - only the
    # writing is left out, so a demo run cannot rewrite a real bridge.env.
    def settings_snapshot(self):
        return {"settings": describe_editable(DEMO_CFG),
                "restart_pending": False,
                "config_file": "(demo - nothing is written)"}

    def update_settings(self, changes):
        applied, errors = apply_settings(DEMO_CFG, changes)
        if errors:
            return False, {"ok": False, "errors": errors}
        # Same comparison the real bridge makes: the panel posts every field,
        # so only what differs from the running value has actually changed.
        applied = {env: value for env, value in applied.items()
                   if value != getattr(DEMO_CFG, BY_ENV[env].name)}
        return True, {
            "ok": True,
            "applied": {k: env_text(v) for k, v in applied.items()},
            "restart_required": [k for k in applied if not BY_ENV[k].live],
        }

    def request_restart(self):
        return True, "demo - not really restarting"


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
