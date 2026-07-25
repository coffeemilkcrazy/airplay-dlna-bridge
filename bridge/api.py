"""HTTP status API and web control panel.

Split out of bridge.py, which had grown to cover process supervision, session
tracking, format probing, metadata and HTTP in one file.

Routes:
    GET  /               web control panel (always served, see below)
    GET  /status         JSON state
    GET  /artwork        current cover art, if any
    POST /volume/<n>     set volume (clamped to max_volume)
    POST /mute/on|off    mute
    POST /transport/<c>  playpause | play | pause | stop | next | previous
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dacp import COMMANDS
from webui import PAGE

log = logging.getLogger("bridge.api")


def make_server(bridge) -> ThreadingHTTPServer:
    """Build the status/control server bound to `bridge`."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # silence per-request logging
            pass

        # -- helpers ---------------------------------------------------- #
        def _send(self, body: bytes, ctype: str, code: int = 200,
                  extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200):
            self._send(json.dumps(payload).encode(), "application/json", code,
                       {"Access-Control-Allow-Origin": "*"})

        def _authorised(self) -> bool:
            token = bridge.cfg.status_token
            if not token:
                return True
            supplied = self.headers.get("X-Bridge-Token", "")
            if not supplied:
                query = urllib.parse.urlparse(self.path).query
                supplied = urllib.parse.parse_qs(query).get("token", [""])[0]
            # Constant-time compare so the token cannot be guessed by timing.
            return hmac.compare_digest(supplied, token)

        # -- GET -------------------------------------------------------- #
        def do_GET(self):
            route = urllib.parse.urlparse(self.path).path

            # The panel itself is served unauthenticated: it holds no data,
            # and it is what tells the user a token is required. Everything
            # it calls is still guarded.
            if route in ("/", "/index.html"):
                return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

            if not self._authorised():
                return self._json({"error": "unauthorised"}, 401)

            if route == "/status":
                return self._json(bridge.snapshot())

            if route == "/artwork":
                data, mime = bridge.metadata.artwork_bytes()
                if not data:
                    return self._json({"error": "no artwork"}, 404)
                return self._send(data, mime)

            self._json({"error": "not found"}, 404)

        # -- POST ------------------------------------------------------- #
        def do_POST(self):
            if not self._authorised():
                return self._json({"error": "unauthorised"}, 401)
            route = urllib.parse.urlparse(self.path).path

            m = re.fullmatch(r"/volume/(\d+)", route)
            if m and bridge.bar:
                # Enforce the cap here rather than trusting callers: this is
                # the one path every client necessarily goes through.
                wanted = int(m.group(1))
                level = max(0, min(bridge.cfg.max_volume, wanted))
                try:
                    bridge.bar.set_volume(level)
                    bridge.invalidate_soundbar_cache()
                    return self._json({"ok": True, "volume": level,
                                       "requested": wanted,
                                       "max_volume": bridge.cfg.max_volume,
                                       "capped": level != wanted})
                except Exception as e:
                    return self._json({"error": str(e)}, 502)

            m = re.fullmatch(r"/mute/(on|off)", route)
            if m and bridge.bar:
                want = m.group(1) == "on"
                try:
                    bridge.bar.set_mute(want)
                    bridge.invalidate_soundbar_cache()
                    return self._json({"ok": True, "muted": want})
                except Exception as e:
                    return self._json({"error": str(e)}, 502)

            m = re.fullmatch(r"/transport/(\w+)", route)
            if m:
                command = m.group(1)
                if command not in COMMANDS:
                    return self._json(
                        {"error": f"unknown command {command!r}",
                         "supported": sorted(COMMANDS)}, 400)
                dacp_id, token = bridge.metadata.remote()
                ok, detail = bridge.dacp.send(command, dacp_id, token)
                log.info("transport %s: %s", command, detail)
                return self._json({"ok": ok, "detail": detail},
                                  200 if ok else 503)

            self._json({"error": "not found"}, 404)

    return ThreadingHTTPServer(
        (bridge.cfg.status_bind, bridge.cfg.status_port), Handler)
