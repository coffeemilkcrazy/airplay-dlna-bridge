#!/usr/bin/env python3
"""End-to-end health check for the AirPlay -> DLNA renderer bridge.

Run this any time playback misbehaves. It reports, in order:

  1. can we find/reach the soundbar
  2. is its control plane answering (UPnP + WAM)
  3. is its playback engine idle and healthy
  4. will it actually fetch and sustain audio from the real streaming code

Step 4 drives bridge/streamer.py itself, so a pass here means the production
audio path works - not merely that some test harness works.

Usage:
    python3 tools/diagnose.py                # discover automatically
    python3 tools/diagnose.py 192.0.2.10     # target a known IP
    python3 tools/diagnose.py --no-audio     # skip the audible tone test
    python3 tools/diagnose.py --test-power   # also switch it off and on again
"""

from __future__ import annotations

import math
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from soundbar import Soundbar, SoundbarError                      # noqa: E402
from streamer import (LiveWavServer, PcmBroadcaster,              # noqa: E402
                      WAV_FEATURES, WAV_MIME)

RATE, CHANS = 44100, 2
TEST_PORT = 8199

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def ok(msg):
    print(f"  {GREEN}PASS{RESET}  {msg}")


def bad(msg):
    print(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg):
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def info(msg):
    print(f"  {DIM}····{RESET}  {msg}")


def local_ip_towards(host: str) -> str:
    """The address this machine uses to reach `host` - what we advertise."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def hhmmss_to_seconds(text: str) -> float:
    """'0:01:23.500' -> 83.5, or -1 when the renderer reports nothing useful."""
    try:
        parts = text.split(":")
        if len(parts) != 3:
            return -1.0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, AttributeError):
        return -1.0


def tone_producer(bc: PcmBroadcaster, stop: threading.Event,
                  freq: float = 440.0, amplitude: int = 1500) -> None:
    """Feed a quiet sine into the broadcaster in real time."""
    phase, step = 0.0, 2 * math.pi * freq / bc.rate
    next_tick = time.monotonic()
    frames = bc.rate // 10
    while not stop.is_set():
        next_tick += 0.1
        buf = bytearray()
        for _ in range(frames):
            v = int(amplitude * math.sin(phase))
            phase = (phase + step) % (2 * math.pi)
            buf += struct.pack("<hh", v, v)      # little-endian PCM for WAV
        bc.write(bytes(buf))
        time.sleep(max(0, next_tick - time.monotonic()))


def probe_power(bar) -> int:
    """Does this renderer accept a WAM power command at all?

    Opt-in, because passing means the speaker really does switch off. Whether
    HW-* soundbars implement SetPowerStatus is unverified - they answer only a
    subset of the WAM API and never reply at all to the rest - so this is how
    to find out for a given device instead of guessing. AUTO_OFF depends on the
    answer: no WAM power means POWER_OFF_COMMAND is the only route.
    """
    print(f"\n{BOLD}5. Power control{RESET}  "
          f"{DIM}the speaker will switch off and back on{RESET}")

    try:
        bar.wam_power(False)
        ok("WAM accepted power-off")
    except SoundbarError as e:
        bad(f"WAM power-off: {e}")
        print("      No network power-off of its own. Drive a smart plug or a")
        print("      hub instead, and the bridge will use that:")
        print("        POWER_OFF_COMMAND='curl -fsS -X POST http://plug/off' \\")
        print("        POWER_ON_COMMAND='curl -fsS -X POST http://plug/on' \\")
        print("        AUTO_OFF=30 ./deploy.sh")
        return 1

    # Accepting the command is not the same as acting on it - a device that
    # answers and stays on is exactly the silent failure this project keeps
    # running into, so confirm it actually went away.
    for _ in range(5):
        time.sleep(2)
        if not bar.is_reachable(timeout=2):
            ok("stopped answering - it really did power down")
            break
    else:
        warn("still answering: it accepted the command but stayed on")

    info("waking it again ...")
    try:
        bar.wam_power(True)
        ok("WAM accepted power-on")
    except SoundbarError as e:
        bad(f"WAM power-on: {e}")
        print("      It powers off but cannot be woken over the network. Set")
        print("      POWER_ON_COMMAND, or leave AUTO_OFF disabled - otherwise")
        print("      the speaker needs its remote after every idle period.")
        return 1

    for _ in range(8):
        time.sleep(2)
        if bar.is_reachable(timeout=2):
            ok("answering again")
            return 0
    warn("did not come back within the wait - check it by hand")
    return 1


# --------------------------------------------------------------------------- #
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    play_audio = "--no-audio" not in sys.argv
    test_power = "--test-power" in sys.argv
    failures = 0

    print(f"\n{BOLD}AirPlay bridge diagnostics{RESET}")

    # -- 1. locate ---------------------------------------------------------- #
    print(f"\n{BOLD}1. Locating the soundbar{RESET}")
    if args:
        bar = Soundbar(ip=args[0])
        info(f"using {bar.ip} (supplied)")
    else:
        found = Soundbar.discover()
        if not found:
            bad("no suitable UPnP MediaRenderer found via SSDP")
            print("\n  Is the soundbar powered on and on the same network?")
            print("  If it is mid-reboot, wait ~30s for Wi-Fi to reconnect.")
            return 1
        bar = found[0]
        kind = bar.product_type or "unknown type"
        ok(f"found {bar.name or 'device'} ({bar.model or '?'}, {kind}) "
           f"at {bar.ip}")
        for extra in found[1:]:
            info(f"also saw {extra.name} ({extra.product_type or '?'}) "
                 f"at {extra.ip}")
        if bar.product_type != "SOUNDBAR":
            warn(f"this is a {kind}, not a soundbar - a Samsung TV will accept "
                 "Play and then never fetch the media")
            print(f"      Target the soundbar explicitly: "
                  f"python3 tools/diagnose.py <soundbar-ip>")

    for label, port in (("UPnP MediaRenderer", bar.dmr_port),
                        ("Samsung WAM API", bar.wam_port)):
        if bar.is_reachable(port):
            ok(f"{label} reachable on port {port}")
        else:
            warn(f"{label} NOT reachable on port {port}")

    # -- 2. control plane --------------------------------------------------- #
    print(f"\n{BOLD}2. Control plane{RESET}")
    try:
        ok(f"UPnP volume readable (currently {bar.get_volume()})")
    except SoundbarError as e:
        bad(f"UPnP volume: {e}")
        failures += 1

    try:
        func, submode = bar.wam_function()
        ok(f"WAM reports function={func!r} submode={submode!r}")
        if func != "wifi":
            warn(f"source is {func!r}, not 'wifi' - switch the soundbar to Wi-Fi")
    except SoundbarError as e:
        warn(f"WAM GetFunc: {e}")

    try:
        meta = bar.wam_info()
        info(f"model {meta['model']}  mac {meta['mac']}")
    except SoundbarError:
        pass

    # -- 3. playback engine ------------------------------------------------- #
    print(f"\n{BOLD}3. Playback engine{RESET}")
    engine_ok = True
    try:
        bar.wam("<name>GetPlayStatus</name>", timeout=6)
        ok("WAM GetPlayStatus answered")
    except SoundbarError:
        # Soundbars (HW-*) implement a smaller WAM command set than the
        # WAM/R-series speakers this API targets. A timeout is normal here and
        # is NOT on its own evidence of a fault.
        info("WAM GetPlayStatus did not answer (not implemented on HW-* models)")

    try:
        state = bar.transport_state()
        ok(f"UPnP transport state = {state}")
        if state == "TRANSITIONING":
            warn("stuck in TRANSITIONING - a previous session never completed")
            engine_ok = False
    except SoundbarError as e:
        bad(f"UPnP transport state: {e}")
        failures += 1

    if not engine_ok:
        print(f"\n  {YELLOW}The soundbar's media engine is wedged.{RESET}")
        print("  Fix: unplug the soundbar at the wall for 30 seconds, then plug")
        print("  it back in. The remote's power button is NOT enough - network")
        print("  standby preserves the stuck session. Then re-run this script.")
        return 1

    # -- 4. real playback --------------------------------------------------- #
    if not play_audio:
        print(f"\n{GREEN}Control plane healthy.{RESET} "
              "Re-run without --no-audio to test playback.")
        return probe_power(bar) if test_power else 0

    print(f"\n{BOLD}4. Playback test{RESET}  (quiet 440 Hz tone, ~16s)")
    print(f"  {DIM}drives bridge/streamer.py - the real audio path{RESET}")

    advertise_ip = local_ip_towards(bar.ip)
    bc = PcmBroadcaster(rate=RATE, channels=CHANS)
    server = LiveWavServer(bc, port=TEST_PORT, path="/diag.wav")
    server.start()
    url = server.url_for(advertise_ip)
    info(f"serving {url}")

    stop = threading.Event()
    threading.Thread(target=tone_producer, args=(bc, stop), daemon=True).start()

    try:
        bar.stop()
    except SoundbarError:
        pass
    time.sleep(0.5)

    try:
        bar.set_uri(url, Soundbar.didl(url, "Bridge diagnostic",
                                       WAV_MIME, WAV_FEATURES))
        bar.play()
        play_at = time.monotonic()
        ok("SetAVTransportURI + Play accepted")
    except SoundbarError as e:
        bad(f"push failed: {e}")
        stop.set()
        server.stop()
        return 1

    states: list[str] = []
    lags: list[float] = []
    for i in range(8):
        time.sleep(2)
        try:
            st = bar.transport_state()
        except SoundbarError:
            st = "?"
        states.append(st)

        # The soundbar reports how far into the stream it has played. The gap
        # between that and wall-clock since Play is everything between us and
        # the speaker: its jitter buffer plus decode.
        lag_text = ""
        try:
            pos = hhmmss_to_seconds(bar.position()["elapsed"])
            if pos >= 0 and st == "PLAYING":
                lag = (time.monotonic() - play_at) - pos
                lags.append(lag)
                lag_text = f"  lag={lag:.1f}s"
        except SoundbarError:
            pass

        info(f"t+{(i + 1) * 2:2}s  state={st:<15} "
             f"streamed={server.bytes_streamed:,} B  conns={server.connections}"
             f"{lag_text}")

    playing = sum(1 for s in states if s == "PLAYING")
    streamed = server.bytes_streamed
    conns = server.connections

    stop.set()
    try:
        bar.stop()
    except SoundbarError:
        pass
    server.stop()

    print()
    if playing >= len(states) - 1 and streamed > 500_000:
        ok(f"sustained playback: {playing}/{len(states)} samples PLAYING, "
           f"{streamed / 1e6:.1f} MB streamed")
        if conns == 1:
            ok("single connection held throughout - no re-buffering")
        else:
            warn(f"{conns} connections - the soundbar reconnected mid-stream")

        if lags:
            steady = sum(lags[-3:]) / len(lags[-3:])
            info(f"soundbar-side latency ~{steady:.1f}s "
                 "(its buffer + decode; AirPlay adds ~2s more upstream)")
    elif conns == 0:
        bad("soundbar never connected to the stream server")
        print("      Most likely another controller already holds it - if the")
        print("      Pi bridge has a live session it will keep re-engaging and")
        print("      win. Check with:")
        print("        curl -s http://<pi>:8772/status | python3 -m json.tool")
        print(f"      Otherwise check nothing blocks inbound TCP {TEST_PORT} "
              f"on {advertise_ip}.")
        failures += 1
    elif streamed < 500_000:
        bad(f"soundbar connected but pulled only {streamed:,} bytes")
        print("      It fetched then gave up - usually a format it will not decode.")
        failures += 1
    else:
        bad(f"unstable: states={states}")
        failures += 1

    if test_power:
        failures += probe_power(bar)

    print()
    if failures == 0:
        print(f"{GREEN}{BOLD}All checks passed.{RESET}")
    else:
        print(f"{RED}{failures} check(s) failed.{RESET}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
