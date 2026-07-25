#!/usr/bin/env python3
"""Measure the real signal level in the bridge's live stream.

Answers "is there actually audio in there?" without relying on ears, and
distinguishes the failure modes that look identical from the outside:

    pure zeros        nothing is arriving from shairport-sync
    very low dBFS     upstream attenuation (the Mac's AirPlay volume slider)
    healthy dBFS      stream is fine - any silence is the soundbar's volume
    low correlation   sample-format mismatch, i.e. it is noise not music

Usage:
    python3 tools/level.py                 # uses PI_HOST or the default
    python3 tools/level.py raspberrypi.local
    python3 tools/level.py --wait          # wait for a session to start first
"""

from __future__ import annotations

import array
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PI = os.environ.get("PI_HOST", "raspberrypi.local")
STATUS_PORT = 8772
STREAM_PORT = 8770
SECONDS = 6
BYTES_PER_SECOND = 44100 * 2 * 2

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def dbfs(v: float) -> float:
    return -999.0 if v <= 0 else 20 * math.log10(v / 32768.0)


def status(pi: str) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://{pi}:{STATUS_PORT}/status", timeout=6) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def capture(pi: str, seconds: int) -> bytes:
    want = 44 + BYTES_PER_SECOND * seconds
    buf = bytearray()
    with urllib.request.urlopen(
            f"http://{pi}:{STREAM_PORT}/airplay.wav", timeout=25) as r:
        while len(buf) < want:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
    return bytes(buf[44:])          # drop the WAV header


def analyse(body: bytes) -> dict:
    samples = array.array("h")
    samples.frombytes(body[:len(body) - len(body) % 4])
    left = samples[0::2]
    n = len(left)
    if n < 1000:
        raise ValueError("not enough audio captured")

    peak = max(max(left), -min(left))
    mean = sum(left) / n
    num = sq = 0.0
    prev = left[0] - mean
    sq += prev * prev
    for i in range(1, n):
        cur = left[i] - mean
        num += prev * cur
        sq += cur * cur
        prev = cur

    return {
        "samples": n,
        "peak": peak,
        "rms": math.sqrt(sq / n),
        "zeros": sum(1 for x in left if x == 0) / n,
        "corr": num / sq if sq else 0.0,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pi = args[0] if args else DEFAULT_PI
    wait = "--wait" in sys.argv

    st = status(pi)
    if st is None:
        print(f"{RED}Bridge unreachable at {pi}:{STATUS_PORT}{RESET}")
        return 1

    if wait and not st["session_active"]:
        print("waiting up to 180s for an AirPlay session ...", flush=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            st = status(pi) or st
            if st["session_active"]:
                break
            time.sleep(2)

    bar = st["soundbar"]
    print(f"{BOLD}bridge{RESET}   session={st['session_active']}  "
          f"conns={st['stream']['connections']}")
    print(f"{BOLD}soundbar{RESET} state={bar['state']}  volume={bar['volume']}  "
          f"muted={bar['muted']}")

    if not st["session_active"]:
        print(f"\n{YELLOW}No AirPlay session active{RESET} - the stream is "
              "silence keepalive, so a reading now proves nothing.")
        print("Start playing, or re-run with --wait.")
        return 1

    print(f"\ncapturing {SECONDS}s from {pi}:{STREAM_PORT} ...")
    try:
        m = analyse(capture(pi, SECONDS))
    except (ValueError, OSError) as e:
        print(f"{RED}capture failed: {e}{RESET}")
        return 1

    print(f"\n  samples    : {m['samples']:,}")
    print(f"  peak       : {m['peak']:6d}   ({dbfs(m['peak']):7.1f} dBFS)")
    print(f"  rms        : {m['rms']:9.1f}   ({dbfs(m['rms']):7.1f} dBFS)")
    print(f"  zeros      : {m['zeros'] * 100:.1f}%")
    print(f"  lag-1 corr : {m['corr']:.3f}")

    print()
    rms_db = dbfs(m["rms"])
    if m["peak"] == 0:
        print(f"{RED}Pure digital silence{RESET} - nothing arriving from "
              "shairport-sync.")
    elif m["corr"] < 0.5:
        print(f"{RED}Not coherent audio{RESET} (corr {m['corr']:.3f}) - this is "
              "noise, i.e. a sample-format mismatch.")
        print("Check the 'stdout' stanza pins S16_LE @ 44100.")
    elif rms_db < -50:
        print(f"{YELLOW}Audio present but very quiet{RESET} ({rms_db:.0f} dBFS) "
              "- upstream attenuation from the Mac's AirPlay volume slider.")
    else:
        print(f"{GREEN}Healthy signal ({rms_db:.0f} dBFS, corr "
              f"{m['corr']:.2f}).{RESET} The stream is fine.")
        if isinstance(bar["volume"], int) and bar["volume"] < 10:
            print(f"{YELLOW}Soundbar volume is {bar['volume']}{RESET} - likely "
                  "inaudible. Raise it:")
            print(f"    curl -s -X POST http://{pi}:{STATUS_PORT}/volume/25")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
