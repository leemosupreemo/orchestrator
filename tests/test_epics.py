from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import epics, features as store
from orchestrator import integrations as ig


def proposal(*names, deps=None):
    deps = deps or {}
    return {"technique": "story_map", "technique_name": "Story map", "rationale": "Flows.", "open_questions": [], "detail": "moderate", "warnings": [],
            "features": [{"name": n, "summary": f"{n} summary", "requirements": [f"{n} works"], "stories": [], "slice": "MVP", "paths": [],
                          "depends_on": deps.get(n, []), "serves": "", "layer": 1 if deps.get(n) else 0} for n in names]}


class EpicStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)

    def job(self, job_id):
        return json.loads((self.runtime / "jobs" / f"{job_id}.json").read_text())

    def test_suggestions_are_stored_and_a_new_breakdown_replaces_only_undecided_ones(self):
        epics.add_suggestions(self.runtime, proposal("A", "B"))
        data = epics.load(self.runtime)
        self.assertEqual([(e["name"], e["status"]) for e in data["epics"]], [("A", "suggested"), ("B", "suggested")])
        self.assertEqual(data["breakdown"]["technique_name"], "Story map")
        a = data["epics"][0]
        epics.approve(self.runtime, [a["id"]])
        epics.decline(self.runtime, [data["epics"][1]["id"]])
        epics.add_suggestions(self.runtime, proposal("A", "B", "C"))  # A is approved, B declined: neither is suggested again
        self.assertEqual([(e["name"], e["status"]) for e in epics.load(self.runtime)["epics"]], [("A", "approved"), ("B", "declined"), ("C", "suggested")])
        with self.assertRaises(epics.EpicError):
            epics.add_suggestions(self.runtime, proposal("A"))  # nothing new

    def test_an_epic_the_project_already_has_is_listed_as_built_without_a_job_and_can_be_brought_back(self):
        built = proposal("Sign in", "Billing")
        built["features"][0].update(state="built", evidence=["app/auth/login.swift"])
        built["features"][1].update(state="partial", evidence=["app/billing/"])
        epics.add_suggestions(self.runtime, built)
        by_name = {e["name"]: e for e in epics.load(self.runtime)["epics"]}
        self.assertEqual((by_name["Sign in"]["status"], by_name["Sign in"]["job"]), ("built", ""))
        self.assertEqual(by_name["Billing"]["status"], "suggested")
        self.assertEqual(list((self.runtime / "jobs").glob("*.json")) if (self.runtime / "jobs").exists() else [], [])
        epics.add_suggestions(self.runtime, proposal("Sign in", "Billing", "Chat"))  # a second breakdown doesn't propose the built one again
        self.assertEqual([e["name"] for e in epics.load(self.runtime)["epics"] if e["status"] == "suggested"], ["Billing", "Chat"])
        epics.restore(self.runtime, [by_name["Sign in"]["id"]])
        self.assertEqual(epics.get(self.runtime, by_name["Sign in"]["id"])["status"], "suggested")

    def test_building_on_a_built_epic_does_not_make_a_job_for_it_and_a_partial_epic_tells_the_job_what_exists(self):
        p = proposal("Base", "Top", deps={"Top": ["Base"]})
        p["features"][0].update(state="built", evidence=["app/base/"])
        p["features"][1].update(state="partial", evidence=["app/top/part.swift"])
        epics.add_suggestions(self.runtime, p)
        top = next(e for e in epics.load(self.runtime)["epics"] if e["name"] == "Top")
        approved = epics.approve(self.runtime, [top["id"]])
        self.assertEqual([e["name"] for e in approved], ["Top"])
        brief = self.job(epics.get(self.runtime, top["id"])["job"])["description"]
        self.assertIn("part of it", brief)
        self.assertIn("- app/top/part.swift", brief)

    def test_approving_makes_a_feature_and_a_waiting_job_labelled_epic(self):
        epics.add_suggestions(self.runtime, proposal("Play a round"))
        epic = epics.load(self.runtime)["epics"][0]
        epics.approve(self.runtime, [epic["id"]])
        epic = epics.get(self.runtime, epic["id"])
        self.assertEqual(epic["status"], "approved")
        self.assertEqual([f["id"] for f in store.load(self.runtime)], [epic["feature"]])
        job = self.job(epic["job"])
        self.assertEqual((job["status"], job["type"], job["epic"], job["feature"]), ("backlog", "epic", epic["id"], epic["feature"]))
        self.assertIn("Play a round works", job["description"])  # shown as the job's brief
        with self.assertRaises(epics.EpicError):
            epics.approve(self.runtime, [epic["id"]])  # already approved

    def test_approving_brings_along_what_the_epic_builds_on(self):
        epics.add_suggestions(self.runtime, proposal("Base", "Top", deps={"Top": ["Base"]}))
        top = next(e for e in epics.load(self.runtime)["epics"] if e["name"] == "Top")
        approved = epics.approve(self.runtime, [top["id"]])
        self.assertEqual({e["name"] for e in approved}, {"Base", "Top"})
        by_name = {f["name"]: f for f in store.load(self.runtime)}
        self.assertEqual(by_name["Top"]["depends_on"], [by_name["Base"]["id"]])

    def test_a_name_that_is_already_a_feature_cannot_be_approved_twice(self):
        store.create(self.runtime, "Play a round")
        epics.add_suggestions(self.runtime, proposal("Brand new"))
        epics.load(self.runtime)  # a suggestion made before the clashing feature existed
        data = epics.load(self.runtime)
        data["epics"][0]["name"] = "Play a round"
        epics.save(self.runtime, data)
        with self.assertRaises(epics.EpicError) as caught:
            epics.approve(self.runtime, [data["epics"][0]["id"]])
        self.assertIn("already exists", str(caught.exception))

    def test_planning_an_epic_replaces_its_waiting_job_and_keeps_the_jira_link(self):
        epics.add_suggestions(self.runtime, proposal("Play a round"))
        epic = epics.load(self.runtime)["epics"][0]
        epics.approve(self.runtime, [epic["id"]])
        epics.link_jira(self.runtime, epic["id"], "APP-7", "https://t.atlassian.net/browse/APP-7")
        waiting = epics.get(self.runtime, epic["id"])["job"]
        self.assertEqual(self.job(waiting)["external_links"][0]["ref"], "APP-7")
        planned = self.runtime / "jobs" / "20261018-120000-feature-9.json"
        planned.write_text(json.dumps({"job_id": "20261018-120000-feature-9", "status": "planned", "epic": epic["id"]}))
        epics.attach_job(self.runtime, epic["id"], "20261018-120000-feature-9")
        self.assertFalse((self.runtime / "jobs" / f"{waiting}.json").exists())
        self.assertEqual(epics.get(self.runtime, epic["id"])["job"], "20261018-120000-feature-9")
        self.assertEqual(self.job("20261018-120000-feature-9")["external_links"][0]["ref"], "APP-7")  # its PR and merge reach Jira

    def test_a_job_that_is_already_underway_is_never_removed(self):
        epics.add_suggestions(self.runtime, proposal("Play a round"))
        epic = epics.load(self.runtime)["epics"][0]
        epics.approve(self.runtime, [epic["id"]])
        underway = epics.get(self.runtime, epic["id"])["job"]
        path = self.runtime / "jobs" / f"{underway}.json"
        path.write_text(json.dumps({**self.job(underway), "status": "executing"}))
        epics.attach_job(self.runtime, epic["id"], "some-other-job")
        self.assertTrue(path.exists())

    def test_importing_jira_epics_makes_approved_epics_and_skips_ones_already_here(self):
        issues = [{"key": "APP-1", "summary": "Lobby", "description": "Friends meet here.", "url": "https://t.atlassian.net/browse/APP-1"},
                  {"key": "bad key", "summary": "Nope"}]
        made = epics.import_issues(self.runtime, issues)
        self.assertEqual([(e["name"], e["status"], e["source"], e["jira"]["key"]) for e in made], [("Lobby", "approved", "jira", "APP-1")])
        self.assertEqual(store.load(self.runtime)[0]["summary"], "Friends meet here.")
        self.assertEqual(self.job(made[0]["job"])["external_links"][0]["ref"], "APP-1")
        with self.assertRaises(epics.EpicError):
            epics.import_issues(self.runtime, issues)


class JiraEpicTests(unittest.TestCase):
    CREDS = {"site": "team.atlassian.net", "email": "me@x.co", "token": "tok", "project": "app"}

    def fake(self, routes):
        calls = []

        def http_json(url, headers=None, secrets=None, method="GET", body=None):
            calls.append((method, url, body))
            for key, value in routes.items():
                if key in url:
                    return value
            raise AssertionError(f"unexpected request: {method} {url}")
        http_json.calls = calls
        return http_json

    def test_creating_an_epic_posts_an_epic_issue_in_the_project(self):
        http = self.fake({"/rest/api/3/issue": {"key": "APP-9"}})
        with patch.object(ig, "http_json", http):
            made = ig.Jira(self.CREDS).create_epic("Lobby", "Friends meet.\n\nSecond paragraph.")
        method, url, body = http.calls[0]
        self.assertEqual((method, url), ("POST", "https://team.atlassian.net/rest/api/3/issue"))
        self.assertEqual(body["fields"]["project"], {"key": "APP"})
        self.assertEqual(body["fields"]["issuetype"], {"name": "Epic"})
        self.assertEqual(len(body["fields"]["description"]["content"]), 2)
        self.assertEqual((made.ref, made.url), ("APP-9", "https://team.atlassian.net/browse/APP-9"))

    def test_creating_needs_a_valid_project_key(self):
        for creds in ({**self.CREDS, "project": ""}, {**self.CREDS, "project": 'A" OR 1=1'}):
            with self.assertRaises(ig.IntegrationError):
                ig.Jira(creds).create_epic("Lobby")

    def test_listing_epics_asks_for_open_epics_of_the_project_and_returns_their_text(self):
        http = self.fake({"/rest/api/3/search/jql": {"issues": [{"key": "APP-1", "fields": {"summary": "Lobby", "status": {"name": "To Do"},
            "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Friends meet."}]}]}}}]}})
        with patch.object(ig, "http_json", http):
            found = ig.Jira(self.CREDS).list_epics()
        self.assertIn("issuetype+%3D+Epic", http.calls[0][1])
        self.assertIn("project+%3D+%22APP%22", http.calls[0][1])
        self.assertEqual((found[0]["key"], found[0]["summary"], found[0]["description"], found[0]["url"]),
                         ("APP-1", "Lobby", "Friends meet.", "https://team.atlassian.net/browse/APP-1"))


if __name__ == "__main__":
    unittest.main()
