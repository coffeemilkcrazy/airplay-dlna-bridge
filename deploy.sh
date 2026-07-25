#!/usr/bin/env bash
#
# Copy the bridge to the Raspberry Pi and install it there.
# Run from your Mac:
#
#     ./deploy.sh
#     ./deploy.sh pi@raspberrypi.local 192.0.2.10
#
# You will be prompted for the Pi's sudo password (an interactive TTY is used,
# so nothing is stored or echoed).

set -euo pipefail

# The installer runs under sudo on the Pi, which needs a real terminal to
# prompt for a password. Without one, ssh -t fails with an opaque sudo error
# after the files have already been copied - so check up front.
if [[ ! -t 0 || ! -t 1 ]]; then
    cat >&2 <<'NOTTY'
deploy.sh needs an interactive terminal: the Pi asks for a sudo password.

Run it from a normal terminal window:

    cd "$(dirname "$0")" && ./deploy.sh

If you need it non-interactive, give the Pi user passwordless sudo for the
installer and re-run.
NOTTY
    exit 1
fi

PI="${1:-${PI_HOST:-pi@raspberrypi.local}}"
# Empty means the bridge auto-discovers the renderer over SSDP.
SOUNDBAR_IP="${2:-${SOUNDBAR_IP:-}}"
AIRPLAY_NAME="${AIRPLAY_NAME:-Samsung Soundbar N850}"
# Forwarded to the installer: AIRPLAY2=0 uses the Debian AirPlay 1 package,
# REBUILD=1 forces a source rebuild even if AirPlay 2 is already installed.
AIRPLAY2="${AIRPLAY2:-1}"
REBUILD="${REBUILD:-0}"
# These three are deliberately passed through EMPTY when unset, so the
# installer can carry forward whatever was configured last time. Defaulting
# them to 0 here would silently reset the Pi's config on every plain deploy.
#   SHAIRPORT_VERBOSITY=2  make shairport-sync explain why a session ends
#   BITPERFECT=1           pass AirPlay audio through untouched
#   MIN_VOLUME=15          raise the soundbar to a usable level on play
SHAIRPORT_VERBOSITY="${SHAIRPORT_VERBOSITY-}"
BITPERFECT="${BITPERFECT-}"
MIN_VOLUME="${MIN_VOLUME-}"
#   MAX_VOLUME=20          safety cap on soundbar volume
MAX_VOLUME="${MAX_VOLUME-}"
#   STATUS_BIND=127.0.0.1  keep the status API off the LAN
#   STATUS_TOKEN=<secret>  require a token for status/control requests
STATUS_BIND="${STATUS_BIND-}"
STATUS_TOKEN="${STATUS_TOKEN-}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_TMP=/tmp/airplay-soundbar-src

# Stamp the deployed revision so /status shows what is actually running.
# "-dirty" matters: it means the Pi has code that exists in no commit.
GIT_REV="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git -C "$SRC" diff --quiet HEAD 2>/dev/null; then
    GIT_REV="${GIT_REV}-dirty"
fi
BRIDGE_VERSION="${BRIDGE_VERSION:-$GIT_REV}"

printf '\n\033[1m==> Deploying %s to %s\033[0m\n' "$BRIDGE_VERSION" "$PI"
ssh "$PI" "rm -rf $REMOTE_TMP && mkdir -p $REMOTE_TMP"
rsync -av --delete \
    --exclude '__pycache__' \
    "$SRC/bridge/" "$PI:$REMOTE_TMP/"

printf '\n\033[1m==> Running installer on the Pi (sudo password required)\033[0m\n'
# Runtime settings are named by config.py, so adding one there is enough -
# this script does not need to know them. Build-time options stay explicit.
RUNTIME_ENV=""
while read -r var; do
    [[ -z "$var" ]] && continue
    RUNTIME_ENV+=" ${var}=$(printf '%q' "${!var-}")"
done < <(python3 "$SRC/bridge/config.py" --env-names)

ssh -t "$PI" "chmod +x $REMOTE_TMP/install.sh && sudo \
    AIRPLAY2='$AIRPLAY2' \
    REBUILD='$REBUILD' \
    SHAIRPORT_VERBOSITY='$SHAIRPORT_VERBOSITY' \
    BITPERFECT='$BITPERFECT' \
   ${RUNTIME_ENV} \
    $REMOTE_TMP/install.sh '$SOUNDBAR_IP' '$AIRPLAY_NAME'"

printf '\n\033[1m==> Done\033[0m\n'
echo "Follow the log with:  ssh $PI 'journalctl -u airplay-soundbar -f'"
