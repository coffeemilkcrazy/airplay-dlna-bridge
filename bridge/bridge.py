#!/usr/bin/env python3
"""AirPlay -> UPnP/DLNA renderer bridge.

Runs on the Raspberry Pi and makes the soundbar appear as an AirPlay speaker:

    AirPlay source   --AirPlay-->  shairport-sync  --PCM-->  this bridge
                                                                 |
                                          live WAV over HTTP     v
                                 UPnP/DLNA renderer  <--UPnP push--

shairport-sync handles the AirPlay protocol and writes raw PCM
(44100 Hz, 16-bit, stereo, little-endian) to stdout. We fan that out over an
endless WAV stream and point the soundbar at it with UPnP AVTransport.

This module is orchestration only. The parts live next door:
    config.py    every setting, declared once
    soundbar.py  UPnP / Samsung WAM control
    streamer.py  PCM fan-out and the WAV server
    metadata.py  shairport metadata pipe: track info, cover art, DACP creds
    dacp.py      play/pause/skip, sent back to the AirPlay sender
    api.py       HTTP status API and web panel
    webui.py     the panel itself

    python3 bridge.py                       # discover the soundbar
    python3 bridge.py --soundbar 192.0.2.10
    python3 bridge.py --no-shairport        # bring your own PCM on stdin
"""

from __future__ import annotations

import array
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api                                                   # noqa: E402
from config import APP_VERSION, Config                       # noqa: E402
from dacp import DacpRemote                                  # noqa: E402
from metadata import MetadataReader                          # noqa: E402
from soundbar import Soundbar, SoundbarError                 # noqa: E402
from streamer import (LiveWavServer, PcmBroadcaster,         # noqa: E402
                      WAV_FEATURES, WAV_MIME)

log = logging.getLogger("bridge")

RATE, CHANNELS, BITS = 44100, 2, 16
CHUNK = 4096

# Status polling must fail fast and must not hammer the soundbar.
STATUS_TIMEOUT = 2.0
STATUS_CACHE_SECONDS = 2.0
# Don't re-run SSDP on every failed engage attempt.
REDISCOVER_COOLDOWN = 30.0


class ReengagePolicy:
    """Decides whether to pull the soundbar back onto our stream.

    Re-engaging is right when the renderer drops out on its own - a transient
    network blip, or the stream being reset. It is wrong when the user has
    deliberately switched the soundbar to TV or Bluetooth while an AirPlay
    session is still alive: then every retry yanks their input back and it
    becomes a tug of war.

    So: retry a few times, then stand down until the next session.
    """

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts
        self.attempts = 0
        self.backed_off = False

    def reset(self) -> None:
        self.attempts = 0
        self.backed_off = False

    def may_engage(self) -> bool:
        return not self.backed_off

    def record_engage(self) -> None:
        self.attempts += 1

    def record_left(self) -> bool:
        """Renderer left our stream. Returns True if we should try again."""
        if self.attempts >= self.max_attempts:
            self.backed_off = True
            return False
        return True


def local_ip_towards(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# --------------------------------------------------------------------------- #
class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bar: Soundbar | None = None
        self.broadcaster = PcmBroadcaster(rate=RATE, channels=CHANNELS, bits=BITS)
        self.server: LiveWavServer | None = None
        self.metadata = MetadataReader(cfg.metadata_pipe)
        self.dacp = DacpRemote()
        self.session_active = False
        self.last_error = ""

        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._status_httpd = None
        self._shairport_stderr: deque[str] = deque(maxlen=12)
        self._fmt_probe = bytearray()
        self._fmt_checked = False
        self._bar_cache = {"state": "unknown", "volume": None, "muted": None,
                           "elapsed": ""}
        self._bar_cache_at = 0.0
        self._bar_lock = threading.Lock()
        self._last_rediscover = 0.0

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        self._resolve_soundbar()

        if not self.cfg.advertise_ip:
            self.cfg.advertise_ip = local_ip_towards(self.bar.ip)
        log.info("advertising stream from %s", self.cfg.advertise_ip)

        self.server = LiveWavServer(self.broadcaster, port=self.cfg.stream_port,
                                    path="/airplay.wav")
        self.server.start()

        self.metadata.start()
        threading.Thread(target=self._session_loop, daemon=True).start()
        threading.Thread(target=self._serve_status, daemon=True).start()

        if self.cfg.run_shairport:
            self._run_shairport()
        else:
            log.info("reading PCM from stdin (--no-shairport)")
            self._pump(sys.stdin.buffer)

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("shutting down")
        self.metadata.stop()
        self._release_soundbar()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self.server:
            self.server.stop()
        if self._status_httpd:
            self._status_httpd.shutdown()

    # -- soundbar -------------------------------------------------------- #
    def _resolve_soundbar(self) -> None:
        if self.cfg.soundbar_ip:
            self.bar = Soundbar(ip=self.cfg.soundbar_ip)
            log.info("using soundbar at %s", self.bar.ip)
            return
        log.info("discovering soundbar ...")
        found = Soundbar.discover()
        if not found:
            raise SystemExit(
                "No Samsung MediaRenderer found. Pass --soundbar <ip>, and "
                "check the soundbar is powered on and on the same network.")
        self.bar = found[0]
        log.info("found %s (%s) at %s", self.bar.name, self.bar.model,
                 self.bar.ip)

    def _stream_url(self) -> str:
        return self.server.url_for(self.cfg.advertise_ip)

    def _rediscover(self) -> bool:
        """Look for the soundbar again after losing contact.

        The configured IP is pinned in bridge.env, so a DHCP reassignment
        would otherwise break the bridge permanently.
        """
        now = time.monotonic()
        if now - self._last_rediscover < REDISCOVER_COOLDOWN:
            return False
        self._last_rediscover = now

        log.info("lost contact with the soundbar - re-discovering ...")
        try:
            found = Soundbar.discover(timeout=3)
        except OSError as e:
            log.debug("discovery failed: %s", e)
            return False

        for cand in found:
            if cand.product_type and cand.product_type != "SOUNDBAR":
                continue
            old = self.bar.ip if self.bar else "?"
            if cand.ip != old:
                log.info("soundbar moved %s -> %s (%s)", old, cand.ip, cand.name)
            self.bar = cand
            return True

        log.warning("re-discovery found no soundbar")
        return False

    def _engage_soundbar(self) -> bool:
        for attempt in (1, 2):
            url = self._stream_url()
            try:
                self.bar.set_uri(url, Soundbar.didl(
                    url, self.cfg.airplay_name, WAV_MIME, WAV_FEATURES))
                self.bar.play()
                log.info("soundbar engaged -> %s", url)
                self._apply_min_volume()
                self.last_error = ""
                return True
            except SoundbarError as e:
                self.last_error = str(e)
                if attempt == 1 and self._rediscover():
                    continue          # found it at a new address; try again
                log.warning("could not engage soundbar: %s", e)
                return False
        return False

    def _apply_min_volume(self) -> None:
        """Optionally raise the soundbar to a minimum volume on session start.

        OFF by default (min_volume = 0) and that is usually right: anything
        set below the floor gets overridden, so it fights anyone using the
        remote. Only enable it where something else leaves the volume
        unusably low. Only ever raises, never lowers.
        """
        if self.cfg.min_volume <= 0:
            return
        floor = min(self.cfg.min_volume, self.cfg.max_volume)
        try:
            current = self.bar.get_volume()
            if current < floor:
                self.bar.set_volume(floor)
                log.info("raised soundbar volume %d -> %d (floor)",
                         current, floor)
        except SoundbarError as e:
            log.debug("min-volume check failed: %s", e)

    def _release_soundbar(self) -> None:
        try:
            if self.bar:
                self.bar.stop()
                log.info("soundbar released")
        except SoundbarError as e:
            log.debug("release failed: %s", e)
        # Stop alone is not enough: the soundbar keeps the HTTP connection
        # open, so we would go on feeding it silence and no other controller
        # could claim it.
        if self.server:
            self.server.disconnect_clients()

    # -- audio ----------------------------------------------------------- #
    def _run_shairport(self) -> None:
        exe = shutil.which(self.cfg.shairport_bin)
        if not exe:
            raise SystemExit(
                f"{self.cfg.shairport_bin!r} not found.\n"
                "Install it on the Pi with:  sudo ./deploy.sh")

        cmd = [exe, "-o", "stdout", "-a", self.cfg.airplay_name]
        if self.cfg.shairport_config and os.path.exists(self.cfg.shairport_config):
            cmd += ["-c", self.cfg.shairport_config]
        log.info("starting: %s", " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            self._pump(self._proc.stdout)
        finally:
            stderr_thread.join(timeout=2)
            rc = self._proc.poll()
            if rc not in (None, 0) and not self._stop.is_set():
                self._report_shairport_failure(rc)

    def _report_shairport_failure(self, rc: int) -> None:
        detail = " | ".join(self._shairport_stderr) or "(no stderr output)"
        log.error("shairport-sync exited with code %s: %s", rc, detail)
        self.last_error = f"shairport-sync exited ({rc}): {detail}"
        if "port 5000" in detail or "another instance" in detail.lower():
            log.error(
                "AirPlay port 5000 is already taken - almost always the "
                "packaged shairport-sync service. Fix it with:\n"
                "    sudo systemctl disable --now shairport-sync\n"
                "    sudo systemctl mask shairport-sync\n"
                "    sudo systemctl restart airplay-soundbar")

    def _drain_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for raw in self._proc.stderr:
            if self._stop.is_set():
                return
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            self._shairport_stderr.append(line)
            # shairport-sync is quiet unless something is wrong.
            log.info("shairport: %s", line)

    def _pump(self, stream) -> None:
        log.info("waiting for AirPlay audio ...")
        while not self._stop.is_set():
            data = stream.read(CHUNK)
            if not data:
                break
            self.broadcaster.write(data)
            if not self._fmt_checked:
                self._probe_input_format(data)

    # -- input sanity ----------------------------------------------------- #
    @staticmethod
    def _lag1_correlation(samples) -> float:
        """Adjacent-sample correlation. Real 44.1 kHz audio is highly
        correlated (>0.7); bytes read under the wrong format look like noise
        (near 0)."""
        n = len(samples)
        if n < 2000:
            return 0.0
        mean = sum(samples) / n
        num = 0.0
        prev = samples[0] - mean
        sq = prev * prev
        for i in range(1, n):
            cur = samples[i] - mean
            num += prev * cur
            sq += cur * cur
            prev = cur
        return num / sq if sq else 0.0

    def _probe_input_format(self, data: bytes) -> None:
        """Verify the incoming PCM really is S16_LE stereo.

        A mismatch is silent and baffling from outside: the soundbar happily
        "plays" the stream and emits noise. shairport-sync's stdout backend
        defaults to S32_LE @ 48000 under AirPlay 2, so this catches a mis-set
        `stdout` stanza instead of leaving it to be diagnosed by ear.
        """
        if self._fmt_checked:
            return
        self._fmt_probe += data
        if len(self._fmt_probe) < self.broadcaster.bytes_per_second // 2:
            return
        self._fmt_checked = True
        raw = bytes(self._fmt_probe)
        self._fmt_probe.clear()

        try:
            as16 = array.array("h")
            as16.frombytes(raw[:len(raw) - len(raw) % 4])
            corr16 = self._lag1_correlation(as16[0::2])
            if corr16 > 0.5:
                log.info("input format OK (S16_LE stereo, corr %.2f)", corr16)
                return
            as32 = array.array("i")
            as32.frombytes(raw[:len(raw) - len(raw) % 8])
            corr32 = self._lag1_correlation(as32[0::2])
        except (ValueError, OverflowError):
            return

        if corr32 > 0.5 >= corr16:
            log.error(
                "INPUT FORMAT MISMATCH: audio looks like S32_LE (corr %.2f), "
                "not the S16_LE we advertise (corr %.2f). The soundbar will "
                "play noise. Fix the 'stdout' stanza in %s:\n"
                "    stdout = { output_rate = %d; output_format = \"S16_LE\"; "
                "output_channels = %d; };",
                corr32, corr16, self.cfg.shairport_config, RATE, CHANNELS)
            self.last_error = "input format mismatch: S32_LE, expected S16_LE"
        else:
            log.warning(
                "input audio does not correlate as S16_LE (%.2f) or S32_LE "
                "(%.2f) - could be silence, or an unexpected rate/channel "
                "count", corr16, corr32)

    # -- session reconciliation ------------------------------------------ #
    def _session_loop(self) -> None:
        """Keep the soundbar engaged exactly while audio is flowing.

        shairport-sync emits nothing between AirPlay sessions, so the age of
        the last PCM write is a reliable liveness signal.
        """
        engaged = False
        last_check = 0.0
        policy = ReengagePolicy()
        while not self._stop.is_set():
            time.sleep(1.0)
            quiet = self.broadcaster.seconds_since_audio
            want = quiet < self.cfg.idle_stop_seconds

            if want != self.session_active:
                self.session_active = want
                log.info("AirPlay session %s", "started" if want else "ended")
                if not want:
                    self.metadata.reset_track()
                    # Re-probe next session: shairport-sync can renegotiate
                    # rate/format between senders.
                    self._fmt_checked = False
                    self._fmt_probe.clear()
                    policy.reset()

            if want and not engaged and policy.may_engage():
                engaged = self._engage_soundbar()
                policy.record_engage()
                last_check = time.monotonic()
            elif not want and engaged:
                self._release_soundbar()
                engaged = False
            elif engaged and time.monotonic() - last_check > 10:
                last_check = time.monotonic()
                try:
                    if self.bar.transport_state() != "PLAYING":
                        if policy.record_left():
                            log.info("soundbar left our stream - re-engaging")
                            engaged = self._engage_soundbar()
                            policy.record_engage()
                        else:
                            # Deliberate input change, most likely. Let go
                            # rather than fight; no Stop, since the soundbar
                            # is now busy with something else.
                            log.info("soundbar left our stream %d times - "
                                     "backing off. Stop and restart playback "
                                     "to reconnect.", policy.attempts)
                            engaged = False
                except SoundbarError:
                    engaged = False

    # -- status ----------------------------------------------------------- #
    def invalidate_soundbar_cache(self) -> None:
        """Force the next /status to re-read the device.

        Without this, a client that sets the volume and immediately polls gets
        the pre-change cached value back and its slider snaps backwards.
        """
        with self._bar_lock:
            self._bar_cache_at = 0.0

    def _soundbar_state(self) -> dict:
        """Soundbar state, cached briefly and fetched with a short timeout.

        /status is polled every couple of seconds. Querying the device every
        time means a powered-off soundbar stalls each request for the full
        UPnP timeout, and pollers pile up faster than they drain.
        """
        now = time.monotonic()
        with self._bar_lock:
            if now - self._bar_cache_at < STATUS_CACHE_SECONDS:
                return dict(self._bar_cache)

            result = {"state": "unknown", "volume": None, "muted": None,
                      "elapsed": ""}
            if self.bar:
                try:
                    result["state"] = self.bar.transport_state(
                        timeout=STATUS_TIMEOUT)
                    result["volume"] = self.bar.get_volume(timeout=STATUS_TIMEOUT)
                    result["muted"] = self.bar.get_mute(timeout=STATUS_TIMEOUT)
                except SoundbarError as e:
                    result["state"] = f"error: {e}"
                if result["state"] == "PLAYING":
                    try:
                        result["elapsed"] = self.bar.position(
                            timeout=STATUS_TIMEOUT).get("elapsed", "")
                    except SoundbarError:
                        pass
            self._bar_cache = result
            self._bar_cache_at = time.monotonic()
            return dict(result)

    def snapshot(self) -> dict:
        bar = self._soundbar_state()
        meta = self.metadata.snapshot()
        dacp_id, token = self.metadata.remote()
        return {
            "airplay_name": self.cfg.airplay_name,
            "version": APP_VERSION,
            "revision": self.cfg.version,
            "session_active": self.session_active,
            "now_playing": {"title": meta["title"], "artist": meta["artist"],
                            "album": meta["album"]},
            "artwork": {"available": meta["artwork"],
                        "version": meta["artwork_version"]},
            "transport": {"available": self.dacp.available(dacp_id, token)},
            "soundbar": {
                "ip": self.bar.ip if self.bar else "",
                "model": self.bar.model if self.bar else "",
                "state": bar["state"],
                "volume": bar["volume"],
                "muted": bar["muted"],
                "elapsed": bar["elapsed"],
                "max_volume": self.cfg.max_volume,
            },
            "stream": {
                "url": self._stream_url() if self.server else "",
                # 'connections' is a lifetime total; 'active' is how many
                # renderers are attached right now.
                "connections": self.server.connections if self.server else 0,
                "active": self.broadcaster.client_count,
                "bytes": self.server.bytes_streamed if self.server else 0,
            },
            "last_error": self.last_error,
        }

    def create_status_server(self):
        """Bind the status server without starting to serve.

        Separated from _serve_status so a caller can know the socket is
        listening before it uses it. Binding inside the serving thread means
        anyone connecting immediately afterwards is racing thread startup,
        which shows up as an inexplicable connection timeout on a loaded
        machine rather than as an obvious failure.
        """
        if self._status_httpd is None:
            self._status_httpd = api.make_server(self)
        return self._status_httpd

    def _serve_status(self) -> None:
        try:
            self.create_status_server()
        except OSError as e:
            log.warning("status API disabled: %s", e)
            return
        log.info("web panel on http://%s:%d/%s", self.cfg.status_bind,
                 self.cfg.status_port,
                 " (token required)" if self.cfg.status_token else "")
        self._status_httpd.serve_forever()


# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = Config.from_args()
    bridge = Bridge(cfg)

    def handle_signal(signum, _frame):
        log.info("signal %s", signum)
        bridge.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        bridge.start()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
