"""Reader for shairport-sync's metadata pipe.

shairport-sync emits a stream of XML items, each carrying a four-character
code and a base64 payload:

    <item><type>636f7265</type><code>6d696e6d</code><length>5</length>
    <data encoding="base64">SGVsbG8=</data></item>

We extract three things:

  * now-playing text  - title / artist / album
  * cover art         - the PICT item, served by the web panel
  * DACP credentials  - the sender's DACP-ID and Active-Remote token, which
                        are what make play/pause/skip possible (see dacp.py)
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("bridge.metadata")

# 'core' items describe the track; 'ssnc' items are shairport's own signals.
TEXT_CODES = {"minm": "title", "asar": "artist", "asal": "album"}

# Grab each item's body first, then pick the code and data out of it. Trying
# to do all three in one pattern needs an optional <data> group behind a lazy
# prefix, and the prefix then happily swallows the data so it never matches -
# a bug this parser has had once already.
ITEM_RE = re.compile(rb"<item>(.*?)</item>", re.S)
CODE_RE = re.compile(rb"<code>([0-9a-fA-F]{8})</code>")
DATA_RE = re.compile(rb"<data[^>]*>(.*?)</data>", re.S)

MAX_BUFFER = 8 << 20        # cover art can be large; cap so a stuck reader
                            # cannot grow the buffer without bound


@dataclass
class NowPlaying:
    title: str = ""
    artist: str = ""
    album: str = ""

    def clear(self) -> None:
        self.title = self.artist = self.album = ""

    def __bool__(self) -> bool:
        return bool(self.title or self.artist)


@dataclass
class Artwork:
    data: bytes = b""
    mime: str = ""
    version: int = 0        # bumped on change, so the page can cache-bust

    def clear(self) -> None:
        if self.data:
            self.data = b""
            self.mime = ""
            self.version += 1


def sniff_image(blob: bytes) -> str:
    """Content type from magic bytes - shairport does not tell us."""
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"


class MetadataReader:
    """Follows the metadata FIFO and keeps the latest state."""

    def __init__(self, pipe_path: str):
        self.pipe_path = pipe_path
        self.now_playing = NowPlaying()
        self.artwork = Artwork()
        # Set from the sender; needed to control it. See dacp.py.
        self.dacp_id = ""
        self.active_remote = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def reset_track(self) -> None:
        with self._lock:
            self.now_playing.clear()
            self.artwork.clear()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        while not self._stop.is_set():
            if not os.path.exists(self.pipe_path):
                time.sleep(5)
                continue
            try:
                with open(self.pipe_path, "rb") as fh:
                    self._consume(fh)
            except OSError as e:
                log.debug("metadata pipe: %s", e)
                time.sleep(5)

    def _consume(self, fh) -> None:
        buf = b""
        while not self._stop.is_set():
            # read1(), not read(): on a FIFO, read(n) blocks until it has all
            # n bytes. shairport-sync writes a few hundred bytes per track and
            # holds the pipe open, so read() would block forever and no
            # metadata would ever be parsed.
            chunk = fh.read1(65536)
            if not chunk:
                return
            buf += chunk
            buf = self._drain(buf)
            if len(buf) > MAX_BUFFER:
                buf = buf[-65536:]

    def _drain(self, buf: bytes) -> bytes:
        while True:
            m = ITEM_RE.search(buf)
            if not m:
                return buf
            buf = buf[m.end():]
            body = m.group(1)
            code_m = CODE_RE.search(body)
            if not code_m:
                continue
            try:
                code = bytes.fromhex(code_m.group(1).decode()).decode(errors="ignore")
            except ValueError:
                continue
            data_m = DATA_RE.search(body)
            self._handle(code, data_m.group(1) if data_m else b"")

    def _handle(self, code: str, payload: bytes) -> None:
        # 'mdst' opens a bundle for a new track. Clear first, so a track that
        # omits (say) album does not inherit the previous one's value.
        if code == "mdst":
            self.reset_track()
            return

        if code == "daid":
            self.dacp_id = self._text(payload)
            return
        if code == "acre":
            self.active_remote = self._text(payload)
            return

        if code == "PICT":
            self._set_artwork(payload)
            return

        field_name = TEXT_CODES.get(code)
        if field_name:
            with self._lock:
                setattr(self.now_playing, field_name, self._text(payload))

    @staticmethod
    def _text(payload: bytes) -> str:
        try:
            return base64.b64decode(payload).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _set_artwork(self, payload: bytes) -> None:
        try:
            blob = base64.b64decode(payload)
        except Exception:
            return
        with self._lock:
            if not blob:
                self.artwork.clear()
                return
            if blob == self.artwork.data:
                return
            self.artwork.data = blob
            self.artwork.mime = sniff_image(blob)
            self.artwork.version += 1
            log.debug("cover art %.0f kB (%s)", len(blob) / 1024,
                      self.artwork.mime)

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "title": self.now_playing.title,
                "artist": self.now_playing.artist,
                "album": self.now_playing.album,
                "artwork": bool(self.artwork.data),
                "artwork_version": self.artwork.version,
            }

    def artwork_bytes(self) -> tuple[bytes, str]:
        with self._lock:
            return self.artwork.data, self.artwork.mime

    def remote(self) -> tuple[str, str]:
        return self.dacp_id, self.active_remote
