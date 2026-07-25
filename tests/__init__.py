"""Test suite for the AirPlay -> Samsung soundbar bridge.

Standard-library unittest only, so it runs on the Raspberry Pi with no extra
packages installed.

    python3 -m unittest discover -s tests -t .
    ./run-tests.sh
"""

import logging

# The bridge logs at INFO by design; during tests that buries real failures.
# Errors we deliberately provoke are asserted on state, not on log output.
logging.disable(logging.CRITICAL)
