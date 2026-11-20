"""Connecting an AI tool is a dialog, not a job or a run to look after.

The Connect dialog started a run for the tool's own sign-in. Closing the dialog only stopped polling, so `claude auth login`
sat waiting for a pasted code for nearly an hour, and the run showed up as "running" in the app's lists as if it were work."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


class AbandonedSignInTests(unittest.TestCase):
    """The run itself, not only the dialog: a sign-in nobody finishes ends on its own."""

    def run_session(self, action, script, limit=2, wait=20):
        from orchestrator.web import server
        with tempfile.TemporaryDirectory() as directory, patch.dict(server.ABANDON_IDLE_SECONDS, {"ai_connect": limit}):
            session = server.PtySession("abandon-test", action, "Test", [sys.executable, "-c", script], Path(directory), dict(os.environ))
            deadline = time.time() + wait
            while session.running and time.time() < deadline:
                time.sleep(0.1)
            return session, session.read(0, 0.1)[1].decode("utf-8", "replace")

    def test_a_sign_in_nobody_answers_stops_itself_and_its_process_is_gone(self):
        session, text = self.run_session("ai_connect", "import time; print('Paste code here', flush=True); time.sleep(120)")
        self.assertFalse(session.running)
        self.assertTrue(session.stopped)
        self.assertNotEqual(session.exit_code, 0)
        self.assertIn("[Stopped: nobody answered", text)
        self.assertIsNotNone(session._proc.poll())

    def test_a_sign_in_that_keeps_talking_is_left_alone(self):
        script = "import time\nfor i in range(4):\n  print('waiting', flush=True); time.sleep(1)\n"
        session, text = self.run_session("ai_connect", script, limit=3)
        self.assertEqual(session.exit_code, 0)
        self.assertFalse(session.stopped)
        self.assertNotIn("[Stopped", text)

    def test_work_runs_are_never_stopped_for_silence(self):
        session, text = self.run_session("coverage", "import time; print('start', flush=True); time.sleep(4)", limit=1)
        self.assertEqual(session.exit_code, 0)
        self.assertFalse(session.stopped)

    def test_only_waiting_runs_have_a_limit(self):
        from orchestrator.web import server
        self.assertEqual(set(server.ABANDON_IDLE_SECONDS), {"ai_connect"})
        self.assertGreaterEqual(server.ABANDON_IDLE_SECONDS["ai_connect"], 10 * 60)  # long enough to sign in on a phone


if __name__ == "__main__":
    unittest.main()
