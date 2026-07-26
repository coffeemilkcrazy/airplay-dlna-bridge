"""Control library for UPnP MediaRenderers, with extras for Samsung devices
that also expose the WAM (Wireless Audio Multiroom) API.

Any renderer implementing AVTransport and RenderingControl should work. The
Samsung-specific parts (WAM, product-type detection) degrade gracefully when
absent. Developed and verified against a Samsung HW-N850, whose endpoints are:
  * UPnP MediaRenderer  : http://<ip>:9197/dmr
  * Samsung WAM API     : http://<ip>:56001/UIC?cmd=...

Standard library only, so it runs unchanged on macOS and on Raspberry Pi OS.
"""

from __future__ import annotations

import html
import re
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

SSDP_ADDR = ("239.255.255.250", 1900)

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNMGR = "urn:schemas-upnp-org:service:ConnectionManager:1"


class SoundbarError(RuntimeError):
    pass


def _text(xml: str, tag: str, default: str = "") -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else default


@dataclass
class Soundbar:
    """A Samsung soundbar reachable on the LAN."""

    ip: str
    name: str = ""
    model: str = ""
    product_type: str = ""          # "SOUNDBAR", "TV", ... when advertised
    dmr_port: int = 9197
    wam_port: int = 56001
    _protocols: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ #
    @staticmethod
    def product_type_from_description(desc: str) -> str:
        """Classify a UPnP device description.

        Samsung TVs and soundbars both advertise a MediaRenderer, so a plain
        "first Samsung device wins" search happily returns the television -
        which accepts SetAVTransportURI and Play, then never fetches the media,
        producing a failure that looks exactly like a broken soundbar.

        Soundbars carry vdProductType=SOUNDBAR in sec:ProductCap; fall back to
        the model description, which reads "Samsung SOUNDBAR DMR".
        """
        m = re.search(r"vdProductType=([A-Za-z_]+)", desc)
        if m:
            return m.group(1).upper()
        blob = " ".join([_text(desc, "modelDescription"),
                         _text(desc, "friendlyName")]).upper()
        if "SOUNDBAR" in blob:
            return "SOUNDBAR"
        if "TV" in blob:
            return "TV"
        return ""

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    @classmethod
    def discover(cls, timeout: float = 4.0, rank: bool = True,
                 vendor: str = "") -> list["Soundbar"]:
        """Find UPnP MediaRenderers via SSDP.

        SSDP replies arrive in arbitrary order, and a network usually holds
        several renderers - a TV, a phone, a Chromecast. `rank` therefore sorts
        by how likely each is to be the intended target:

            soundbars first, then unknown devices, televisions last

        TVs are deliberately last: they accept SetAVTransportURI and Play and
        then never fetch the media, which looks exactly like a broken soundbar
        and is genuinely confusing to debug.

        Pass `vendor` to keep only devices whose description mentions it
        (case-insensitive), e.g. vendor="samsung".
        """
        msg = "\r\n".join([
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
            'MAN: "ssdp:discover"',
            "MX: 3",
            "ST: urn:schemas-upnp-org:device:MediaRenderer:1",
            "", "",
        ])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(msg.encode(), SSDP_ADDR)

        locations: dict[str, str] = {}
        try:
            while True:
                data, addr = sock.recvfrom(65507)
                for line in data.decode(errors="ignore").split("\r\n"):
                    if line.lower().startswith("location:"):
                        locations[addr[0]] = line.split(":", 1)[1].strip()
        except socket.timeout:
            pass
        finally:
            sock.close()

        found: list[Soundbar] = []
        for ip, loc in locations.items():
            try:
                with urllib.request.urlopen(loc, timeout=5) as r:
                    desc = r.read().decode(errors="ignore")
            except Exception:
                continue
            if vendor and vendor.lower() not in desc.lower():
                continue
            port = urllib.parse.urlparse(loc).port or 9197
            found.append(cls(
                ip=ip,
                name=_text(desc, "friendlyName"),
                model=_text(desc, "modelName"),
                product_type=cls.product_type_from_description(desc),
                dmr_port=port,
            ))

        if rank:
            order = {"SOUNDBAR": 0, "": 1, "TV": 2}
            found.sort(key=lambda d: order.get(d.product_type, 1))
        return found

    # ------------------------------------------------------------------ #
    # UPnP
    # ------------------------------------------------------------------ #
    def _control_url(self, service: str) -> str:
        leaf = {
            AVTRANSPORT: "AVTransport1",
            RENDERING: "RenderingControl1",
            CONNMGR: "ConnectionManager1",
        }[service]
        return f"http://{self.ip}:{self.dmr_port}/upnp/control/{leaf}"

    def upnp(self, service: str, action: str, timeout: float = 8, **args) -> str:
        body = "".join(
            f"<{k}>{html.escape(str(v), quote=False)}</{k}>" for k, v in args.items()
        )
        envelope = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body><u:{action} xmlns:u=\"{service}\">{body}</u:{action}></s:Body>"
            "</s:Envelope>"
        )
        req = urllib.request.Request(
            self._control_url(service),
            data=envelope.encode(),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(errors="ignore")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = _text(e.read().decode(errors="ignore"), "errorDescription")
            except Exception:
                pass
            finally:
                # HTTPError holds an open socket; without this the connection
                # leaks on every SOAP fault.
                e.close()
            raise SoundbarError(f"{action} failed: HTTP {e.code} {detail}") from e
        except Exception as e:
            raise SoundbarError(f"{action} failed: {e}") from e

    # --- rendering control (verified working on HW-N850) ---------------- #
    # Read-only calls take a timeout so status polling can fail fast: with the
    # default 8s, a powered-off soundbar makes every poll hang that long.
    def get_volume(self, timeout: float = 8) -> int:
        xml = self.upnp(RENDERING, "GetVolume", timeout=timeout,
                        InstanceID=0, Channel="Master")
        return int(_text(xml, "CurrentVolume", "0"))

    def set_volume(self, value: int) -> None:
        self.upnp(RENDERING, "SetVolume", InstanceID=0, Channel="Master",
                  DesiredVolume=max(0, min(100, int(value))))

    def get_mute(self, timeout: float = 8) -> bool:
        xml = self.upnp(RENDERING, "GetMute", timeout=timeout,
                        InstanceID=0, Channel="Master")
        return _text(xml, "CurrentMute", "0") in ("1", "true")

    def set_mute(self, muted: bool) -> None:
        self.upnp(RENDERING, "SetMute", InstanceID=0, Channel="Master",
                  DesiredMute=1 if muted else 0)

    # --- transport ------------------------------------------------------ #
    def transport_state(self, timeout: float = 8) -> str:
        xml = self.upnp(AVTRANSPORT, "GetTransportInfo", timeout=timeout,
                        InstanceID=0)
        return _text(xml, "CurrentTransportState", "UNKNOWN")

    def position(self, timeout: float = 8) -> dict[str, str]:
        xml = self.upnp(AVTRANSPORT, "GetPositionInfo", timeout=timeout,
                        InstanceID=0)
        return {
            "track": _text(xml, "Track"),
            "duration": _text(xml, "TrackDuration"),
            "elapsed": _text(xml, "RelTime"),
            "uri": _text(xml, "TrackURI"),
        }

    def set_uri(self, url: str, metadata: str = "") -> None:
        self.upnp(AVTRANSPORT, "SetAVTransportURI", InstanceID=0,
                  CurrentURI=url, CurrentURIMetaData=metadata)

    def set_next_uri(self, url: str, metadata: str = "") -> None:
        """Queue the following track - enables gapless handoff."""
        self.upnp(AVTRANSPORT, "SetNextAVTransportURI", InstanceID=0,
                  NextURI=url, NextURIMetaData=metadata)

    def play(self) -> None:
        self.upnp(AVTRANSPORT, "Play", InstanceID=0, Speed=1)

    def pause(self) -> None:
        self.upnp(AVTRANSPORT, "Pause", InstanceID=0)

    def stop(self) -> None:
        self.upnp(AVTRANSPORT, "Stop", InstanceID=0)

    def seek(self, hhmmss: str) -> None:
        self.upnp(AVTRANSPORT, "Seek", InstanceID=0, Unit="REL_TIME", Target=hhmmss)

    def stopped_reason(self) -> str:
        """Samsung vendor extension - why playback ended."""
        try:
            xml = self.upnp(AVTRANSPORT, "X_GetStoppedReason", InstanceID=0)
            return _text(xml, "StoppedReason") or "(empty)"
        except SoundbarError:
            return "(unsupported)"

    def supported_formats(self) -> list[str]:
        if not self._protocols:
            xml = self.upnp(CONNMGR, "GetProtocolInfo")
            sink = html.unescape(_text(xml, "Sink"))
            self._protocols = [p.strip() for p in sink.split(",") if p.strip()]
        return self._protocols

    # ------------------------------------------------------------------ #
    # Samsung WAM API
    # ------------------------------------------------------------------ #
    def wam(self, command: str, timeout: float = 6) -> str:
        url = f"http://{self.ip}:{self.wam_port}/UIC?cmd=" + urllib.parse.quote(
            command, safe=""
        )
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode(errors="ignore")
        except urllib.error.HTTPError as e:
            # Same open socket as in upnp(). It matters more here: a WAM
            # failure is a routine outcome rather than an exception - most
            # renderers answer nothing at all - so leaking one per attempt
            # would accumulate for the life of the process.
            e.close()
            raise SoundbarError(f"WAM {command[:40]}: {e}") from e
        except Exception as e:
            raise SoundbarError(f"WAM {command[:40]}: {e}") from e

    def wam_function(self) -> tuple[str, str]:
        """Return (function, submode), e.g. ('wifi', 'dlna')."""
        xml = self.wam("<name>GetFunc</name>")
        return _text(xml, "function"), _text(xml, "submode")

    def wam_info(self) -> dict[str, str]:
        xml = self.wam("<name>GetMainInfo</name>")
        return {
            "model": _text(xml, "spkmodelname"),
            "mac": _text(xml, "spkmacaddr"),
            "group": _text(xml, "grouptype"),
        }

    def wam_power(self, on: bool, timeout: float = 6) -> str:
        """Power the speaker on or off over the WAM API.

        UNVERIFIED ON HW-* SOUNDBARS. SetPowerStatus is what the WAM/R-series
        speakers this protocol targets use; soundbars implement only a subset,
        and an unsupported command gets no response at all rather than an error
        (see wam_play_url), so it surfaces as a SoundbarError after `timeout`.

        Callers must therefore read a failure as "this device cannot be powered
        over the network" and fall back to something else, not as a transient
        fault worth retrying. Probe a real device with:

            python3 tools/diagnose.py --test-power
        """
        return self.wam(
            "<name>SetPowerStatus</name>"
            f'<p type="dec" name="powerstatus" val="{1 if on else 0}"/>',
            timeout=timeout)

    def wam_play_url(self, url: str, buffersize: int = 0) -> str:
        """Ask the speaker to fetch and play a URL directly (no UPnP).

        NOT SUPPORTED ON HW-* SOUNDBARS. Verified against an HW-N850: this
        command, like GetPlayStatus, gets no response at all and raises
        SoundbarError after the timeout, whereas GetFunc/GetVolume/GetMainInfo
        answer instantly. Soundbars implement only a subset of the WAM API that
        the WAM/R-series speakers this protocol targets.

        Use the UPnP path instead - set_uri() followed by play() - which is
        what the bridge does. This method is kept for genuine WAM speakers.
        """
        cmd = (
            "<name>SetUrlPlayback</name>"
            f'<p type="cdata" name="url" val="empty"><![CDATA[{url}]]></p>'
            f'<p type="dec" name="buffersize" val="{buffersize}"/>'
            '<p type="dec" name="seektime" val="0"/>'
            '<p type="dec" name="resume" val="1"/>'
        )
        return self.wam(cmd)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def didl(url: str, title: str, mime: str, dlna_features: str,
             duration: str | None = None, size: int | None = None) -> str:
        """Build the DIDL-Lite metadata blob renderers expect alongside a URI."""
        extra = ""
        if duration:
            extra += f' duration="{duration}"'
        if size is not None:
            extra += f' size="{size}"'
        upnp_class = ("object.item.audioItem.audioBroadcast" if duration is None
                      else "object.item.audioItem.musicTrack")
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1">'
            f"<dc:title>{html.escape(title)}</dc:title>"
            f"<upnp:class>{upnp_class}</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:{dlna_features}"{extra}>'
            f"{html.escape(url)}</res>"
            "</item></DIDL-Lite>"
        )

    def is_reachable(self, port: int | None = None, timeout: float = 3) -> bool:
        try:
            s = socket.create_connection((self.ip, port or self.dmr_port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False
