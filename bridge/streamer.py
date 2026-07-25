"""Live PCM -> HTTP WAV streaming for DLNA renderers.

The Samsung HW-N850 was measured to accept a continuous `audio/x-wav` stream
advertised with a large Content-Length, sustaining full-rate 44.1 kHz/16-bit
stereo.  It rejects `audio/L16` outright, so WAV is the transport here.

Two pieces:

  PcmBroadcaster   fan-out buffer - audio written once, read by each connected
                   renderer independently.  Slow readers drop audio rather than
                   stalling the writer.

  LiveWavServer    minimal HTTP/1.1 server exposing that audio as an endless
                   WAV file.

When no one is producing audio the broadcaster emits digital silence, which
keeps the renderer's connection open.  Without it the soundbar tears down the
session between tracks and re-buffers audibly on the next one.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("bridge.streamer")

# A renderer will not accept a truly infinite stream, so we advertise a size it
# will never reach: 2 GiB - 1 is ~3.4 hours of CD-quality audio.
_ADVERTISED_SIZE = 0x7FFFFFFF

WAV_FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=8D100000000000000000000000000000"
WAV_MIME = "audio/x-wav"

# How long a blocked send may stall before we treat the renderer as dead.
STALL_TIMEOUT_SECONDS = 15
# How much backed-up audio justifies dropping the connection to resync.
MAX_DROP_SECONDS = 5.0
# Drift correction: shed 1/TRIM_DIVISOR of the overshoot per write, capped at
# MAX_TRIM_SECONDS per correction so no single splice becomes audible.
TRIM_DIVISOR = 32
MAX_TRIM_SECONDS = 0.01

# NOTE: this is drift *mitigation*, not resampling. A true fix would resample
# the stream by a continuously adjusted ratio (~1 +/- 1e-4) so no samples are
# discarded at all. That is a substantially larger piece of work; this keeps
# corrections small and frequent instead, which is inaudible in practice.


def wav_header(rate: int, channels: int, bits: int, data_size: int) -> bytes:
    """A RIFF/WAVE header declaring `data_size` bytes of PCM."""
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", data_size)
    )


class PcmBroadcaster:
    """Fan-out ring buffer for live PCM."""

    def __init__(self, rate: int = 44100, channels: int = 2, bits: int = 16,
                 max_backlog_seconds: float = 2.0,
                 soft_backlog_seconds: float = 0.5):
        self.rate, self.channels, self.bits = rate, channels, bits
        self.bytes_per_frame = channels * bits // 8
        self.bytes_per_second = rate * channels * bits // 8
        self._max_backlog = int(self.bytes_per_second * max_backlog_seconds)
        # The soft threshold must sit below the hard one, or the trim branch is
        # unreachable and drift only ever surfaces as an audible bulk drop.
        self._soft_backlog = min(
            int(self.bytes_per_second * soft_backlog_seconds),
            self._max_backlog // 2)
        self._max_trim = max(
            self.bytes_per_frame,
            int(self.bytes_per_second * MAX_TRIM_SECONDS))
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._last_write = 0.0

    # -- producer side --------------------------------------------------- #
    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._last_write = time.monotonic()
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.push(chunk, self._max_backlog, self._soft_backlog,
                   self.bytes_per_frame, self._max_trim)

    @property
    def seconds_since_audio(self) -> float:
        if not self._last_write:
            return float("inf")
        return time.monotonic() - self._last_write

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # -- consumer side --------------------------------------------------- #
    def add_client(self) -> "_Client":
        c = _Client()
        with self._lock:
            self._clients.append(c)
        log.info("stream client attached (now %d)", len(self._clients))
        return c

    def remove_client(self, c: "_Client") -> None:
        with self._lock:
            if c in self._clients:
                self._clients.remove(c)
        log.info("stream client detached (now %d)", len(self._clients))

    def silence(self, seconds: float) -> bytes:
        return b"\x00" * (int(self.bytes_per_second * seconds) & ~3)


class _Client:
    """Per-connection buffer."""

    def __init__(self):
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self.dropped = 0        # bulk discards - audible
        self.trimmed = 0        # micro-corrections - inaudible

    def push(self, chunk: bytes, max_backlog: int, soft_backlog: int,
             frame: int, max_trim: int) -> None:
        with self._cond:
            if self._closed:
                return
            self._buf += chunk

            if len(self._buf) > max_backlog:
                # Hard limit: a reader has stalled badly. Staying near live
                # matters more than completeness.
                excess = len(self._buf) - max_backlog
                # Round to whole frames. Dropping a partial frame shifts the
                # channel interleave, which swaps left/right for the rest of
                # the stream - far worse than the gap itself.
                excess -= excess % frame
                if excess:
                    del self._buf[:excess]
                    self.dropped += excess

            elif len(self._buf) > soft_backlog:
                # The producer's clock (the sender's) and the renderer's DAC
                # clock differ by a few ppm, so the backlog creeps up over a
                # long session. Bleed it off continuously instead of letting it
                # accumulate into one audible multi-second drop.
                #
                # The amount is proportional to how far past the threshold we
                # are, so faster drift is corrected faster and the hard limit
                # is never reached. A fixed one-frame trim can be outrun.
                # Each correction is capped so a single splice stays short.
                over = len(self._buf) - soft_backlog
                trim = max(frame, over // TRIM_DIVISOR)
                trim = min(trim, max_trim)
                trim -= trim % frame          # keep the interleave intact
                if trim:
                    del self._buf[:trim]
                    self.trimmed += trim

            self._cond.notify()

    def read(self, timeout: float = 0.5) -> bytes:
        with self._cond:
            if not self._buf and not self._closed:
                self._cond.wait(timeout)
            data = bytes(self._buf)
            self._buf.clear()
            return data

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed


class LiveWavServer:
    """Serves the broadcaster's audio as an endless WAV over HTTP."""

    def __init__(self, broadcaster: PcmBroadcaster, host: str = "0.0.0.0",
                 port: int = 8770, path: str = "/airplay.wav"):
        self.broadcaster = broadcaster
        self.host, self.port, self.path = host, port, path
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self.connections = 0
        self.bytes_streamed = 0
        self.silence_streamed = 0
        self._active: set[socket.socket] = set()
        self._active_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def url_for(self, advertise_ip: str) -> str:
        return f"http://{advertise_ip}:{self.port}{self.path}"

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        threading.Thread(target=self._accept_loop, daemon=True).start()
        log.info("live WAV server on %s:%d%s", self.host, self.port, self.path)

    def stop(self) -> None:
        self._stop.set()
        self.disconnect_clients()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def disconnect_clients(self) -> int:
        """Hang up on every attached renderer.

        The silence keepalive is what stops a renderer re-buffering between
        tracks, but once the session is over it becomes a liability: the
        soundbar holds the connection open indefinitely, we keep pushing
        ~176 kB/s of silence at it, and no other controller can claim it.
        Closing here lets it go genuinely idle.
        """
        with self._active_lock:
            socks = list(self._active)
            # Clear immediately. The pump thread also discards on exit, but it
            # may not have run yet, and a second call in the meantime would
            # otherwise report the same connection as closed twice.
            self._active.clear()
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if socks:
            log.info("closed %d idle stream connection(s)", len(socks))
        return len(socks)

    # ------------------------------------------------------------------ #
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr),
                             daemon=True).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        conn.settimeout(30)
        peer = addr[0]
        fh = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                request_line = fh.readline()
                if not request_line:
                    break
                req = request_line.decode(errors="ignore").strip()
                headers = {}
                while True:
                    line = fh.readline()
                    if not line or line in (b"\r\n", b"\n"):
                        break
                    text = line.decode(errors="ignore").strip()
                    if ":" in text:
                        k, v = text.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                parts = req.split(" ")
                method = parts[0].upper() if parts else ""
                target = parts[1] if len(parts) > 1 else "/"
                log.debug("%s %s from %s", method, target, peer)

                if not target.startswith(self.path):
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    continue

                head = (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Type: {WAV_MIME}\r\n"
                    f"Content-Length: {_ADVERTISED_SIZE}\r\n"
                    "Accept-Ranges: none\r\n"
                    "transferMode.dlna.org: Streaming\r\n"
                    f"contentFeatures.dlna.org: {WAV_FEATURES}\r\n"
                    "Connection: keep-alive\r\n"
                    "\r\n"
                )
                conn.sendall(head.encode())

                # The renderer probes with HEAD before committing to a GET.
                if method == "HEAD":
                    continue
                if method != "GET":
                    break

                self.connections += 1
                self._pump(conn)
                return
        except (OSError, socket.timeout):
            pass
        finally:
            # The makefile() wrapper holds its own reference to the socket;
            # closing only `conn` leaves it dangling until GC.
            try:
                fh.close()
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def _pump(self, conn: socket.socket) -> None:
        """Stream audio until the renderer hangs up."""
        b = self.broadcaster
        client = b.add_client()
        sent = 0
        with self._active_lock:
            self._active.add(conn)
        # A renderer that stops reading leaves sendall() blocking on a full TCP
        # window while the producer keeps queueing audio. Without a send
        # timeout that silently accumulates (a 369s backlog was observed once),
        # so bound it and treat a stall as a dead connection.
        conn.settimeout(STALL_TIMEOUT_SECONDS)
        try:
            conn.sendall(wav_header(b.rate, b.channels, b.bits,
                                    _ADVERTISED_SIZE - 44))
            # Prime the renderer so it can fill its jitter buffer immediately;
            # starting empty makes it give up before audio arrives.
            conn.sendall(b.silence(0.25))

            while not self._stop.is_set() and not client.closed:
                # Bail out rather than quietly discarding a growing backlog:
                # reconnecting resyncs to live, whereas dropping audio in place
                # produces repeated glitches with no recovery.
                if client.dropped > b.bytes_per_second * MAX_DROP_SECONDS:
                    log.warning(
                        "renderer stalled (%.1f s of audio backed up) - "
                        "closing so it reconnects and resyncs to live",
                        client.dropped / b.bytes_per_second)
                    break
                chunk = client.read(timeout=0.25)
                if chunk:
                    conn.sendall(chunk)
                    sent += len(chunk)
                    self.bytes_streamed += len(chunk)
                else:
                    # No audio available: emit silence so the renderer keeps
                    # the socket (and its decoder) alive between tracks.
                    silence = b.silence(0.25)
                    conn.sendall(silence)
                    sent += len(silence)
                    self.silence_streamed += len(silence)
        except (OSError, socket.timeout) as e:
            log.info("stream ended after %.1f MB (%s)", sent / 1e6, e)
        finally:
            with self._active_lock:
                self._active.discard(conn)
            b.remove_client(client)
            if client.dropped:
                log.warning("dropped %.2f s of audio to stay live",
                            client.dropped / b.bytes_per_second)
            if client.trimmed:
                # Expected on any long session; only notable if it is large.
                log.info("clock drift trimmed %.3f s over the session "
                         "(inaudible micro-corrections)",
                         client.trimmed / b.bytes_per_second)
            try:
                conn.close()
            except OSError:
                pass
