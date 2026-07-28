"""HTTP status API and web control panel.

Split out of bridge.py, which had grown to cover process supervision, session
tracking, format probing, metadata and HTTP in one file.

Routes:
    GET  /               web control panel (always served, see below)
    GET  /status         JSON state
    GET  /settings       editable settings, their saved and running values
    GET  /artwork        current cover art, if any
    POST /volume/<n>     set volume (clamped to max_volume)
    POST /mute/on|off    mute
    POST /transport/<c>  playpause | play | pause | stop | next | previous
    POST /power/on|off   power the renderer on or off
    POST /test-tone      play a test tone down the real audio path
    POST /settings       save settings (JSON body, keyed by env name)
    POST /restart        restart the service to apply saved settings
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

# The only route with a body is /settings, which carries a handful of short
# strings. Anything larger is not a settings form.
MAX_BODY_BYTES = 64 * 1024


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

        def _body(self):
            """The request's JSON object, or None if it is not one.

            Content-Length is bounded before reading: this server is on the
            LAN, and an unbounded read is a way to make it hold memory.
            """
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return None
            if not 0 < length <= MAX_BODY_BYTES:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

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

            if route == "/settings":
                return self._json(bridge.settings_snapshot())

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

            if route == "/settings":
                payload = self._body()
                if not isinstance(payload, dict):
                    return self._json(
                        {"ok": False,
                         "errors": {"": "expected a JSON object"}}, 400)
                ok, result = bridge.update_settings(payload)
                return self._json(result, 200 if ok else 400)

            if route == "/restart":
                ok, detail = bridge.request_restart()
                return self._json({"ok": ok, "detail": detail},
                                  200 if ok else 503)

            m = re.fullmatch(r"/power/(on|off)", route)
            if m:
                if m.group(1) == "on":
                    # wait=False: a button press should not hold the request
                    # open for the whole wake. The next poll shows the result.
                    ok, detail = bridge.power_on(wait=False)
                else:
                    ok, detail = bridge.power_off("requested", manual=True)
                log.info("power %s: %s", m.group(1), detail)
                return self._json({"ok": ok, "detail": detail},
                                  200 if ok else 503)

            if route == "/test-tone":
                # Holds the request for the length of the tone: the caller
                # wants the verdict, and it is only worth anything once the
                # audio has actually been through the chain.
                ok, verdict = bridge.play_test_tone()
                return self._json({"ok": ok, **verdict}, 200 if ok else 503)

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
