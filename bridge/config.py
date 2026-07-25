"""Runtime configuration for the bridge — declared once, used everywhere.

Every knob appears in SETTINGS below. From that single table we derive:

  * the argparse interface        (Config.from_args)
  * the environment variable read for each option
  * the list the installer writes into bridge.env, and the list deploy.sh
    forwards over ssh   (`python3 config.py --env-names`)

Previously each new option had to be added by hand in four places — the CLI,
the service env file, the installer and the deploy script — which is tedious
and easy to get half-right. Adding one line here now covers all of them.

The Config fields are still written out explicitly rather than generated:
generating them made the class opaque and fought dataclass ordering rules for
no real gain. `test_config.py` asserts the two stay in step.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

# Human-facing release, shown in the web panel. Bump on a meaningful change;
# independent of the git revision, which tracks what is actually deployed.
APP_VERSION = "1.1"


@dataclass(frozen=True)
class Setting:
    name: str                       # attribute on Config
    env: str                        # environment variable
    cli: str                        # command-line flag
    default: Any
    help: str
    kind: Callable[[str], Any] = str
    # Written to bridge.env and carried across re-installs. Build-time options
    # (AIRPLAY2, REBUILD, ...) live in the installer; they are not runtime.
    persist: bool = True


SETTINGS: list[Setting] = [
    Setting("soundbar_ip", "SOUNDBAR_IP", "--soundbar", "",
            "soundbar IP; empty means auto-discover via SSDP"),
    Setting("airplay_name", "AIRPLAY_NAME", "--name", "Soundbar",
            "name shown in the AirPlay menu"),
    Setting("shairport_bin", "SHAIRPORT_BIN", "--shairport-bin",
            "shairport-sync", "path to the shairport-sync binary"),
    Setting("shairport_config", "SHAIRPORT_CONFIG", "--shairport-config",
            "/etc/airplay-soundbar/shairport.conf",
            "shairport-sync config file; ignored if absent"),
    Setting("metadata_pipe", "METADATA_PIPE", "--metadata-pipe",
            "/tmp/shairport-sync-metadata",
            "FIFO shairport-sync writes now-playing metadata to"),
    Setting("stream_port", "STREAM_PORT", "--stream-port", 8770,
            "port the endless-WAV audio stream is served on", int),
    Setting("status_port", "STATUS_PORT", "--status-port", 8772,
            "port for the web panel and status API", int),
    Setting("status_bind", "STATUS_BIND", "--status-bind", "0.0.0.0",
            "address the status API listens on; 127.0.0.1 keeps it local"),
    Setting("status_token", "STATUS_TOKEN", "--status-token", "",
            "require this token (X-Bridge-Token header or ?token=)"),
    Setting("advertise_ip", "ADVERTISE_IP", "--advertise-ip", "",
            "address the soundbar fetches audio from; empty means auto"),
    Setting("idle_stop_seconds", "IDLE_STOP", "--idle-stop", 20.0,
            "seconds of silence before releasing the soundbar", float),
    Setting("min_volume", "MIN_VOLUME", "--min-volume", 0,
            "raise the soundbar to at least this on play; 0 disables", int),
    Setting("max_volume", "MAX_VOLUME", "--max-volume", 12,
            "never set the soundbar above this (1-100)", int),
    Setting("version", "BRIDGE_VERSION", "--bridge-version", "unknown",
            "git revision this was deployed from; shown in /status"),
]

BY_NAME = {s.name: s for s in SETTINGS}


@dataclass
class Config:
    soundbar_ip: str = ""
    airplay_name: str = "Soundbar"
    shairport_bin: str = "shairport-sync"
    shairport_config: str = "/etc/airplay-soundbar/shairport.conf"
    metadata_pipe: str = "/tmp/shairport-sync-metadata"
    stream_port: int = 8770
    status_port: int = 8772
    status_bind: str = "0.0.0.0"
    status_token: str = ""
    advertise_ip: str = ""
    idle_stop_seconds: float = 20.0
    min_volume: int = 0
    max_volume: int = 12
    version: str = "unknown"
    # not persisted - process-level switches
    run_shairport: bool = True
    verbose: bool = False

    def __post_init__(self):
        self.normalise()

    def normalise(self) -> None:
        """Cross-field constraints, applied however the Config was built."""
        self.max_volume = max(1, min(100, int(self.max_volume)))
        # The floor can never exceed the cap, or the two settings contradict.
        self.min_volume = max(0, min(self.max_volume, int(self.min_volume)))

    # ------------------------------------------------------------------ #
    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "Config":
        p = argparse.ArgumentParser(
            description="AirPlay bridge for UPnP/DLNA renderers")
        for s in SETTINGS:
            raw = os.environ.get(s.env)
            try:
                default = s.kind(raw) if raw not in (None, "") else s.default
            except ValueError:
                default = s.default
            kwargs = {"default": default, "help": s.help}
            if s.kind in (int, float):
                kwargs["type"] = s.kind
            p.add_argument(s.cli, **kwargs)
        p.add_argument("--no-shairport", action="store_true",
                       help="do not spawn shairport-sync; read PCM on stdin")
        p.add_argument("-v", "--verbose", action="store_true")
        a = p.parse_args(argv)

        logging.basicConfig(
            level=logging.DEBUG if a.verbose else logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S")

        values = {s.name: getattr(a, s.cli.lstrip("-").replace("-", "_"))
                  for s in SETTINGS}
        return cls(run_shairport=not a.no_shairport, verbose=a.verbose,
                   **values)


def env_names(persist_only: bool = True) -> list[str]:
    """Variable names for install.sh and deploy.sh, so the shell never
    hardcodes the list."""
    return [s.env for s in SETTINGS if s.persist or not persist_only]


def launchd_plist(app_dir: str, label: str = "com.airplay-dlna-bridge",
                  log_path: str = "/tmp/airplay-dlna-bridge.log",
                  env: dict | None = None) -> bytes:
    """Build the macOS LaunchAgent plist.

    Written with plistlib rather than assembled in the shell: values land in
    XML, and a device named "Kitchen & Den" produces a malformed plist that
    launchd silently refuses to load. plistlib escapes correctly, and Python
    is already a dependency.
    """
    import plistlib

    source = os.environ if env is None else env
    variables = {name: source.get(name, "") for name in env_names()
                 if source.get(name, "") != ""}

    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["/usr/bin/env", "python3",
                             os.path.join(app_dir, "bridge.py")],
        "EnvironmentVariables": variables,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    })


if __name__ == "__main__":
    if "--env-names" in sys.argv:
        print("\n".join(env_names()))
    elif "--launchd-plist" in sys.argv:
        i = sys.argv.index("--launchd-plist")
        app_dir = sys.argv[i + 1] if len(sys.argv) > i + 1 else "."
        sys.stdout.buffer.write(launchd_plist(app_dir))
    elif "--app-version" in sys.argv:
        print(APP_VERSION)
    else:
        for s in SETTINGS:
            print(f"{s.env:18} {s.cli:22} default={s.default!r}")
