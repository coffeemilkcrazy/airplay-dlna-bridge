"""Transport control of the AirPlay *sender* via DACP.

Volume and mute act on the soundbar, but play/pause/skip cannot: the soundbar
is only rendering a stream we push at it and knows nothing about tracks. Those
commands have to go back to whatever is sending the audio - Music.app on the
Mac, or an iPhone.

Apple's remote-control protocol (DACP) makes that possible, and shairport-sync
hands us the two pieces needed:

    daid  DACP-ID          identifies the sender's control service
    acre  Active-Remote    per-session token, sent as a header

The service itself is advertised over mDNS as `iTunes_Ctrl_<DACP-ID>` under
`_dacp._tcp`. We resolve it with avahi-browse rather than adding a Python
mDNS dependency - avahi-daemon is already required for AirPlay to work at all.

    GET http://<host>:<port>/ctrl-int/1/playpause
    Active-Remote: <token>
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("bridge.dacp")

# Commands we expose. Anything not listed is rejected rather than passed
# through, so the endpoint cannot be used to poke arbitrary DACP paths.
COMMANDS = {
    "playpause": "playpause",
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "next": "nextitem",
    "previous": "previtem",
}

RESOLVE_TTL = 60.0          # re-resolve at most this often
RESOLVE_TIMEOUT = 6.0


class DacpRemote:
    """Resolves and talks to the sender's DACP endpoint."""

    def __init__(self):
        self._lock = threading.Lock()
        self._endpoint: tuple[str, int] | None = None
        self._resolved_for = ""      # dacp_id the endpoint belongs to
        self._resolved_at = 0.0

    # ------------------------------------------------------------------ #
    def available(self, dacp_id: str, token: str) -> bool:
        return bool(dacp_id and token)

    def send(self, command: str, dacp_id: str, token: str) -> tuple[bool, str]:
        """Returns (ok, detail)."""
        path = COMMANDS.get(command)
        if not path:
            return False, f"unknown command {command!r}"
        if not self.available(dacp_id, token):
            return False, ("no AirPlay sender known yet - start playing so "
                           "shairport-sync reports its DACP credentials")

        endpoint = self._endpoint_for(dacp_id)
        if not endpoint:
            return False, f"could not resolve DACP service for {dacp_id}"

        host, port = endpoint
        url = f"http://{host}:{port}/ctrl-int/1/{path}"
        req = urllib.request.Request(url, headers={"Active-Remote": token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
                return True, f"{command} -> {host}:{port}"
        except urllib.error.HTTPError as e:
            e.close()
            # A stale endpoint is the usual cause; drop it so the next
            # attempt re-resolves rather than failing the same way.
            self._forget()
            return False, f"sender returned HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            self._forget()
            return False, f"sender unreachable: {e}"

    # ------------------------------------------------------------------ #
    def _forget(self) -> None:
        with self._lock:
            self._endpoint = None
            self._resolved_at = 0.0

    def _endpoint_for(self, dacp_id: str) -> tuple[str, int] | None:
        now = time.monotonic()
        with self._lock:
            fresh = (self._endpoint
                     and self._resolved_for == dacp_id
                     and now - self._resolved_at < RESOLVE_TTL)
            if fresh:
                return self._endpoint

        endpoint = resolve_dacp(dacp_id)
        with self._lock:
            self._endpoint = endpoint
            self._resolved_for = dacp_id
            self._resolved_at = time.monotonic()
        return endpoint


def resolve_dacp(dacp_id: str) -> tuple[str, int] | None:
    """Find iTunes_Ctrl_<dacp_id>._dacp._tcp via avahi-browse.

    Output lines look like:
      =;eth0;IPv4;iTunes_Ctrl_ABC123;_dacp._tcp;local;host.local;192.168.1.5;3689;
    """
    target = f"iTunes_Ctrl_{dacp_id}"
    try:
        proc = subprocess.run(
            ["avahi-browse", "-rptk", "_dacp._tcp"],
            capture_output=True, text=True, timeout=RESOLVE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("avahi-browse unavailable (%s); transport control needs it", e)
        return None

    for line in proc.stdout.splitlines():
        if not line.startswith("=") or target not in line:
            continue
        parts = line.split(";")
        if len(parts) < 9:
            continue
        host, port = parts[7], parts[8]
        # Prefer a literal address; a .local name needs another lookup.
        if not re.match(r"^[\d.]+$|^[0-9a-fA-F:]+$", host):
            host = parts[6] or host
        try:
            return host, int(port)
        except ValueError:
            continue

    log.info("no DACP service advertised for %s", target)
    return None
