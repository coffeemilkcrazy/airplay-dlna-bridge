"""Tests for the UPnP / Samsung WAM control library.

Uses a fake MediaRenderer so the real soundbar is never needed.
"""

import html
import re
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from soundbar import (AVTRANSPORT, RENDERING, Soundbar,  # noqa: E402
                      SoundbarError)


def soap_response(action: str, inner: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action}Response '
        f'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f"{inner}</u:{action}Response></s:Body></s:Envelope>"
    ).encode()


class FakeRenderer:
    """Minimal UPnP renderer that records what it was sent."""

    def __init__(self):
        self.requests = []            # (soapaction, body)
        self.fail_next = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                action_hdr = self.headers.get("SOAPAction", "")
                outer.requests.append((action_hdr, body))

                if outer.fail_next:
                    outer.fail_next = False
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"<errorDescription>boom</errorDescription>")
                    return

                m = re.search(r"#(\w+)", action_hdr)
                action = m.group(1) if m else "Unknown"
                inner = {
                    "GetVolume": "<CurrentVolume>37</CurrentVolume>",
                    "GetMute": "<CurrentMute>1</CurrentMute>",
                    "GetTransportInfo":
                        "<CurrentTransportState>PLAYING</CurrentTransportState>",
                    "GetPositionInfo": ("<Track>1</Track>"
                                        "<TrackDuration>0:03:21</TrackDuration>"
                                        "<RelTime>0:00:42</RelTime>"
                                        "<TrackURI>http://x/y.wav</TrackURI>"),
                    "X_GetStoppedReason": "<StoppedReason></StoppedReason>",
                }.get(action, "")
                payload = soap_response(action, inner)
                self.send_response(200)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()      # release the listening socket too

    def last(self):
        return self.requests[-1]


class TestDidl(unittest.TestCase):
    def test_structure(self):
        d = Soundbar.didl("http://a/b.wav", "Title", "audio/x-wav", "FLAGS")
        self.assertIn("<dc:title>Title</dc:title>", d)
        self.assertIn("http-get:*:audio/x-wav:FLAGS", d)
        self.assertIn("http://a/b.wav", d)
        self.assertIn("DIDL-Lite", d)

    def test_escapes_title_and_url(self):
        d = Soundbar.didl("http://a/b.wav?x=1&y=2", "Rock & <Roll>",
                          "audio/x-wav", "F")
        self.assertIn("Rock &amp; &lt;Roll&gt;", d)
        self.assertIn("x=1&amp;y=2", d)
        self.assertNotIn("<Roll>", d)

    def test_live_stream_uses_broadcast_class(self):
        live = Soundbar.didl("u", "t", "m", "f")
        self.assertIn("audioBroadcast", live)

    def test_finite_track_uses_musictrack_class(self):
        track = Soundbar.didl("u", "t", "m", "f", duration="0:03:00", size=100)
        self.assertIn("musicTrack", track)
        self.assertIn('duration="0:03:00"', track)
        self.assertIn('size="100"', track)


class TestProductClassification(unittest.TestCase):
    """A Samsung TV also advertises a MediaRenderer and will accept Play then
    never fetch, so discovery must be able to tell them apart."""

    SOUNDBAR = ("<root><device>"
                "<friendlyName>[AV] Samsung Soundbar N850</friendlyName>"
                "<modelName>HW-N850</modelName>"
                "<modelDescription>Samsung SOUNDBAR DMR</modelDescription>"
                "<sec:ProductCap>Tizen,Y2018,vdProductType=SOUNDBAR,OCF=1"
                "</sec:ProductCap></device></root>")

    TV = ("<root><device>"
          "<friendlyName>TV Living</friendlyName>"
          "<modelName>UA55BU8100KXXT</modelName>"
          "<modelDescription>Samsung DTV RCR</modelDescription>"
          "<sec:ProductCap>Resolution:1920X1080,Tizen,Y2021</sec:ProductCap>"
          "</device></root>")

    def test_soundbar_detected_from_productcap(self):
        self.assertEqual(
            Soundbar.product_type_from_description(self.SOUNDBAR), "SOUNDBAR")

    def test_tv_not_classified_as_soundbar(self):
        self.assertNotEqual(
            Soundbar.product_type_from_description(self.TV), "SOUNDBAR")

    def test_falls_back_to_model_description(self):
        desc = ("<root><device><friendlyName>x</friendlyName>"
                "<modelDescription>Samsung SOUNDBAR DMR</modelDescription>"
                "</device></root>")
        self.assertEqual(Soundbar.product_type_from_description(desc), "SOUNDBAR")

    def test_unknown_device(self):
        self.assertEqual(Soundbar.product_type_from_description("<root/>"), "")


class TestUpnpCalls(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRenderer()
        self.bar = Soundbar(ip="127.0.0.1", dmr_port=self.fake.port)

    def tearDown(self):
        self.fake.stop()

    def test_get_volume(self):
        self.assertEqual(self.bar.get_volume(), 37)
        action, body = self.fake.last()
        self.assertIn(f"{RENDERING}#GetVolume", action)
        self.assertIn("<InstanceID>0</InstanceID>", body)
        self.assertIn("<Channel>Master</Channel>", body)

    def test_set_volume_sends_desired_value(self):
        self.bar.set_volume(42)
        _, body = self.fake.last()
        self.assertIn("<DesiredVolume>42</DesiredVolume>", body)

    def test_set_volume_clamps(self):
        self.bar.set_volume(500)
        self.assertIn("<DesiredVolume>100</DesiredVolume>", self.fake.last()[1])
        self.bar.set_volume(-10)
        self.assertIn("<DesiredVolume>0</DesiredVolume>", self.fake.last()[1])

    def test_get_mute(self):
        self.assertTrue(self.bar.get_mute())

    def test_set_mute(self):
        self.bar.set_mute(True)
        self.assertIn("<DesiredMute>1</DesiredMute>", self.fake.last()[1])
        self.bar.set_mute(False)
        self.assertIn("<DesiredMute>0</DesiredMute>", self.fake.last()[1])

    def test_transport_state(self):
        self.assertEqual(self.bar.transport_state(), "PLAYING")

    def test_position(self):
        p = self.bar.position()
        self.assertEqual(p["duration"], "0:03:21")
        self.assertEqual(p["elapsed"], "0:00:42")
        self.assertEqual(p["uri"], "http://x/y.wav")

    def test_set_uri_escapes_metadata(self):
        meta = Soundbar.didl("http://a/b.wav", "T&T", "audio/x-wav", "F")
        self.bar.set_uri("http://a/b.wav?q=1&r=2", meta)
        action, body = self.fake.last()
        self.assertIn(f"{AVTRANSPORT}#SetAVTransportURI", action)
        # the URL's ampersand must be escaped in the XML payload
        self.assertIn("q=1&amp;r=2", body)
        # metadata is nested XML, so its markup must be escaped too
        self.assertIn("&lt;DIDL-Lite", body)

    def test_play_pause_stop(self):
        self.bar.play()
        self.assertIn("#Play", self.fake.last()[0])
        self.bar.pause()
        self.assertIn("#Pause", self.fake.last()[0])
        self.bar.stop()
        self.assertIn("#Stop", self.fake.last()[0])

    def test_seek_sends_rel_time(self):
        self.bar.seek("0:01:30")
        _, body = self.fake.last()
        self.assertIn("<Unit>REL_TIME</Unit>", body)
        self.assertIn("<Target>0:01:30</Target>", body)

    def test_http_error_becomes_soundbar_error(self):
        self.fake.fail_next = True
        with self.assertRaises(SoundbarError):
            self.bar.get_volume()

    def test_unreachable_host_raises(self):
        bar = Soundbar(ip="127.0.0.1", dmr_port=1)     # nothing listening
        with self.assertRaises(SoundbarError):
            bar.get_volume()


class TestReachability(unittest.TestCase):
    def test_reachable(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(Soundbar(ip="127.0.0.1", dmr_port=port).is_reachable())
        finally:
            srv.close()

    def test_not_reachable(self):
        self.assertFalse(
            Soundbar(ip="127.0.0.1", dmr_port=1).is_reachable(timeout=0.5))


if __name__ == "__main__":
    unittest.main()
