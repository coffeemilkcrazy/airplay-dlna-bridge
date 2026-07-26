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
import dataclasses
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

# Human-facing release, shown in the web panel. Bump on a meaningful change;
# independent of the git revision, which tracks what is actually deployed.
APP_VERSION = "1.2"

# Auto power-off must never land while the release path is still running: the
# renderer would be told to power down mid-Stop and keep our stream connection
# with no controller left to clear it. Keep a margin past idle_stop_seconds.
AUTO_OFF_MARGIN_SECONDS = 10.0

ENV_FILE = "bridge.env"


def default_config_dir() -> str:
    """Where install.sh puts bridge.env, per platform (see its CONF_DIR)."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/.config/airplay-dlna-bridge")
    return "/etc/airplay-soundbar"


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
    # Offered by the web panel's settings form. Deliberately a small set:
    #   * the volume cap and floor are the one thing enforced server-side, so
    #     letting a client raise them would defeat the point;
    #   * POWER_OFF_COMMAND / POWER_ON_COMMAND run a shell as the service user,
    #     and the panel binds every interface with no token by default, so an
    #     editable command would be remote code execution on the LAN;
    #   * STATUS_* can make the panel unreachable from the panel itself;
    #   * the shairport and metadata paths belong to the installer.
    editable: bool = False
    # Applied to the running process. Everything else needs a restart, because
    # it is read once at startup.
    live: bool = False


SETTINGS: list[Setting] = [
    Setting("soundbar_ip", "SOUNDBAR_IP", "--soundbar", "",
            "soundbar IP; empty means auto-discover via SSDP", editable=True),
    Setting("airplay_name", "AIRPLAY_NAME", "--name", "Soundbar",
            "name shown in the AirPlay menu", editable=True),
    Setting("shairport_bin", "SHAIRPORT_BIN", "--shairport-bin",
            "shairport-sync", "path to the shairport-sync binary"),
    Setting("shairport_config", "SHAIRPORT_CONFIG", "--shairport-config",
            "/etc/airplay-soundbar/shairport.conf",
            "shairport-sync config file; ignored if absent"),
    Setting("metadata_pipe", "METADATA_PIPE", "--metadata-pipe",
            "/tmp/shairport-sync-metadata",
            "FIFO shairport-sync writes now-playing metadata to"),
    Setting("stream_port", "STREAM_PORT", "--stream-port", 8770,
            "port the endless-WAV audio stream is served on", int,
            editable=True),
    Setting("status_port", "STATUS_PORT", "--status-port", 8772,
            "port for the web panel and status API", int),
    Setting("status_bind", "STATUS_BIND", "--status-bind", "0.0.0.0",
            "address the status API listens on; 127.0.0.1 keeps it local"),
    Setting("status_token", "STATUS_TOKEN", "--status-token", "",
            "require this token (X-Bridge-Token header or ?token=)"),
    Setting("advertise_ip", "ADVERTISE_IP", "--advertise-ip", "",
            "address the soundbar fetches audio from; empty means auto",
            editable=True),
    Setting("idle_stop_seconds", "IDLE_STOP", "--idle-stop", 20.0,
            "seconds of silence before releasing the soundbar", float,
            editable=True, live=True),
    Setting("auto_off_minutes", "AUTO_OFF", "--auto-off", 0.0,
            "minutes of silence before powering the soundbar off; 0 disables",
            float, editable=True, live=True),
    Setting("power_off_command", "POWER_OFF_COMMAND", "--power-off-command", "",
            "shell command that powers the soundbar off, for renderers with "
            "no network power-off of their own"),
    Setting("power_on_command", "POWER_ON_COMMAND", "--power-on-command", "",
            "shell command that powers it back on; the inverse of the above"),
    Setting("min_volume", "MIN_VOLUME", "--min-volume", 0,
            "raise the soundbar to at least this on play; 0 disables", int),
    Setting("max_volume", "MAX_VOLUME", "--max-volume", 12,
            "never set the soundbar above this (1-100)", int),
    Setting("version", "BRIDGE_VERSION", "--bridge-version", "unknown",
            "git revision this was deployed from; shown in /status"),
    Setting("config_dir", "CONFIG_DIR", "--config-dir", default_config_dir(),
            "directory holding bridge.env; where panel edits are saved"),
]

BY_NAME = {s.name: s for s in SETTINGS}
BY_ENV = {s.env: s for s in SETTINGS}
EDITABLE = [s for s in SETTINGS if s.editable]


# --------------------------------------------------------------------------- #
# bridge.env - the one place a setting is persisted.
#
# systemd loads it as an EnvironmentFile, so on Linux its values also arrive as
# real environment variables; macOS bakes them into the LaunchAgent plist
# instead. Reading the file directly is what makes both platforms behave the
# same, and it is what lets the web panel persist a change: install.sh carries
# every value already in this file forward across re-installs, so a panel edit
# survives the next deploy, while a value passed explicitly to the installer
# still wins.
# --------------------------------------------------------------------------- #
def read_env_file(config_dir: str) -> dict[str, str]:
    """Parse bridge.env. Missing or unreadable is not an error - the bridge
    runs perfectly well from environment variables alone."""
    values: dict[str, str] = {}
    try:
        with open(os.path.join(config_dir, ENV_FILE), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        return {}
    return values


def write_env_file(config_dir: str, updates: dict[str, str]) -> tuple[bool, str]:
    """Merge `updates` into bridge.env, leaving every other value alone.

    Written to a temporary file and renamed, because a half-written config is
    one the service will not start from - and it would be rewritten at exactly
    the moment someone is restarting to apply it.
    """
    path = os.path.join(config_dir, ENV_FILE)
    values = read_env_file(config_dir)
    values.update(updates)

    body = ["# Environment for the airplay-soundbar service",
            "# Generated by install.sh; also updated by the web panel."]
    body += [f"{k}={v}" for k, v in values.items()]
    tmp = path + ".tmp"
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"could not write {path}: {e}"
    return True, path


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
    auto_off_minutes: float = 0.0
    power_off_command: str = ""
    power_on_command: str = ""
    min_volume: int = 0
    max_volume: int = 12
    version: str = "unknown"
    config_dir: str = default_config_dir()
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
        # Auto-off is measured from the same silence that drives the release,
        # so anything at or below idle_stop would fire while the release is
        # still in flight. Raise it rather than refusing to start.
        self.auto_off_minutes = max(0.0, float(self.auto_off_minutes))
        if self.auto_off_minutes > 0:
            floor = (float(self.idle_stop_seconds)
                     + AUTO_OFF_MARGIN_SECONDS) / 60.0
            self.auto_off_minutes = max(self.auto_off_minutes, floor)

    @property
    def auto_off_seconds(self) -> float:
        """0 when disabled. Minutes are the friendly unit; the session loop
        works in seconds, as does everything else timing-related."""
        return self.auto_off_minutes * 60.0

    # ------------------------------------------------------------------ #
    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "Config":
        # Where bridge.env lives has to be settled before it can be read, so
        # that one option comes from the environment or the command line only.
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--config-dir",
                         default=os.environ.get("CONFIG_DIR")
                         or default_config_dir())
        config_dir = pre.parse_known_args(argv)[0].config_dir
        # The file wins over the environment: on macOS the environment comes
        # from a LaunchAgent plist that only install.sh rewrites, so a panel
        # edit would otherwise be invisible until the next deploy.
        stored = read_env_file(config_dir)
        stored.pop("CONFIG_DIR", None)

        p = argparse.ArgumentParser(
            description="AirPlay bridge for UPnP/DLNA renderers")
        for s in SETTINGS:
            raw = stored.get(s.env)
            if raw in (None, ""):
                raw = os.environ.get(s.env)
            try:
                default = s.kind(raw) if raw not in (None, "") else s.default
            except ValueError:
                default = s.default
            if s.name == "config_dir":
                default = config_dir
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


def env_text(value: Any) -> str:
    """Render a value for bridge.env the way a person would write it."""
    return f"{value:g}" if isinstance(value, float) else str(value)


def describe_editable(cfg: "Config", stored: dict | None = None) -> list[dict]:
    """What the panel needs to build its form, from the same table that drives
    everything else - so a new editable option appears there automatically.

    `value` is what is saved and what the form shows; `running` is what this
    process is actually using. They differ after saving something that only
    takes effect on restart, and saying so is the whole point: a setting that
    silently has not taken effect yet is the kind of quiet mismatch this
    project keeps being bitten by.
    """
    stored = stored or {}
    out = []
    for s in EDITABLE:
        running = getattr(cfg, s.name)
        saved = running
        raw = stored.get(s.env)
        if raw not in (None, ""):
            try:
                saved = s.kind(raw)
            except (TypeError, ValueError):
                saved = running
        out.append({
            "env": s.env,
            "help": s.help,
            "value": saved,
            "running": running,
            "kind": s.kind.__name__,
            "live": s.live,
            "pending": saved != running,
        })
    return out


def apply_settings(cfg: "Config", changes: dict) -> tuple[dict, dict]:
    """Validate `changes` (keyed by environment variable name) and work out
    what would actually be stored.

    Returns (applied, errors). Nothing is applied when anything fails: a form
    that half-saved is worse than one that refused. The values in `applied` can
    differ from what was asked for, because normalise() still has the last word
    exactly as it does at startup - so the panel cannot set an auto-off that
    would fire while the speaker is still being released.
    """
    editable = {s.env: s for s in EDITABLE}
    coerced: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for env_name, raw in changes.items():
        s = editable.get(env_name)
        if s is None:
            # Server-side, like the volume cap: the form is generated from
            # this table, so anything else arriving here was hand-crafted.
            errors[env_name] = "not editable"
            continue
        text = "" if raw is None else str(raw).strip()
        try:
            value = s.kind(text) if text != "" else s.default
        except (TypeError, ValueError):
            errors[env_name] = f"expected a {s.kind.__name__}"
            continue
        problem = _validate(s, value)
        if problem:
            errors[env_name] = problem
            continue
        coerced[s.name] = value

    if errors:
        return {}, errors

    # Same cross-field rules as startup, rather than a second copy of them.
    trial = dataclasses.replace(cfg, **coerced)
    applied = {s.env: getattr(trial, s.name) for s in EDITABLE
               if s.name in coerced}
    return applied, {}


def _validate(s: Setting, value: Any) -> str:
    """Refuse nonsense rather than clamping it: a mistyped port should come
    back as an error, not be silently turned into a different port."""
    if s.env.endswith("_PORT"):
        return ("" if 1 <= value <= 65535
                else "port must be between 1 and 65535")
    # An AirPlay name may contain spaces; an address may not.
    if s.env.endswith("_IP") and value and any(c.isspace() for c in str(value)):
        return "an address cannot contain spaces"
    if s.kind in (int, float) and value < 0:
        return "cannot be negative"
    return ""


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
