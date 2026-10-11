"""Tests for the feedback button, modal dialog, and backend submission endpoint."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.web import server as ui

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FEEDBACK_JS = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "feedback.js"
INDEX_HTML = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "index.html"
STYLE_CSS = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css"
APP_JS = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"


class FeedbackFrontendFileTests(unittest.TestCase):
    """Verify frontend code conforms to the spot-the-difference design and requirements."""

    def test_constants_and_email_delivery(self):
        js = FEEDBACK_JS.read_text(encoding="utf-8")
        self.assertIn("support@thejauntcompany.com", js)
        self.assertIn("[Orchestrator Feedback]", js)
        # Background submission without launching local mail clients
        self.assertNotIn("mailto:", js)
        # Avoid autofocus that causes jumpy viewport behavior
        self.assertNotIn("autoFocus", js)

    def test_modal_ui_copy_and_layout(self):
        js = FEEDBACK_JS.read_text(encoding="utf-8")
        # Centered title and left-aligned subtitle
        self.assertIn("How can we improve?", js)
        self.assertIn("Tell us what felt off or share any ideas.", js)
        self.assertIn("Write something above to send.", js)
        self.assertIn("Send Feedback", js)
        # Emoji satisfaction prompt choices
        self.assertIn("Enjoying Orchestrator?", js)
        self.assertIn("Could be better", js)
        self.assertIn("Enjoying it", js)
        self.assertIn("🙁", js)
        self.assertIn("😍", js)
        # Status / delivery views
        self.assertIn("Thank You!", js)
        self.assertIn("Couldn't Send", js)
        self.assertIn("Try Again", js)
        self.assertIn("Done", js)

    def test_index_html_integration(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="feedback-btn"', html)
        self.assertIn('id="feedback-dialog"', html)
        self.assertIn('id="i-feedback"', html)
        self.assertIn('src="feedback.js"', html)

    def test_style_css_integration(self):
        css = STYLE_CSS.read_text(encoding="utf-8")
        self.assertIn(".feedback-dialog", css)
        self.assertIn(".feedback-card", css)
        self.assertIn(".feedback-close-btn", css)
        self.assertIn(".feedback-title", css)
        self.assertIn(".feedback-textarea", css)
        self.assertIn(".feedback-submit-btn", css)
        self.assertIn(".feedback-btn-spinner", css)

    def test_app_js_integration(self):
        app = APP_JS.read_text(encoding="utf-8")
        self.assertIn("#feedback-btn", app)
        self.assertIn("Feedback.open", app)
        self.assertIn("Send feedback", app)


@unittest.skipUnless(shutil.which("node"), "node is needed to run frontend module tests")
class FeedbackNodeModuleTests(unittest.TestCase):
    """Test feedback.js logic under Node.js."""

    def run_js(self, body: str) -> dict:
        script = f"""
const Feedback = require(process.argv[1]);
{body}
"""
        res = subprocess.run(["node", "-e", script, str(FEEDBACK_JS)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_module_exports(self):
        out = self.run_js("""
console.log(JSON.stringify({
  email: Feedback.SUPPORT_EMAIL,
  prefix: Feedback.FEEDBACK_SUBJECT_PREFIX,
  hasSubmit: typeof Feedback.submitFeedback === 'function',
  hasOpen: typeof Feedback.open === 'function',
  hasClose: typeof Feedback.close === 'function'
}));
""")
        self.assertEqual(out["email"], "support@thejauntcompany.com")
        self.assertEqual(out["prefix"], "[Orchestrator Feedback]")
        self.assertTrue(out["hasSubmit"])
        self.assertTrue(out["hasOpen"])
        self.assertTrue(out["hasClose"])

    def test_submit_feedback_validation_and_reporting(self):
        out = self.run_js("""
(async () => {
  let emptyError = false;
  try {
    await Feedback.submitFeedback({ feedbackText: '   ' });
  } catch (err) {
    emptyError = true;
  }

  // Simulated client success
  const mockApi = async (path, opts) => ({ ok: true, delivered: true, id: '123' });
  const successReport = await Feedback.submitFeedback({
    userName: 'Tester',
    feedbackText: 'Great app!',
    apiClient: mockApi
  });

  // Simulated client queued
  const mockQueued = async (path, opts) => ({ ok: true, delivered: false, id: '456' });
  const queuedReport = await Feedback.submitFeedback({
    userName: 'Tester',
    feedbackText: 'Saved for later',
    apiClient: mockQueued
  });

  console.log(JSON.stringify({ emptyError, successReport, queuedReport }));
})();
""")
        self.assertTrue(out["emptyError"])
        self.assertTrue(out["successReport"]["success"])
        self.assertTrue(out["successReport"]["sent"])
        self.assertFalse(out["successReport"]["queued"])
        self.assertTrue(out["queuedReport"]["success"])
        self.assertFalse(out["queuedReport"]["sent"])
        self.assertTrue(out["queuedReport"]["queued"])


class FeedbackBackendTests(unittest.TestCase):
    """Test backend dispatch_feedback helper and persistence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir(parents=True)
        (self.root / ".orchestrator").mkdir(parents=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatch_feedback_requires_text(self):
        with self.assertRaises(ui.UIError) as ctx:
            ui.dispatch_feedback(self.root, {"feedbackText": "   "})
        self.assertIn("Feedback text is required", str(ctx.exception))

    def test_dispatch_feedback_writes_locally_and_tracks_analytics(self):
        with patch("orchestrator.analytics.track_event") as mock_track:
            res = ui.dispatch_feedback(self.root, {
                "userName": "Alice",
                "feedbackText": "Loved the prompt diffing",
                "platform": "macOS Web",
                "appVersion": "2.1.0"
            })
            self.assertTrue(res["ok"])
            self.assertTrue(res["success"])
            self.assertTrue(res["queued"] or res["delivered"])

            # Verify local persistence in .orchestrator/feedback/feedback.jsonl
            fb_log = self.root / ".orchestrator" / "feedback" / "feedback.jsonl"
            self.assertTrue(fb_log.is_file())
            lines = fb_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["userName"], "Alice")
            self.assertEqual(record["feedbackText"], "Loved the prompt diffing")
            self.assertEqual(record["targetEmail"], "support@thejauntcompany.com")

            # Verify analytics event
            mock_track.assert_called()
            event_name, event_props = mock_track.call_args[0][:2]
            self.assertEqual(event_name, "feedback_submitted")
            self.assertEqual(event_props["chars"], len("Loved the prompt diffing"))
            self.assertEqual(event_props["platform"], "macOS Web")

    def test_dispatch_feedback_resend_mocked_success(self):
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__.return_value = fake_resp

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_urlopen, \
             patch.dict(os.environ, {"RESEND_API_KEY": "re_test_dummy_key"}):
            res = ui.dispatch_feedback(self.root, {
                "userName": "Bob",
                "feedbackText": "Super fast response!"
            })
            self.assertTrue(res["delivered"])
            self.assertFalse(res["queued"])
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "https://api.resend.com/emails")
            body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(body["to"], ["support@thejauntcompany.com"])
            self.assertIn("[Orchestrator Feedback]", body["subject"])
            self.assertIn("Bob", body["subject"])


class ServerEndpointFeedbackTests(unittest.TestCase):
    """Test the HTTP API endpoint /api/feedback over live socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir(parents=True)
        (self.root / ".orchestrator").mkdir(parents=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo"}))

        import http.client
        import threading
        self.http_client = http.client
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")})
        self.env.start()
        self.server = ui.UIServer(("127.0.0.1", 0), self.root, token="test-token")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.sessions.stop_all()
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True):
        conn = self.http_client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = "Bearer test-token"
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            data = json.loads(raw) if raw else None
        except Exception:
            data = raw
        return res, data

    def test_feedback_static_file_served(self):
        res, data = self.request("GET", "/feedback.js", auth=False)
        self.assertEqual(res.status, 200)
        self.assertIn(b"support@thejauntcompany.com", data)

    def test_feedback_endpoint_refuses_without_ui_header(self):
        res, data = self.request("POST", "/api/feedback", body={"feedbackText": "Nice work"})
        self.assertEqual(res.status, 403)
        self.assertEqual(data["error"], "Missing UI headers")

    def test_feedback_endpoint_validates_empty_text(self):
        res, data = self.request(
            "POST",
            "/api/feedback",
            body={"feedbackText": "   "},
            headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"}
        )
        self.assertEqual(res.status, 400)
        self.assertIn("Feedback text is required", data["error"])

    def test_feedback_endpoint_accepts_valid_submission(self):
        res, data = self.request(
            "POST",
            "/api/feedback",
            body={"userName": "Charlie", "feedbackText": "Everything works seamlessly"},
            headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"}
        )
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["success"])
        self.assertTrue(data["queued"] or data["delivered"])

        # Check file was created
        log_file = self.root / ".orchestrator" / "feedback" / "feedback.jsonl"
        self.assertTrue(log_file.is_file())
        entry = json.loads(log_file.read_text().strip())
        self.assertEqual(entry["userName"], "Charlie")
        self.assertEqual(entry["feedbackText"], "Everything works seamlessly")


if __name__ == "__main__":
    unittest.main()

