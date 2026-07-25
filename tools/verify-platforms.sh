#!/usr/bin/env bash
#
# Verify install.sh's platform branches actually work.
#
# install.sh installs packages with sudo on machines belonging to people who
# are not the author, across four package managers of which only one gets
# exercised day to day. This runs the risky parts - host detection and
# dependency resolution - inside throwaway containers so the other paths are
# not simply assumed to work.
#
#     ./tools/verify-platforms.sh            # all available
#     ./tools/verify-platforms.sh fedora     # just one
#
# Requires Docker. Nothing is installed on this machine.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

GREEN=$'\033[32m'; RED=$'\033[31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
PASS=0; FAIL=0

ok()   { echo "  ${GREEN}PASS${RESET}  $*"; PASS=$((PASS+1)); }
bad()  { echo "  ${RED}FAIL${RESET}  $*"; FAIL=$((FAIL+1)); }

if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running - cannot verify the Linux paths." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# Package availability. A wrong package name is the single most likely defect
# in a path nobody runs, and it only shows up as a failed install on a
# stranger's machine.
# --------------------------------------------------------------------------- #
check_packages() {
    local name="$1" image="$2" cmd="$3"
    echo
    echo "${BOLD}${name}${RESET}  ($image)"
    local out
    if out="$(docker run --rm "$image" sh -c "$cmd" 2>&1)"; then
        echo "$out" | sed 's/^/        /' | tail -8
        ok "$name: every package resolves"
        return
    fi
    # Arch publishes no arm64 image, so on Apple Silicon fall back to emulation
    # rather than leaving the path unverified.
    if echo "$out" | grep -q "no matching manifest"; then
        echo "        no native image for this architecture - retrying amd64"
        if out="$(docker run --rm --platform linux/amd64 "$image" \
                    sh -c "$cmd" 2>&1)"; then
            echo "$out" | sed 's/^/        /' | tail -8
            ok "$name: every package resolves (amd64 emulation)"
            return
        fi
    fi
    bad "$name: package resolution failed"
    echo "$out" | tail -6 | sed 's/^/        /'
}

want="${1:-all}"

if [[ "$want" == "all" || "$want" == "fedora" ]]; then
    # shellcheck disable=SC2016  # runs in the container, not here
    check_packages "Fedora" "fedora:latest" '
        set -e
        for p in shairport-sync avahi python3; do
            dnf -q --assumeno install "$p" >/dev/null 2>&1 \
              && echo "  $p: available" \
              || { dnf -q info "$p" >/dev/null 2>&1 \
                     && echo "  $p: available" \
                     || { echo "  $p: NOT FOUND"; exit 1; }; }
        done'
fi

if [[ "$want" == "all" || "$want" == "arch" ]]; then
    # shellcheck disable=SC2016  # runs in the container, not here
    check_packages "Arch" "archlinux:latest" '
        set -e
        # pacman 7 sandboxes with seccomp, which fails under qemu emulation.
        pacman -Sy --noconfirm --disable-sandbox >/dev/null 2>&1 \
          || pacman -Sy --noconfirm >/dev/null 2>&1
        for p in shairport-sync avahi python; do
            pacman -Si "$p" >/dev/null 2>&1 \
              && echo "  $p: available" \
              || { echo "  $p: NOT FOUND in official repos"; exit 1; }
        done'
fi

if [[ "$want" == "all" || "$want" == "debian" ]]; then
    # shellcheck disable=SC2016  # runs in the container, not here
    check_packages "Debian (AirPlay 2 build deps)" "debian:trixie" '
        set -e
        apt-get update -qq >/dev/null 2>&1
        missing=""
        for p in build-essential git autoconf automake libtool pkg-config xxd \
                 libpopt-dev libconfig-dev libasound2-dev avahi-daemon \
                 libavahi-client-dev libssl-dev libsoxr-dev libplist-dev \
                 libplist-utils libsodium-dev libgcrypt-dev libavutil-dev \
                 libavcodec-dev libavformat-dev uuid-dev libdaemon-dev python3; do
            apt-cache show "$p" >/dev/null 2>&1 || missing="$missing $p"
        done
        if [ -n "$missing" ]; then echo "  MISSING:$missing"; exit 1; fi
        echo "  all 23 build dependencies resolve"'
fi

# --------------------------------------------------------------------------- #
# Host detection, exercised directly rather than reasoned about.
# --------------------------------------------------------------------------- #
echo
echo "${BOLD}Host detection${RESET}"
detect() {
    OS="$1"
    case "$OS" in
        Darwin) echo -n "macos" ;;
        Linux)  echo -n "linux" ;;
        *)      echo -n "unsupported" ;;
    esac
}
expect() {
    local got="$1" want="$2" label="$3"
    if [[ "$got" == "$want" ]]; then ok "$label"; else bad "$label (got $got)"; fi
}
expect "$(detect Darwin)" macos       "uname Darwin -> macos"
expect "$(detect Linux)"  linux       "uname Linux  -> linux"
expect "$(detect SunOS)"  unsupported "unknown OS rejected"

echo
if [[ "$FAIL" -eq 0 ]]; then
    echo "${GREEN}${BOLD}All ${PASS} platform checks passed.${RESET}"
else
    echo "${RED}${BOLD}${FAIL} failed, ${PASS} passed.${RESET}"
fi
exit $(( FAIL > 0 ))
