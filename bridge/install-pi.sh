#!/usr/bin/env bash
#
# Install the AirPlay -> Samsung soundbar bridge on a Raspberry Pi.
# Run ON the Pi, as a user with sudo:
#
#     sudo ./install-pi.sh [SOUNDBAR_IP] [AIRPLAY_NAME]
#
# Builds shairport-sync from source with AirPlay 2 support (the Debian package
# is AirPlay 1 only), installs nqptp for PTP timing, then installs the bridge
# as a systemd service.
#
# Idempotent: re-running skips builds that are already in place. Force a
# rebuild with  REBUILD=1 sudo ./install-pi.sh
#
# Set AIRPLAY2=0 to use the (much faster) Debian AirPlay 1 package instead.

set -euo pipefail

SOUNDBAR_IP="${1:-${SOUNDBAR_IP:-}}"
AIRPLAY_NAME="${2:-${AIRPLAY_NAME:-Soundbar}}"
AIRPLAY2="${AIRPLAY2:-1}"
REBUILD="${REBUILD:-0}"
JOBS="$(nproc)"

APP_DIR=/opt/airplay-soundbar
CONF_DIR=/etc/airplay-soundbar
BUILD_DIR=/usr/local/src
METADATA_PIPE=/tmp/shairport-sync-metadata
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo:  sudo $0 $*" >&2
    exit 1
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------- #
# The packaged service binds AirPlay port 5000 and would stop our own instance
# from starting ("could not establish a service on port 5000"). Mask it rather
# than merely disable it, since apt re-enables units on upgrade.
# --------------------------------------------------------------------------- #
say "Clearing any existing shairport-sync service"
systemctl disable --now shairport-sync.service 2>/dev/null || true
systemctl mask shairport-sync.service 2>/dev/null || true
pkill -x shairport-sync 2>/dev/null || true
sleep 1

# --------------------------------------------------------------------------- #
if [[ "$AIRPLAY2" == "1" ]]; then

    have_airplay2() {
        command -v shairport-sync >/dev/null 2>&1 && \
            shairport-sync -V 2>/dev/null | grep -qi 'AirPlay2'
    }

    if have_airplay2 && [[ "$REBUILD" != "1" ]]; then
        say "shairport-sync already has AirPlay 2 support"
        note "$(shairport-sync -V 2>/dev/null | head -1)"
    else
        say "Installing build dependencies for AirPlay 2"
        apt-get update -qq
        # The AirPlay 2 build additionally needs plist/sodium/gcrypt/ffmpeg
        # and uuid, none of which the Debian AirPlay 1 package pulls in.
        # libplist-utils supplies the `plistutil` binary that shairport-sync's
        # configure requires for AirPlay 2 - libplist-dev alone is not enough.
        apt-get install -y --no-install-recommends \
            build-essential git autoconf automake libtool pkg-config xxd \
            libpopt-dev libconfig-dev libasound2-dev \
            avahi-daemon libavahi-client-dev \
            libssl-dev libsoxr-dev \
            libplist-dev libplist-utils libsodium-dev libgcrypt-dev \
            libavutil-dev libavcodec-dev libavformat-dev uuid-dev \
            libdaemon-dev python3

        for tool in plistutil xxd; do
            command -v "$tool" >/dev/null || {
                echo "ERROR: '$tool' still missing after apt install." >&2
                exit 1
            }
        done

        # Remove the Debian binary so only our AirPlay 2 build is on PATH.
        if dpkg -s shairport-sync >/dev/null 2>&1; then
            say "Removing the Debian shairport-sync package (AirPlay 1 only)"
            apt-get purge -y shairport-sync
        fi

        say "Building nqptp (PTP timing daemon required by AirPlay 2)"
        mkdir -p "$BUILD_DIR"
        if [[ ! -d "$BUILD_DIR/nqptp/.git" ]]; then
            git clone --depth 1 https://github.com/mikebrady/nqptp.git \
                "$BUILD_DIR/nqptp"
        else
            git -C "$BUILD_DIR/nqptp" pull --ff-only || true
        fi
        (
            cd "$BUILD_DIR/nqptp"
            autoreconf -fi
            ./configure --with-systemd-startup
            make -j"$JOBS"
            make install
        )
        systemctl enable --now nqptp
        note "nqptp: $(systemctl is-active nqptp)"

        say "Building shairport-sync with AirPlay 2 (a few minutes)"
        if [[ ! -d "$BUILD_DIR/shairport-sync/.git" ]]; then
            git clone --depth 1 https://github.com/mikebrady/shairport-sync.git \
                "$BUILD_DIR/shairport-sync"
        else
            git -C "$BUILD_DIR/shairport-sync" pull --ff-only || true
        fi
        (
            cd "$BUILD_DIR/shairport-sync"
            autoreconf -fi
            # No --with-systemd: the bridge spawns shairport-sync itself, so we
            # deliberately do not install a competing service unit.
            ./configure \
                --sysconfdir=/etc \
                --with-airplay-2 \
                --with-stdout \
                --with-pipe \
                --with-metadata \
                --with-avahi \
                --with-ssl=openssl \
                --with-soxr
            make -j"$JOBS"
            make install
        )
        hash -r
        ldconfig

        if ! have_airplay2; then
            echo "ERROR: build finished but AirPlay 2 is not reported by" >&2
            echo "       shairport-sync -V. Aborting." >&2
            shairport-sync -V >&2 || true
            exit 1
        fi
        say "Built: $(shairport-sync -V | head -1)"
    fi

else
    say "Installing shairport-sync from Debian (AirPlay 1)"
    apt-get update -qq
    apt-get install -y --no-install-recommends shairport-sync avahi-daemon python3
    systemctl disable --now shairport-sync.service 2>/dev/null || true
    systemctl mask shairport-sync.service 2>/dev/null || true
fi

SHAIRPORT_BIN="$(command -v shairport-sync)"
note "using ${SHAIRPORT_BIN}"

# --------------------------------------------------------------------------- #
say "Installing bridge to ${APP_DIR}"
install -d -m 755 "$APP_DIR"
for mod in config.py soundbar.py streamer.py metadata.py dacp.py api.py webui.py; do
    install -m 644 "$SRC_DIR/$mod" "$APP_DIR/$mod"
done
install -m 755 "$SRC_DIR/bridge.py" "$APP_DIR/bridge.py"

# --------------------------------------------------------------------------- #
say "Writing configuration to ${CONF_DIR}"
install -d -m 755 "$CONF_DIR"

# The config files are regenerated on every install, so carry forward whatever
# was chosen last time unless this run explicitly overrides it. Without this a
# plain ./deploy.sh would silently revert settings the user had opted into.
prev_env() {
    [[ -f "$CONF_DIR/bridge.env" ]] && \
        sed -n "s/^$1=//p" "$CONF_DIR/bridge.env" | head -1
}

# Carry forward every runtime setting that this run did not explicitly set,
# so a plain ./deploy.sh never silently reverts a configured value.
while read -r var; do
    [[ -z "$var" ]] && continue
    if [[ -z "${!var:-}" ]]; then
        prev="$(prev_env "$var")"
        if [[ -n "$prev" ]]; then
            export "$var=$prev"
            note "keeping ${var}=${prev} from previous install"
        fi
    fi
done < <(python3 "$SRC_DIR/config.py" --env-names)

# Defaults for the two that must never be empty.
MAX_VOLUME="${MAX_VOLUME:-12}"
MIN_VOLUME="${MIN_VOLUME:-0}"
STATUS_BIND="${STATUS_BIND:-0.0.0.0}"
SOUNDBAR_IP="${SOUNDBAR_IP:-}"
AIRPLAY_NAME="${AIRPLAY_NAME:-Soundbar}"
SHAIRPORT_BIN="${SHAIRPORT_BIN:-}"

if [[ -z "${BITPERFECT:-}" ]]; then
    if [[ -f "$CONF_DIR/shairport.conf" ]] && \
       grep -qE '^[[:space:]]*ignore_volume_control[[:space:]]*=[[:space:]]*"yes"' \
            "$CONF_DIR/shairport.conf"; then
        BITPERFECT=1
        note "keeping BITPERFECT=1 from previous install"
    else
        BITPERFECT=0
    fi
fi

if [[ -z "${SHAIRPORT_VERBOSITY:-}" ]]; then
    SHAIRPORT_VERBOSITY="$(sed -n \
        's/.*log_verbosity[[:space:]]*=[[:space:]]*\([0-9]\).*/\1/p' \
        "$CONF_DIR/shairport.conf" 2>/dev/null | head -1)"
    SHAIRPORT_VERBOSITY="${SHAIRPORT_VERBOSITY:-0}"
fi

# Pin AirPlay to the interface that actually reaches the LAN. Without this,
# shairport-sync registers on every interface - including Docker bridges
# (172.x) - so senders receive unreachable addresses for the AirPlay control
# and timing channels. Audio starts, then the session dies seconds later.
PRIMARY_IFACE="${PRIMARY_IFACE:-$(ip route show default | awk '/default/{print $5; exit}')}"
if [[ -n "$PRIMARY_IFACE" ]]; then
    note "binding AirPlay to interface ${PRIMARY_IFACE}"
    IFACE_LINE="    interface = \"${PRIMARY_IFACE}\";"
else
    note "could not detect primary interface - leaving unbound"
    IFACE_LINE="    // interface not detected"
fi

# Bit-perfect mode: shairport-sync applies the sender's AirPlay volume by
# scaling the PCM in software, which requantises 16-bit samples. Ignoring it
# passes AirPlay's ALAC 16/44.1 through untouched - the best quality the
# protocol can carry. The cost is that the Mac's AirPlay volume slider stops
# doing anything; volume must then come from the soundbar (or the menu bar app).
if [[ "$BITPERFECT" == "1" ]]; then
    note "bit-perfect mode ON - the Mac's AirPlay volume slider is disabled"
    VOLUME_LINE='    ignore_volume_control = "yes";'
else
    VOLUME_LINE='    // ignore_volume_control = "yes";  // set BITPERFECT=1'
fi

cat > "$CONF_DIR/shairport.conf" <<CONF
// Managed by install-pi.sh - edits are overwritten on reinstall.
general = {
    name = "${AIRPLAY_NAME}";
    output_backend = "stdout";
${IFACE_LINE}
${VOLUME_LINE}
};

stdout = {
    // MUST match the WAV header the bridge advertises (RATE/CHANNELS/BITS in
    // bridge.py). Do not change one without the other.
    //
    // These are not optional: with AirPlay 2 the stdout backend defaults to
    // S32_LE @ 48000, so the soundbar decodes 32-bit samples as 16-bit and
    // plays loud noise ("fizzy" audio) at the wrong pitch. shairport-sync
    // transcodes to these values in C, which is cheaper and more correct than
    // converting in the bridge.
    output_rate = 44100;
    output_format = "S16_LE";
    output_channels = 2;
};

metadata = {
    enabled = "yes";
    include_cover_art = "no";
    pipe_name = "${METADATA_PIPE}";
    pipe_timeout = 5000;
};

sessioncontrol = {
    // shairport-sync rejects anything between 1 and 59 here; 60 is the
    // minimum it accepts. The bridge does its own, faster release via
    // --idle-stop, so this only bounds how long a dead AirPlay session lingers.
    session_timeout = 60;
};

diagnostics = {
    // 0 = errors only. Raise to 2 to see why a session ends:
    //   SHAIRPORT_VERBOSITY=2 ./deploy.sh
    // Output lands in the journal, prefixed "shairport:".
    log_verbosity = ${SHAIRPORT_VERBOSITY:-0};
};
CONF

# Written from the settings config.py declares, so a new option needs adding
# in exactly one place rather than here as well.
{
    echo "# Environment for airplay-soundbar.service"
    echo "# Generated by install-pi.sh - edits are overwritten on reinstall."
    while read -r var; do
        [[ -z "$var" ]] && continue
        printf '%s=%s\n' "$var" "${!var-}"
    done < <(python3 "$SRC_DIR/config.py" --env-names)
} > "$CONF_DIR/bridge.env"

chmod 644 "$CONF_DIR/shairport.conf" "$CONF_DIR/bridge.env"

# --------------------------------------------------------------------------- #
say "Installing systemd service"
install -m 644 "$SRC_DIR/systemd/airplay-soundbar.service" \
    /etc/systemd/system/airplay-soundbar.service
systemctl daemon-reload
systemctl enable airplay-soundbar.service
systemctl restart airplay-soundbar.service

sleep 4
say "Service status"
systemctl --no-pager --lines=20 status airplay-soundbar.service || true

# --------------------------------------------------------------------------- #
say "Smoke check"
SMOKE_FAIL=0

if systemctl is-active --quiet airplay-soundbar; then
    note "service active"
else
    echo "  FAIL: service is not active" >&2
    SMOKE_FAIL=1
fi

# Give the status API a moment, then confirm it actually answers.
for _ in 1 2 3 4 5; do
    STATUS_JSON="$(curl -s --max-time 4 "http://127.0.0.1:8772/status" || true)"
    [[ -n "$STATUS_JSON" ]] && break
    sleep 2
done

if [[ -n "$STATUS_JSON" ]]; then
    note "status API responding"
    RUNNING_RELEASE="$(printf '%s' "$STATUS_JSON" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version","?"))' \
        2>/dev/null || echo "?")"
    # Compare the git revision, not the release string: the release only
    # changes when someone bumps it, so it cannot detect a stale deploy.
    RUNNING_VERSION="$(printf '%s' "$STATUS_JSON" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision","?"))' \
        2>/dev/null || echo "?")"
    note "running v${RUNNING_RELEASE} (${RUNNING_VERSION})"
    if [[ "$RUNNING_VERSION" != "${BRIDGE_VERSION:-unknown}" ]]; then
        echo "  WARN: expected ${BRIDGE_VERSION:-unknown} but the service" >&2
        echo "        reports ${RUNNING_VERSION} - it may not have restarted." >&2
    fi
    SB_STATE="$(printf '%s' "$STATUS_JSON" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["soundbar"]["state"])' \
        2>/dev/null || echo "?")"
    note "soundbar reports: ${SB_STATE}"
    case "$SB_STATE" in
        error*|unknown) echo "  WARN: soundbar not reachable - is it powered on?" >&2 ;;
    esac
else
    echo "  FAIL: status API did not respond on 127.0.0.1:8772" >&2
    SMOKE_FAIL=1
fi

if [[ "$SMOKE_FAIL" == "1" ]]; then
    echo >&2
    echo "  Install completed but the smoke check failed. Inspect:" >&2
    echo "    journalctl -u airplay-soundbar -n 50 --no-pager" >&2
fi

PI_IP="$(hostname -I | awk '{print $1}')"
cat <<DONE

Installed.

  AirPlay name : ${AIRPLAY_NAME}
  AirPlay ver  : $( [[ "$AIRPLAY2" == "1" ]] && echo "2 (port 7000)" || echo "1 (port 5000)" )
  Soundbar     : ${SOUNDBAR_IP:-auto-discover}
  Status API   : http://${PI_IP}:8772/status

  journalctl -u airplay-soundbar -f      # follow the log
  systemctl restart airplay-soundbar     # restart after changes

On your Mac, "${AIRPLAY_NAME}" should now appear in the AirPlay menu
(Control Centre > Sound, or the AirPlay button in Music.app).
DONE
