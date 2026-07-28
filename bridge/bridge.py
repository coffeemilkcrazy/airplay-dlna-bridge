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
from config import (APP_VERSION, BY_ENV, Config,             # noqa: E402
                    apply_settings, config_writable,
                    describe_editable, env_text, read_env_file,
                    write_env_file)
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
# A power command talks to a plug or a hub, so it should be quick. Bound it:
# the session loop waits on it.
POWER_COMMAND_TIMEOUT = 20.0
# How long a woken renderer gets to rejoin the network before we push to it.
WAKE_TIMEOUT = 20.0
WAKE_POLL_SECONDS = 1.0
# Long enough for the HTTP response to reach the panel before we exit.
RESTART_DELAY = 0.4
# The WAM 'function' that means network audio - the only input that is ours.
# Everything else (hdmi, bt, optical, aux, soundshare, ...) is someone using
# the speaker for something this bridge cannot see.
NETWORK_INPUT = "wifi"

# Test tone: long enough to be unmistakable, short enough not to be a nuisance.
TEST_TONE_SECONDS = 2.0
TEST_TONE_HZ = 440.0
# ~-12 dBFS peak: clearly audible on a speaker at a normal setting, without
# being alarming on one that someone has turned up.
TEST_TONE_AMPLITUDE = 0.25
# How long a renderer gets to fetch the stream before we play into nothing.
TEST_TONE_CLIENT_WAIT = 4.0


def restart_process() -> None:
    """Exit so the service manager starts us again.

    Exiting is the portable way to restart: the systemd unit is Restart=always
    and the LaunchAgent is KeepAlive, so both bring the bridge straight back,
    and neither needs this process to shell out to a service manager it may not
    be allowed to drive. Run from a terminal with no supervisor, it just stops
    - which the API response says.
    """
    os._exit(0)


def run_command(command: str) -> tuple[bool, str]:
    """Run a user-configured power command.

    The only place this process runs a shell, so it is the only place to audit.
    shell=True is deliberate and not an injection hole: the string is a whole
    command the operator wrote (`curl -X POST http://plug/off`, a pipeline, an
    IR blaster invocation), it is read from the root-owned config file that
    install.sh writes, and no API route can set or influence it. Splitting it
    into an argv list would break every command that needs a shell without
    closing anything, since there is no untrusted input to inject.

    The command itself is never logged: it typically carries a webhook URL with
    a token in it, and the log goes to the journal.
    """
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              timeout=POWER_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"command timed out after {POWER_COMMAND_TIMEOUT:.0f}s"
    except OSError as e:
        return False, f"command could not run: {e}"

    if proc.returncode == 0:
        return True, "command succeeded"
    lines = proc.stderr.decode(errors="ignore").strip().splitlines()
    return False, f"command exited {proc.returncode}: {lines[-1] if lines else '(no stderr)'}"


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


class AutoOffPolicy:
    """Decides when to power the renderer off after a spell of silence.

    Two rules matter more than the timer itself.

    *Never before the first session.* PcmBroadcaster.seconds_since_audio is
    infinite until something has played, so a bare threshold check would power
    the speaker off shortly after every restart - conceivably in the middle of
    a film someone is watching on it. The countdown only starts once this
    process has held a session and that session has ended.

    *One attempt per idle period.* Whether the attempt worked or not, it is not
    repeated until audio returns. A speaker the user switched back on by hand
    is left alone, and a method that does not work does not fail every second
    for the rest of the night.
    """

    def __init__(self, seconds: float):
        self.seconds = max(0.0, seconds)
        self.armed = False
        self.fired = False
        self.disabled_reason = ""

    @property
    def enabled(self) -> bool:
        return self.seconds > 0 and not self.disabled_reason

    def disable(self, reason: str) -> None:
        """Stop trying for the life of the process. Used when there is no way
        to power this device off at all - a fact that will not change until
        someone reconfigures and restarts the bridge."""
        self.disabled_reason = reason

    def session_started(self) -> None:
        self.armed = False
        self.fired = False

    def session_ended(self) -> None:
        self.armed = True
        self.fired = False

    def should_fire(self, quiet_seconds: float) -> bool:
        return (self.enabled and self.armed and not self.fired
                and quiet_seconds >= self.seconds)

    def record_fired(self) -> None:
        self.fired = True

    def seconds_remaining(self, quiet_seconds: float) -> float | None:
        """Countdown for /status, or None when there is nothing to count down.

        Returning None rather than a number is what lets the web panel show or
        hide the countdown without reimplementing the arming rules.
        """
        if not self.enabled or not self.armed or self.fired:
            return None
        return max(0.0, self.seconds - quiet_seconds)


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
        self.auto_off = AutoOffPolicy(cfg.auto_off_seconds)
        self.powered_off = False
        self.power_result = ""       # last power action, surfaced in /status
        self._off_method = ""        # "wam" | "command"; its inverse wakes it
        self._suppress_wake = False  # an explicit off outranks a live session
        self._input_readable = False  # has this renderer ever reported its input
        self.test_tone_playing = False
        self.test_tone_result = ""   # last tone verdict, surfaced in /status
        self._tone_lock = threading.Lock()

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

    # -- power ----------------------------------------------------------- #
    def power_off(self, reason: str, manual: bool = False) -> tuple[bool, str]:
        """Power the renderer down: WAM first, the configured command second.

        Which one succeeds is remembered, because only its inverse can be
        relied on to undo it. A smart plug that cut the power leaves nothing on
        the network to answer WAM, so waking has to go back through the plug.

        `manual` marks a request from the API. A live AirPlay session would
        otherwise wake the speaker again on the very next tick, so an explicit
        off holds until the user asks for it back or a new session begins.
        """
        if self.powered_off:
            return True, "already off"

        wam_detail = ""
        if self.bar:
            try:
                self.bar.wam_power(False)
                return self._record_power_off("wam", reason, manual)
            except SoundbarError as e:
                wam_detail = str(e)

        if self.cfg.power_off_command:
            ok, detail = run_command(self.cfg.power_off_command)
            if ok:
                return self._record_power_off("command", reason, manual)
            return self._power_failed(f"power-off command failed: {detail}")

        # Nothing available. Say so once and stop asking: this is a
        # configuration fact, not a transient failure.
        self.auto_off.disable("no power-off method available")
        return self._power_failed(
            "cannot power this renderer off: WAM did not answer"
            + (f" ({wam_detail})" if wam_detail else "")
            + " and POWER_OFF_COMMAND is not set")

    def power_on(self, wait: bool = True) -> tuple[bool, str]:
        """Wake the renderer with the inverse of whatever powered it off.

        `wait` is for the session loop, which must not push UPnP at a speaker
        whose Wi-Fi is still coming up. The API passes wait=False: a button
        press should not hold an HTTP request open for the whole wake, and the
        panel sees the speaker return on its next poll anyway.
        """
        if not self.powered_off:
            return True, "already on"

        if self._off_method == "command":
            if not self.cfg.power_on_command:
                return self._power_failed(
                    "powered off by POWER_OFF_COMMAND but POWER_ON_COMMAND is "
                    "not set - the speaker has to be switched on by hand")
            ok, detail = run_command(self.cfg.power_on_command)
            if not ok:
                return self._power_failed(f"power-on command failed: {detail}")
        else:
            try:
                self.bar.wam_power(True)
            except SoundbarError as e:
                return self._power_failed(f"WAM power-on failed: {e}")

        self.powered_off = False
        self._suppress_wake = False
        self.power_result = "powered on"
        self.invalidate_soundbar_cache()
        if wait:
            log.info("powered on - waiting for the renderer to answer")
            self._await_renderer()
        else:
            log.info("powered on")
        return True, "powered on"

    def _record_power_off(self, method: str, reason: str,
                          manual: bool = False) -> tuple[bool, str]:
        self._off_method = method
        self.powered_off = True
        self._suppress_wake = manual
        self.power_result = f"powered off ({reason}) via {method}"
        self.invalidate_soundbar_cache()
        log.info("%s", self.power_result)
        return True, self.power_result

    def _power_failed(self, message: str) -> tuple[bool, str]:
        # Loud, and visible in /status: a power action that quietly did nothing
        # looks exactly like one that worked.
        self.power_result = message
        log.error("%s", message)
        return False, message

    def _await_renderer(self) -> bool:
        """Give a woken speaker time to rejoin the network.

        A wake is not instant - its Wi-Fi has to come back - and an engage
        attempt that lands too early counts against ReengagePolicy, which backs
        off after three and then stays off for the rest of the session.
        """
        deadline = time.monotonic() + WAKE_TIMEOUT
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.bar and self.bar.is_reachable(timeout=1.0):
                return True
            time.sleep(WAKE_POLL_SECONDS)
        log.warning("renderer did not answer within %.0fs of waking",
                    WAKE_TIMEOUT)
        return False

    def _renderer_in_use(self) -> str:
        """Why the renderer must not be powered off now, or "" if it may be.

        Two separate questions, because they catch different situations:

        *Is it playing?* AVTransport reports what the renderer's own media
        engine is doing, which catches another DLNA controller having pushed
        something to it after we let go.

        *What input is it on?* AVTransport says nothing at all about HDMI-ARC,
        optical or Bluetooth: a soundbar playing a film through ARC looks
        exactly like an idle one. Samsung's WAM GetFunc does answer that, and is
        one of the commands the HW-N850 answers instantly. Only `wifi` is ours -
        anything else means the speaker is in use for something the bridge
        cannot see, and powering it off would cut it dead mid-film.

        Unknowable is not the same as free, but it cannot mean "never power
        off" either, or the feature would not work on any renderer without a
        WAM API. So: fail open for a device that has never answered GetFunc,
        and fail closed for one that used to and has stopped.
        """
        try:
            if self.bar.transport_state(timeout=STATUS_TIMEOUT) == "PLAYING":
                return "it is playing something else"
        except SoundbarError:
            pass          # unreachable over UPnP; the input may still answer

        try:
            function = (self.bar.wam_function()[0] or "").lower()
        except SoundbarError:
            if self._input_readable:
                # It answered before, so silence now is a fault, not an
                # absence. Leave the speaker alone rather than guess.
                return "its input could not be checked"
            return ""     # no WAM API at all: nothing to consult, carry on

        self._input_readable = True
        if function and function != NETWORK_INPUT:
            return f"it is on its {function} input"
        return ""

    def _release_soundbar(self) -> None:
        try:
            # A Stop aimed at a speaker we already powered off just burns the
            # full UPnP timeout, which on shutdown delays the whole service.
            if self.bar and not self.powered_off:
                self.bar.stop()
                log.info("soundbar released")
        except SoundbarError as e:
            log.debug("release failed: %s", e)
        # Stop alone is not enough: the soundbar keeps the HTTP connection
        # open, so we would go on feeding it silence and no other controller
        # could claim it.
        if self.server:
            self.server.disconnect_clients()

    # -- test tone -------------------------------------------------------- #
    def play_test_tone(self) -> tuple[bool, dict]:
        """Play a tone down the real audio path and report what happened.

        Deliberately not a synthetic check. The tone goes through
        PcmBroadcaster and LiveWavServer exactly as AirPlay audio does, so
        hearing it means the whole chain works - whereas a renderer that
        answers control commands perfectly well while emitting nothing is the
        failure this exists to catch.

        The verdict names the likely fault rather than only reporting numbers.
        "No sound" is the symptom this shortens, and `renderers: 0` means
        nothing to someone who is not holding the source open.
        """
        if not self._tone_lock.acquire(blocking=False):
            return False, self._tone_verdict("a test tone is already playing")
        try:
            # Both feed the same broadcaster, so a tone played during a session
            # interleaves with the music and arrives as noise - which is
            # indistinguishable from the fault being tested for.
            if self.broadcaster.seconds_since_audio < self.cfg.idle_stop_seconds:
                return False, self._tone_verdict(
                    "an AirPlay session is playing - stop it and try again")
            if not self.bar:
                return False, self._tone_verdict("no renderer is configured")
            if not self.server:
                return False, self._tone_verdict("the audio stream is not running")

            if self.powered_off:
                ok, detail = self.power_on()
                if not ok:
                    return False, self._tone_verdict(
                        f"could not wake the speaker: {detail}")

            # Engage only when nothing is listening: the session loop may have
            # done it already, and a second SetAVTransportURI would restart the
            # renderer's fetch underneath the tone.
            if self.broadcaster.client_count == 0 and not self._engage_soundbar():
                return False, self._tone_verdict(
                    f"could not engage the renderer: {self.last_error}")

            attached = self._await_stream_client()
            before = self.server.bytes_streamed if self.server else 0
            self.test_tone_playing = True
            try:
                self._write_paced(self.broadcaster.tone(
                    TEST_TONE_SECONDS, TEST_TONE_HZ, TEST_TONE_AMPLITUDE))
            finally:
                self.test_tone_playing = False
            sent = (self.server.bytes_streamed if self.server else 0) - before

            # Read the speaker once, after the tone, and use that one reading
            # for both the explanation and the numbers - so they cannot
            # disagree with each other.
            self.invalidate_soundbar_cache()
            bar = self._soundbar_state()
            return True, self._tone_verdict(
                self._tone_detail(attached, bar), sent=sent, bar=bar)
        finally:
            self._tone_lock.release()

    def _await_stream_client(self) -> bool:
        """Wait for a renderer to actually fetch the stream.

        Engaging only tells the renderer where the audio is; it then opens its
        own HTTP connection, which takes a moment. A two-second tone played
        before that lands nowhere and would read as a fault.
        """
        deadline = time.monotonic() + TEST_TONE_CLIENT_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.broadcaster.client_count:
                return True
            time.sleep(0.1)
        return False

    def _write_paced(self, pcm: bytes) -> None:
        """Feed PCM to the broadcaster at the rate it would arrive live.

        Writing it in one go would overshoot _Client's backlog - two seconds,
        which a two-second tone reaches exactly - and the trim and drop paths
        would shed most of it. Pacing makes the tone travel as AirPlay audio
        does, which is the entire point of testing this way.
        """
        rate = self.broadcaster.bytes_per_second
        start = time.monotonic()
        for offset in range(0, len(pcm), CHUNK):
            self.broadcaster.write(pcm[offset:offset + CHUNK])
            ahead = start + (offset + CHUNK) / rate - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)

    def _tone_detail(self, attached: bool, bar: dict) -> str:
        """Name the likeliest reason a tone that was sent was not heard."""
        count = self.broadcaster.client_count
        if not attached or not count:
            return ("tone sent, but no renderer fetched the stream - check "
                    f"the speaker can reach {self.cfg.advertise_ip} on port "
                    f"{self.cfg.stream_port}")
        where = f"{count} renderer" + ("s" if count > 1 else "")
        if bar["muted"]:
            return f"tone sent to {where}, but the speaker is muted"
        if bar["volume"] == 0:
            return f"tone sent to {where}, but its volume is 0"
        return (f"tone sent to {where} - if you heard nothing, check the "
                "speaker is on its network input")

    def _tone_verdict(self, detail: str, sent: int = 0,
                      bar: dict | None = None) -> dict:
        bar = self._soundbar_state() if bar is None else bar
        self.test_tone_result = detail
        log.info("test tone: %s", detail)
        return {
            "detail": detail,
            "renderers": self.broadcaster.client_count,
            "bytes_sent": sent,
            "seconds": TEST_TONE_SECONDS,
            "volume": bar["volume"],
            "muted": bar["muted"],
            "state": bar["state"],
        }

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
                if want:
                    self.auto_off.session_started()
                    # A new session is a fresh instruction: it clears an
                    # earlier manual off, which only outranked the session it
                    # was issued during.
                    self._suppress_wake = False
                else:
                    self.metadata.reset_track()
                    # Re-probe next session: shairport-sync can renegotiate
                    # rate/format between senders.
                    self._fmt_checked = False
                    self._fmt_probe.clear()
                    policy.reset()
                    self.auto_off.session_ended()

            # A session on a speaker we switched off: wake it before anything
            # else tries to talk to it, and before the engage attempt below.
            if want and self.powered_off and not self._suppress_wake:
                self.power_on()

            # A test tone moves the same liveness signal as AirPlay audio, so
            # without this the loop would engage a second time in the middle of
            # one - and a renderer told to fetch the URI again restarts the
            # stream, cutting the tone off. play_test_tone has already engaged.
            if want and not engaged and policy.may_engage() \
                    and not self.test_tone_playing:
                engaged = self._engage_soundbar()
                policy.record_engage()
                last_check = time.monotonic()
            elif not want and engaged:
                self._release_soundbar()
                engaged = False
            elif not want and self.auto_off.should_fire(quiet):
                # Marked fired first: one attempt per idle period, whether or
                # not it works, so a failing method cannot retry every second.
                self.auto_off.record_fired()
                in_use = self._renderer_in_use()
                if in_use:
                    # Recorded, not just logged: "why didn't it turn off?" is
                    # otherwise only answerable from the journal.
                    self.power_result = f"auto power-off skipped - {in_use}"
                    log.info("%s", self.power_result)
                else:
                    self.power_off(
                        f"idle for {self.cfg.auto_off_minutes:g} min")
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
            if self.powered_off:
                # Don't query a device we switched off: every poll would stall
                # for the full timeout and report an error we already know the
                # cause of.
                result["state"] = "off"
                self._bar_cache = result
                self._bar_cache_at = time.monotonic()
                return dict(result)
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
            "power": {
                "auto_off_minutes": self.cfg.auto_off_minutes,
                "off": self.powered_off,
                # None unless a countdown is genuinely running, so the panel
                # does not have to reimplement the arming rules.
                "seconds_until_off": self.auto_off.seconds_remaining(
                    self.broadcaster.seconds_since_audio),
                "last_result": self.power_result,
            },
            # Audio from a test tone moves seconds_since_audio exactly as
            # AirPlay audio does, so session_active goes true for one. Say
            # which it is rather than letting the panel imply a session.
            "test_tone": {
                "playing": self.test_tone_playing,
                "last_result": self.test_tone_result,
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

    # -- settings --------------------------------------------------------- #
    def settings_snapshot(self) -> dict:
        items = describe_editable(self.cfg,
                                  read_env_file(self.cfg.config_dir))
        return {"settings": items,
                "restart_pending": any(i["pending"] for i in items),
                # Reported up front so the panel can say the form is read-only
                # rather than letting someone fill it in and hit a wall.
                "writable": config_writable(self.cfg.config_dir),
                "config_file": os.path.join(self.cfg.config_dir, "bridge.env")}

    def update_settings(self, changes: dict) -> tuple[bool, dict]:
        """Validate, persist, and apply what can be applied now.

        Saving and applying are deliberately separate. Everything is written to
        bridge.env, but only the settings the session loop re-reads each tick
        take effect immediately; the rest keep running on their old values
        until a restart, and `pending` in the snapshot says so rather than
        letting the panel imply otherwise.
        """
        applied, errors = apply_settings(self.cfg, changes)
        if errors:
            return False, {"ok": False, "errors": errors}

        # The panel posts the whole form, so most of what arrives is already
        # in force. Comparing against the running value is what keeps "needs a
        # restart" meaning something: it is exactly the set that is saved but
        # not yet in effect, which is also true of an earlier save nobody has
        # restarted for yet.
        applied = {env: value for env, value in applied.items()
                   if value != getattr(self.cfg, BY_ENV[env].name)}
        if not applied:
            return True, {"ok": True, "applied": {}, "restart_required": []}

        ok, detail = write_env_file(
            self.cfg.config_dir,
            {env: env_text(value) for env, value in applied.items()})
        if not ok:
            log.error("could not save settings: %s", detail)
            return False, {"ok": False, "errors": {"": detail}}

        restart_required = []
        for env_name, value in applied.items():
            setting = BY_ENV[env_name]
            if setting.live:
                setattr(self.cfg, setting.name, value)
            else:
                restart_required.append(env_name)
        # Live values still go through the cross-field rules, and the auto-off
        # policy holds its own copy of the threshold.
        self.cfg.normalise()
        self.auto_off.seconds = max(0.0, self.cfg.auto_off_seconds)
        log.info("settings saved: %s", ", ".join(sorted(applied)))
        return True, {"ok": True,
                      "applied": {k: env_text(v) for k, v in applied.items()},
                      "restart_required": restart_required}

    def request_restart(self) -> tuple[bool, str]:
        threading.Thread(target=self._restart_soon, daemon=True).start()
        return True, "restarting - the service manager will bring it back"

    def _restart_soon(self) -> None:
        time.sleep(RESTART_DELAY)
        log.info("restarting to apply settings")
        try:
            self.stop()
        except Exception as e:      # we are exiting regardless
            log.debug("shutdown before restart: %s", e)
        restart_process()

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
