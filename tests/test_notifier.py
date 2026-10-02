from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import notifier  # noqa: E402


def item(i, kind="job", **extra):
    base = {"id": f"{kind}:{i}", "kind": kind, "title": f"T{i}", "label": "Question for you", "reason": "Needs an answer"}
    base[f"{kind}_id"] = i
    return {**base, **extra}


class TrackerTests(unittest.TestCase):
    def test_first_snapshot_only_seeds(self):
        t = notifier.Tracker()
        self.assertEqual(t.update([item("a")], []), [])
        self.assertEqual(t.update([item("a")], []), [])

    def test_new_inbox_items_notify_once_with_a_link_path(self):
        t = notifier.Tracker()
        t.update([item("a")], [])
        events = t.update([item("a"), item("b"), item("r1", "run")], [])
        self.assertEqual([(e["kind"], e["path"]) for e in events], [("needs-you", "#/jobs/b"), ("needs-you", "#/runs/r1")])
        self.assertEqual(events[0]["body"], "Tb: Needs an answer")
        self.assertEqual(t.update([item("a"), item("b")], []), [])

    def test_an_item_that_leaves_and_returns_notifies_again(self):
        t = notifier.Tracker()
        t.update([item("a")], [])
        t.update([], [])
        self.assertEqual(len(t.update([item("a")], [])), 1)

    def test_finished_run_notifies_and_failed_run_only_when_not_on_a_job(self):
        t = notifier.Tracker()
        running = [{"id": "r1", "running": True, "title": "Build"}, {"id": "r2", "running": True, "title": "Fix", "job": "j"},
                   {"id": "r3", "running": True, "title": "Check"}]
        t.update([], running)
        ended = [{"id": "r1", "running": False, "exit_code": 0, "title": "Build", "job": "j1"},
                 {"id": "r2", "running": False, "exit_code": 1, "title": "Fix", "job": "j"},
                 {"id": "r3", "running": False, "exit_code": 2, "title": "Check"}]
        events = t.update([], ended)
        self.assertEqual([(e["kind"], e["path"]) for e in events], [("done", "#/jobs/j1"), ("problem", "#/runs/r3")])
        self.assertEqual(t.update([], ended), [])

    def test_runs_already_finished_at_startup_are_silent(self):
        t = notifier.Tracker()
        self.assertEqual(t.update([], [{"id": "r1", "running": False, "exit_code": 0}]), [])


class WebhookTests(unittest.TestCase):
    def test_only_https_urls_are_accepted(self):
        self.assertTrue(notifier.valid_webhook("https://hooks.slack.com/services/T/B/x"))
        for bad in ("", "http://hooks.slack.com/x", "ftp://x/y", "https://", "https://user:pw@host/x", "not a url"):
            self.assertFalse(notifier.valid_webhook(bad), bad)

    def test_payload_has_title_project_body_and_link(self):
        body = notifier.payload({"title": "Failed", "body": "Run fix", "path": "#/jobs/j1"}, "Demo", "https://app.example/")
        self.assertEqual(body["text"], "*Failed* · Demo\nRun fix <https://app.example/#/jobs/j1|Open>")
        hosted = notifier.payload({"title": "t", "body": "b", "path": "#/jobs/j"}, "P", "https://app.web.app/?backend=https://t.example")
        self.assertTrue(hosted["text"].endswith("<https://app.web.app/?backend=https://t.example#/jobs/j|Open>"))
        self.assertNotIn("<", notifier.payload({"title": "t", "body": "b", "path": "#/x"}, "P")["text"])

    def test_post_sends_json_and_reports_failures(self):
        sent = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout):
            sent.append((req.full_url, json.loads(req.data), req.get_header("Content-type")))
            return Resp()

        notifier.post_webhook("https://hooks.example/x", {"text": "hi"}, opener)
        self.assertEqual(sent, [("https://hooks.example/x", {"text": "hi"}, "application/json")])

        def http_error(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, io.BytesIO(b""))

        def down(req, timeout):
            raise urllib.error.URLError("dns")

        with self.assertRaisesRegex(notifier.NotifyError, "404"):
            notifier.post_webhook("https://hooks.example/x", {}, http_error)
        with self.assertRaisesRegex(notifier.NotifyError, "reach"):
            notifier.post_webhook("https://hooks.example/x", {}, down)
        with self.assertRaises(notifier.NotifyError):
            notifier.post_webhook("http://insecure.example/x", {}, opener)


if __name__ == "__main__":
    unittest.main()
