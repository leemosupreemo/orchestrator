from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import integrations as ig
from orchestrator import integrations_sync as sync
from orchestrator.web import server as ui

SAVED = {"jira": {"site": "team.atlassian.net", "email": "a@b.co", "token": "jt"},
         "trello": {"key": "k", "token": "tt"}, "sentry": {"org": "o", "token": "st"}, "figma": {"token": "ft"}}
LINKS = [{"provider": "jira", "ref": "APP-42"}, {"provider": "trello", "ref": "abc123XY"},
         {"provider": "sentry", "ref": "123"}, {"provider": "figma", "ref": "https://figma.com/design/Ab12/x"}]


def recorder(responses=None):
    calls = []

    def http_json(url, headers=None, secrets=None, method="GET", body=None):
        calls.append((method, url, body))
        for key, value in (responses or {}).items():
            if key in url and (not isinstance(key, tuple)):
                if isinstance(value, Exception):
                    raise value
                return value
        return {}
    http_json.calls = calls
    return http_json


def job(**over):
    return {"job_id": "j1", "title": "Fix lobby", "external_links": [dict(l) for l in LINKS], **over}


class WritebackTests(unittest.TestCase):
    def test_pr_opened_comments_everywhere_that_can_be_written_to_and_skips_figma(self):
        http = recorder()
        with patch.object(ig, "http_json", http):
            new = sync.writeback(job(), "pr_opened", {"integrations": SAVED}, pr_url="https://github.com/o/r/pull/7", pr_number=7)
        self.assertEqual([(e["provider"], e["ok"]) for e in new], [("jira", True), ("trello", True), ("sentry", True)])
        methods = [(m, u.split("?")[0]) for m, u, _ in http.calls]
        self.assertIn(("POST", "https://team.atlassian.net/rest/api/3/issue/APP-42/comment"), methods)
        self.assertIn(("POST", "https://api.trello.com/1/cards/abc123XY/actions/comments"), methods)
        self.assertIn(("POST", "https://sentry.io/api/0/issues/123/comments/"), methods)
        body = json.dumps(http.calls[0][2])
        self.assertIn("pull/7", body)
        self.assertFalse(any("figma" in u for _, u, _ in http.calls))

    def test_nothing_moves_on_merge_unless_the_user_turned_it_on(self):
        http = recorder()
        with patch.object(ig, "http_json", http):
            sync.writeback(job(), "merged", {"integrations": SAVED}, pr_number=7)
        self.assertFalse(any("/transitions" in u or m == "PUT" for m, u, _ in http.calls))
        self.assertEqual(sum(1 for m, _, _ in http.calls if m == "POST"), 3)  # just the comments

    def test_jira_moves_through_the_named_transition_when_enabled(self):
        http = recorder({"/transitions": {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}, {"id": "21", "name": "Start", "to": {"name": "In Progress"}}]}})
        settings = {"integrations": SAVED, "integration_options": {"jira": {"comment_merge": False, "move_on_merge": True, "target": "done"}}}
        with patch.object(ig, "http_json", http):
            new = sync.writeback(job(external_links=[LINKS[0]]), "merged", settings, pr_number=7)
        self.assertEqual(new[0]["message"], "Moved APP-42 to Done")
        post = [c for c in http.calls if c[0] == "POST"]
        self.assertEqual(post[0][2], {"transition": {"id": "31"}})

    def test_a_missing_transition_is_reported_not_raised(self):
        http = recorder({"/transitions": {"transitions": [{"id": "1", "name": "Start", "to": {"name": "In Progress"}}]}})
        settings = {"integrations": SAVED, "integration_options": {"jira": {"comment_merge": False, "move_on_merge": True}}}
        with patch.object(ig, "http_json", http):
            new = sync.writeback(job(external_links=[LINKS[0]]), "merged", settings, pr_number=7)
        self.assertFalse(new[0]["ok"])
        self.assertIn("available: Start", new[0]["message"])

    def test_trello_moves_the_card_to_the_named_list_and_sentry_resolves(self):
        http = recorder({"/boards/B1/lists": [{"id": "L1", "name": "Doing"}, {"id": "L2", "name": "Done"}], "/cards/abc123XY": {"idBoard": "B1"}})
        settings = {"integrations": SAVED, "integration_options": {
            "trello": {"comment_merge": False, "move_on_merge": True}, "sentry": {"comment_merge": False, "move_on_merge": True},
            "jira": {"comment_merge": False}}}
        with patch.object(ig, "http_json", http):
            new = sync.writeback(job(external_links=LINKS[1:3]), "merged", settings, pr_number=7)
        self.assertEqual([e["message"] for e in new], ["Moved the card to Done", "Resolved the Sentry issue"])
        put = [(u.split("?")[0], u) for m, u, b in http.calls if m == "PUT"]
        self.assertTrue(any("idList=L2" in full for _, full in put))
        self.assertTrue(any(b == {"status": "resolved"} for m, u, b in http.calls if m == "PUT"))

    def test_the_same_event_is_only_sent_once(self):
        j = job(external_links=[LINKS[0]])
        http = recorder()
        with patch.object(ig, "http_json", http):
            sync.writeback(j, "pr_opened", {"integrations": SAVED}, pr_url="https://x/pull/7", pr_number=7)
            again = sync.writeback(j, "pr_opened", {"integrations": SAVED}, pr_url="https://x/pull/7", pr_number=7)
            other_pr = sync.writeback(j, "pr_opened", {"integrations": SAVED}, pr_url="https://x/pull/8", pr_number=8)
        self.assertEqual(again, [])
        self.assertEqual(len(other_pr), 1)
        self.assertEqual(len(http.calls), 2)

    def test_a_failing_service_never_raises_and_is_retried_next_time(self):
        j = job(external_links=[LINKS[0]])
        with patch.object(ig, "http_json", recorder({"/comment": ig.IntegrationError("The credentials were rejected.")})):
            new = sync.writeback(j, "pr_opened", {"integrations": SAVED}, pr_url="u", pr_number=1)
        self.assertEqual(new[0]["ok"], False)
        with patch.object(ig, "http_json", recorder()):
            retry = sync.writeback(j, "pr_opened", {"integrations": SAVED}, pr_url="u", pr_number=1)
        self.assertTrue(retry[0]["ok"])

    def test_comment_options_can_switch_everything_off(self):
        off = {"comment_pr": False, "comment_merge": False, "move_on_merge": False}
        settings = {"integrations": SAVED, "integration_options": {k: off for k in ("jira", "trello", "sentry")}}
        with patch.object(ig, "http_json", recorder()) as _:
            self.assertEqual(sync.writeback(job(), "pr_opened", settings, pr_url="u"), [])

    def test_unlinked_or_disconnected_jobs_do_nothing(self):
        self.assertEqual(sync.writeback({"job_id": "j"}, "merged", {"integrations": SAVED}), [])
        self.assertEqual(sync.writeback(job(), "merged", {"integrations": {}}), [])

    def test_script_entry_point_persists_the_log_without_clobbering_the_job(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "settings.json").write_text(json.dumps({"integrations": SAVED}))
            j = job(external_links=[LINKS[0]], status="working")
            path = d / "j1.json"
            path.write_text(json.dumps({**j, "extra": "kept"}))
            with patch.object(ig, "http_json", recorder()):
                sync.notify_job_event(j, "pr_opened", d / "settings.json", path, pr_url="u", pr_number=1)
            on_disk = json.loads(path.read_text())
            self.assertEqual(on_disk["extra"], "kept")
            self.assertEqual(len(on_disk["integration_log"]), 1)
            self.assertEqual(len(j["integration_log"]), 1)  # caller's dict keeps it for its next write


class ServerOptionTests(unittest.TestCase):
    def test_options_are_validated_and_only_for_writable_apps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".orchestrator" / "config").mkdir(parents=True)
            saved = ui.save_integration_options(root, "jira", {"move_on_merge": True, "target": " Done ", "bogus": 1, "comment_pr": "yes"})
            self.assertEqual(saved, {"comment_pr": True, "comment_merge": True, "move_on_merge": True, "target": "Done"})
            with self.assertRaises(ui.UIError):
                ui.save_integration_options(root, "figma", {})
            with self.assertRaises(ui.UIError):
                ui.save_integration_options(root, "jira", ["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
