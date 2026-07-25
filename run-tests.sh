#!/usr/bin/env bash
#
# Run the full test suite. Standard library only - no pytest, no pip install,
# so this works unchanged on the Raspberry Pi.
#
#     ./run-tests.sh              # everything
#     ./run-tests.sh test_bridge  # one module
#
# Note: none of these tests need the real soundbar, the Pi, or a browser.
# UPnP calls run against a fake renderer, the metadata reader against a
# temporary FIFO, and DACP against captured avahi-browse output.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ $# -gt 0 ]]; then
    exec python3 -m unittest "tests.$1" -v
fi

exec python3 -m unittest discover -s tests -t . "$@"
