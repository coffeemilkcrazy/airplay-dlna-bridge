"""Execute the web panel's JavaScript and assert on its behaviour.

The panel carries the most intricate logic in the project - the slider settle
window, request coalescing, the polling lifecycle - and it was previously
covered only by checking that certain strings appeared in the HTML. That would
not have caught the slider snapping backwards, and would not catch it again.

Here the <script> block is extracted from webui.py and executed under Node
against a stub DOM, so the assertions run against the shipped code rather than
a transcription of it. Skipped when Node is unavailable; the rest of the suite
stays standard-library only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))

from webui import PAGE  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "webui_harness.js"
NODE = shutil.which("node")


def extract_script(page: str) -> str:
    m = re.search(r"<script>(.*?)</script>", page, re.S)
    if not m:
        raise AssertionError("no <script> block found in the panel")
    return m.group(1)


@unittest.skipIf(NODE is None, "node not installed")
class TestPanelBehaviour(unittest.TestCase):
    """One Node run exercises every case; its output is asserted line by line
    so a failure names the specific behaviour rather than just 'JS failed'."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            script_path = Path(d) / "panel.js"
            script_path.write_text(extract_script(PAGE))
            proc = subprocess.run(
                [NODE, str(HARNESS), str(script_path)],
                capture_output=True, text=True, timeout=60)
        cls.out = proc.stdout
        cls.err = proc.stderr
        cls.code = proc.returncode
        cls.results = {}
        for line in cls.out.splitlines():
            if line.startswith(("PASS ", "FAIL ")):
                status, label = line.split(" ", 1)
                # labels may carry a "(got ...)" suffix; key on the prefix
                cls.results[label.split(" (got ")[0]] = status == "PASS"

    def assertBehaviour(self, label):
        self.assertIn(label, self.results,
                      f"harness did not report {label!r}.\n"
                      f"stdout:\n{self.out}\nstderr:\n{self.err}")
        self.assertTrue(self.results[label],
                        f"{label} failed.\nstdout:\n{self.out}")

    # -- the script must at least run -------------------------------------- #
    def test_script_executes_without_error(self):
        self.assertEqual(self.code, 0,
                         f"harness exited {self.code}\n"
                         f"stdout:\n{self.out}\nstderr:\n{self.err}")

    # -- rendering ---------------------------------------------------------- #
    def test_renders_track(self):
        self.assertBehaviour("title rendered")

    def test_joins_artist_and_album(self):
        self.assertBehaviour("artist and album joined")

    def test_release_version_prefixed(self):
        self.assertBehaviour("version prefixed with v")

    def test_build_shows_revision(self):
        self.assertBehaviour("build shows revision")

    def test_humanises_byte_counts(self):
        self.assertBehaviour("bytes humanised")

    def test_whitespace_only_metadata_ignored(self):
        self.assertBehaviour("whitespace-only title ignored")

    # -- equaliser ---------------------------------------------------------- #
    def test_equaliser_follows_session(self):
        self.assertBehaviour("equaliser on while playing")
        self.assertBehaviour("equaliser off when idle")

    # -- transport ---------------------------------------------------------- #
    def test_transport_gated_on_sender(self):
        self.assertBehaviour("transport enabled when available")
        self.assertBehaviour("transport disabled without sender")

    # -- volume ------------------------------------------------------------- #
    def test_volume_clamped_to_server_cap_before_sending(self):
        self.assertBehaviour("volume POSTed")
        self.assertBehaviour("request clamped before sending")

    def test_server_applied_value_is_authoritative(self):
        """A capped request must settle on what the bridge actually set."""
        self.assertBehaviour("capped value adopted from response")

    def test_requests_are_coalesced(self):
        """Dragging must not queue a backlog of stale values on a slow link."""
        self.assertBehaviour("only one volume request in flight")

    # -- polling lifecycle --------------------------------------------------- #
    def test_polling_stops_when_hidden(self):
        self.assertBehaviour("polling stops when hidden")

    def test_polling_resumes_when_visible(self):
        self.assertBehaviour("polling resumes when visible")

    # -- errors -------------------------------------------------------------- #
    def test_unauthorised_is_explained(self):
        self.assertBehaviour("401 explains a token is needed")


class TestExtraction(unittest.TestCase):
    """Guards the harness itself: if the script stops being extractable, the
    JS tests would silently stop testing anything."""

    def test_script_block_present_and_substantial(self):
        js = extract_script(PAGE)
        self.assertGreater(len(js.splitlines()), 100)
        self.assertIn("function render", js)
        self.assertIn("function poll", js)


if __name__ == "__main__":
    unittest.main()
