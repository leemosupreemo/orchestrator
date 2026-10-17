from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import integrations as ig
from orchestrator.web import server as ui


def fake_http(routes):
    """http_json stand-in: first route whose key is in the URL wins."""
    calls = []

    def http_json(url, headers=None, secrets=None):
        calls.append((url, headers or {}))
        for key, value in routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected request: {url}")
    http_json.calls = calls
    return http_json


JIRA_ISSUE = {"key": "APP-42", "fields": {
    "summary": "Lobby empties on rejoin", "status": {"name": "To Do"}, "issuetype": {"name": "Bug"},
    "priority": {"name": "High"}, "labels": ["ios"],
    "description": {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Rejoining shows an empty seat."}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Background app"}]}]}]}]},
    "comment": {"comments": [{"author": {"displayName": "Sam"}, "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Repro on 17.2"}]}]}}]}}}


class ProviderTests(unittest.TestCase):
    def test_jira_lookup_turns_adf_into_markdown_and_uses_basic_auth(self):
        http = fake_http({"/rest/api/3/issue/APP-42": JIRA_ISSUE})
        with patch.object(ig, "http_json", http):
            ctx = ig.Jira({"site": "team.atlassian.net", "email": "me@x.co", "token": "tok"}).lookup("https://team.atlassian.net/browse/APP-42")
        self.assertIn("## Jira APP-42: Lobby empties on rejoin", ctx.markdown)
        self.assertIn("Rejoining shows an empty seat.", ctx.markdown)
        self.assertIn("- Background app", ctx.markdown)
        self.assertIn("Repro on 17.2", ctx.markdown)
        self.assertTrue(http.calls[0][1]["Authorization"].startswith("Basic "))
        self.assertEqual(ctx.item.url, "https://team.atlassian.net/browse/APP-42")

    def test_jira_rejects_bad_keys_and_non_https_sites(self):
        with self.assertRaises(ig.IntegrationError):
            ig.Jira({"site": "team.atlassian.net", "email": "a", "token": "t"}).lookup("not a ticket")
        with self.assertRaises(ig.IntegrationError):
            ig.Jira({"site": "http://team.atlassian.net", "email": "a", "token": "t"}).test()
        with self.assertRaises(ig.IntegrationError):
            ig.Jira({"site": "https://user:pw@team.atlassian.net", "email": "a", "token": "t"}).test()

    def test_jira_search_by_key_and_by_text(self):
        http = fake_http({"/rest/api/3/search/jql": {"issues": [{"key": "APP-1", "fields": {"summary": "S", "status": {"name": "Done"}}}]}})
        with patch.object(ig, "http_json", http):
            jira = ig.Jira({"site": "team.atlassian.net", "email": "a", "token": "t"})
            self.assertEqual(jira.search("app-1")[0].ref, "APP-1")
            jira.search('crash "quote')
        self.assertIn("key+%3D+%22APP-1%22", http.calls[0][0])
        self.assertNotIn('"quote', http.calls[1][0].replace("%22", ""))  # quotes in user text can't break out of the JQL string

    def test_trello_lookup_includes_checklists(self):
        http = fake_http({"/cards/abc123XY": {"name": "Add rematch", "desc": "Let players rematch.", "url": "https://trello.com/c/abc123XY/1",
                                              "shortLink": "abc123XY", "labels": [{"name": "mobile"}], "due": "2026-10-01",
                                              "checklists": [{"name": "Done when", "checkItems": [{"name": "Button", "state": "complete"}, {"name": "Sync", "state": "incomplete"}]}]}})
        with patch.object(ig, "http_json", http):
            ctx = ig.Trello({"key": "k", "token": "t"}).lookup("https://trello.com/c/abc123XY/1-add-rematch")
        self.assertIn("- [x] Button", ctx.markdown)
        self.assertIn("- [ ] Sync", ctx.markdown)
        with self.assertRaises(ig.IntegrationError):
            ig.Trello({"key": "k", "token": "t"}).lookup("../../etc")

    def test_sentry_lookup_lists_innermost_frame_first(self):
        event = {"entries": [{"type": "exception", "data": {"values": [{"type": "KeyError", "value": "'seat'", "stacktrace": {"frames": [
            {"filename": "outer.py", "lineno": 1, "function": "main", "inApp": True},
            {"filename": "lobby.py", "lineno": 88, "function": "rejoin", "inApp": True, "context_line": "seat = lobby['seat']"}]}}]}},
                                {"type": "breadcrumbs", "data": {"values": [{"timestamp": "t", "category": "ui", "message": "tap rejoin"}]}}],
                 "tags": [{"key": "os", "value": "iOS 17"}]}
        http = fake_http({"/events/latest/": event, "/api/0/issues/123/": {"shortId": "APP-9", "title": "KeyError: seat", "permalink": "https://sentry.io/x/123/", "level": "error", "count": "40"}})
        with patch.object(ig, "http_json", http):
            ctx = ig.Sentry({"org": "o", "token": "t"}).lookup("https://sentry.io/organizations/o/issues/123/?project=1")
        self.assertLess(ctx.markdown.index("lobby.py:88"), ctx.markdown.index("outer.py:1"))
        self.assertIn("seat = lobby['seat']", ctx.markdown)
        self.assertIn("tap rejoin", ctx.markdown)
        self.assertIn("os=iOS 17", ctx.markdown)

    def test_figma_parses_urls_and_summarises_the_frame(self):
        self.assertEqual(ig.Figma.parse("https://www.figma.com/design/AbC123/My-App?node-id=12-34&t=x"), ("AbC123", "12:34"))
        self.assertEqual(ig.Figma.parse("https://www.figma.com/file/AbC123/My-App"), ("AbC123", None))
        with self.assertRaises(ig.IntegrationError):
            ig.Figma.parse("https://example.com/not-figma")
        frame = {"name": "Home", "nodes": {"12:34": {"document": {"type": "FRAME", "name": "Home", "absoluteBoundingBox": {"width": 390, "height": 844},
                 "layoutMode": "VERTICAL", "itemSpacing": 16, "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
                 "children": [{"type": "TEXT", "name": "Title", "characters": "Welcome back", "fills": [{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}]}]}}}}
        http = fake_http({"/v1/images/": {"images": {"12:34": "https://img.example/x.png"}}, "/v1/files/AbC123/nodes": frame})
        with patch.object(ig, "http_json", http), patch.object(ig, "http_bytes", return_value=b"\x89PNG"):
            ctx = ig.Figma({"token": "t"}).lookup("https://www.figma.com/design/AbC123/My-App?node-id=12-34")
        self.assertIn("FRAME 'Home' · 390x844", ctx.markdown)
        self.assertIn("text='Welcome back'", ctx.markdown)
        self.assertIn("fill=#FFFFFF", ctx.markdown)
        self.assertEqual(ctx.image, b"\x89PNG")
        self.assertTrue(ctx.image_name.endswith(".png"))

    def test_connect_validates_live_and_blank_secrets_keep_the_saved_one(self):
        with patch.object(ig, "http_json", fake_http({"/rest/api/3/myself": {"displayName": "Me"}})):
            creds, who = ig.connect("jira", {"site": "team.atlassian.net", "email": "a@b.co", "token": "secret"})
            self.assertEqual(who, "Me")
            creds2, _ = ig.connect("jira", {"site": "team.atlassian.net", "email": "a@b.co", "token": ""}, creds)
            self.assertEqual(creds2["token"], "secret")
            with self.assertRaises(ig.IntegrationError):
                ig.connect("jira", {"site": "team.atlassian.net", "email": "a@b.co", "token": ""})
        with patch.object(ig, "http_json", fake_http({"/rest/api/3/myself": ig.IntegrationError("The credentials were rejected.")})):
            with self.assertRaises(ig.IntegrationError):
                ig.connect("jira", {"site": "team.atlassian.net", "email": "a@b.co", "token": "bad"})

    def test_catalog_never_contains_secret_values(self):
        saved = {"jira": {"site": "team.atlassian.net", "email": "a@b.co", "token": "SUPERSECRET"}, "figma": {"token": "FIGSECRET"}}
        text = json.dumps(ig.public_catalog(saved))
        self.assertNotIn("SUPERSECRET", text)
        self.assertNotIn("FIGSECRET", text)
        self.assertTrue([p for p in ig.public_catalog(saved) if p["id"] == "jira"][0]["connected"])


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestrator" / "jobs").mkdir(parents=True)
        (self.root / ".orchestrator" / "config").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def connect_jira(self):
        with patch.object(ig, "http_json", fake_http({"/myself": {"displayName": "Me"}})):
            ui.connect_integration(self.root, "jira", {"site": "team.atlassian.net", "email": "a@b.co", "token": "tok"})

    def test_credentials_are_stored_privately_and_can_be_removed(self):
        self.connect_jira()
        settings = self.root / ".orchestrator" / "config" / "settings.json"
        self.assertEqual(oct(settings.stat().st_mode)[-3:], "600")
        self.assertEqual(ui.saved_integrations(self.root)["jira"]["token"], "tok")
        ui.disconnect_integration(self.root, "jira")
        self.assertNotIn("jira", ui.saved_integrations(self.root))

    def test_new_job_spec_carries_the_ticket_and_records_the_link(self):
        self.connect_jira()
        params = {"type": "bug", "summary": "Fix lobby", "links": [{"provider": "jira", "ref": "APP-42"}]}
        with patch.object(ig, "http_json", fake_http({"/issue/APP-42": JIRA_ISSUE})):
            argv = ui.build_new_job(params, self.root)
        spec = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertIn("# Context from connected apps", spec)
        self.assertIn("Rejoining shows an empty seat.", spec)
        self.assertEqual(params["_linked"][0]["ref"], "APP-42")

    def test_link_to_an_app_that_isnt_connected_is_refused(self):
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "bug", "summary": "x", "links": [{"provider": "sentry", "ref": "1"}]}, self.root)

    def test_attach_context_to_existing_job(self):
        self.connect_jira()
        job = self.root / ".orchestrator" / "jobs" / "job-1.json"
        job.write_text(json.dumps({"job_id": "job-1", "title": "T"}))
        with patch.object(ig, "http_json", fake_http({"/issue/APP-42": JIRA_ISSUE})):
            links = ui.attach_links_to_job(self.root, "job-1", [{"provider": "jira", "ref": "APP-42"}])
            ui.attach_links_to_job(self.root, "job-1", [{"provider": "jira", "ref": "APP-42"}])  # re-attach doesn't duplicate the link
        data = json.loads(job.read_text())
        self.assertEqual(len(data["external_links"]), 1)
        art = data["reference_artifacts"][0]
        self.assertEqual(art["type"], "text_reference")
        self.assertIn("Lobby empties on rejoin", (self.root / art["path"]).read_text())


if __name__ == "__main__":
    unittest.main()
