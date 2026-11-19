"""Connecting an AI tool is a dialog, not a job or a run to look after.

The Connect dialog started a run for the tool's own sign-in. Closing the dialog only stopped polling, so `claude auth login`
sat waiting for a pasted code for nearly an hour, and the run showed up as "running" in the app's lists as if it were work."""
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
APP = (STATIC / "app.js").read_text()
HOME = (STATIC / "home-status.js").read_text()
CONNECT = (STATIC / "tool-connect.js").read_text()


class ConnectRunTests(unittest.TestCase):
    def test_a_connect_run_is_not_listed_or_counted_as_running(self):
        self.assertIn('r.action === "dev_preview" || r.action === "ai_connect"', APP)
        self.assertNotIn("!isPreviewRun(r)", APP)  # every list uses the wider rule; the Preview card alone wants the live preview
        self.assertIn('r.action !== "ai_connect"', HOME)

    def test_closing_the_dialog_before_it_finishes_stops_the_sign_in(self):
        self.assertIn("runs/${encodeURIComponent(runId)}/stop", CONNECT)
        self.assertIn("{closed = true; stopRun(); onClose();}", CONNECT)
        self.assertIn("if (closed) { stopRun(); return; }", CONNECT)  # closed before the run had an id
        self.assertIn("runId && !finished", CONNECT)  # a finished sign-in is left alone


if __name__ == "__main__":
    unittest.main()
