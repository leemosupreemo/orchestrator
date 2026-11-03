from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import integrations
from orchestrator.web import server

DEFECT = {"summary": "CRLF is not a line break", "test": "testCRLF", "expected": "2 items", "actual": "1 item",
          "file": "App/Extensions.swift", "code": "func testCRLF() {}"}


class FakeJira:
    created: list = []

    def __init__(self, config):
        pass

    def create_child_bug(self, parent_ref, summary, description=""):
        FakeJira.created.append((parent_ref, summary, description))
        return integrations.Item("jira", "ABC-124", summary, "https://x.atlassian.net/browse/ABC-124", "To Do")


class DefectJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.jobs = server.jobs_dir(self.root)
        self.jobs.mkdir(parents=True)
        self.parent = {"job_id": "p1", "title": "Audit tests", "feature": "parser", "production_defects": [dict(DEFECT)],
                       "external_links": [{"provider": "jira", "ref": "ABC-123", "title": "Parser", "url": "u"}]}
        self.child = {"job_id": "c1", "title": "CRLF is not a line break", "raw_input": "the bug", "external_links": []}
        server.write_json_file(self.jobs / "p1.json", self.parent)
        server.write_json_file(self.jobs / "c1.json", self.child)
        FakeJira.created = []

    def test_the_bug_form_fields_carry_the_defect_and_the_held_back_test(self):
        params = server.defect_bug_params(self.parent, DEFECT)
        self.assertEqual((params["type"], params["summary"], params["expected"], params["feature"]), ("bug", DEFECT["summary"], "2 items", "parser"))
        self.assertIn("testCRLF was left out", params["repro"])
        self.assertIn("App/Extensions.swift", params["repro"])
        self.assertIn("func testCRLF() {}", params["spec"])

    def test_the_bug_job_becomes_a_child_and_the_defect_points_at_it(self):
        with mock.patch.object(server, "saved_integrations", return_value={}):
            server.link_defect_job(self.root, "p1", 0, self.jobs / "c1.json")
        child, parent = (json.loads((self.jobs / f"{n}.json").read_text()) for n in ("c1", "p1"))
        self.assertEqual(child["parent_job_id"], "p1")
        self.assertEqual(parent["production_defects"][0]["bug_job"], "c1")
        self.assertEqual(parent["child_jobs"], [{"job_id": "c1", "kind": "defect"}])
        self.assertEqual(server.job_stub(self.root, "p1"), {"id": "p1", "title": "Audit tests"})
        self.assertIsNone(server.job_stub(self.root, "../nope"))

    def test_a_job_tied_to_jira_gets_a_child_ticket_that_the_bug_job_then_follows(self):
        with mock.patch.object(server, "saved_integrations", return_value={"jira": {"site": "x"}}), mock.patch.object(integrations, "Jira", FakeJira):
            server.link_defect_job(self.root, "p1", 0, self.jobs / "c1.json")
        self.assertEqual(FakeJira.created[0][0], "ABC-123")
        child, parent = (json.loads((self.jobs / f"{n}.json").read_text()) for n in ("c1", "p1"))
        self.assertEqual([l["ref"] for l in child["external_links"]], ["ABC-124"])  # merging the fix moves the bug, not the original ticket
        self.assertEqual(child["jira_parent"], "ABC-123")
        self.assertEqual(parent["production_defects"][0]["jira"]["key"], "ABC-124")

    def test_without_jira_or_when_jira_fails_the_job_is_still_linked(self):
        with mock.patch.object(server, "saved_integrations", return_value={"jira": {"site": "x"}}), \
                mock.patch.object(integrations, "Jira", side_effect=lambda c: mock.Mock(create_child_bug=mock.Mock(side_effect=integrations.IntegrationError("no Sub-task type")))):
            server.link_defect_job(self.root, "p1", 0, self.jobs / "c1.json")
        child = json.loads((self.jobs / "c1.json").read_text())
        self.assertEqual((child["parent_job_id"], child["jira_link_error"]), ("p1", "no Sub-task type"))
        self.assertNotIn("jira", json.loads((self.jobs / "p1.json").read_text())["production_defects"][0])


class CaseViewTests(unittest.TestCase):
    def test_a_measurement_case_is_covered_once_coverage_was_measured_and_a_skipped_case_shows_why(self):
        from orchestrator.scripts import test_cases as tc
        cases = [{"id": "TC-81-01", "title": "Produce fresh baseline coverage measurement", "type": "integration", "expected": "x"},
                 {"id": "TC-81-14", "title": "Loader resets", "type": "functionality", "expected": "y"}]
        job = {"type": "test-audit", "plan": {"test_cases": cases}, "skipped_cases": [{"case": "TC-81-14", "reason": "Needs a production change"}]}
        self.assertEqual(tc.satisfied_cases(job), {})
        job["coverage"] = {"before": 60.0, "after": 70.25}
        with tempfile.TemporaryDirectory() as tmp:
            view = server.test_case_view(Path(tmp), tc.job_cases(job), None, tc.satisfied_cases(job), tc.skipped_cases(job))
        rows = {r["id"]: r for r in view["cases"]}
        self.assertEqual((rows["TC-81-01"]["status"], rows["TC-81-01"]["found_in"]), ("covered", ["Measured by the job: coverage is now 70.2%"]))
        self.assertEqual(rows["TC-81-14"]["skipped"], "Needs a production change")
        self.assertEqual(view["summary"]["covered"], 1)


class JiraChildBugTests(unittest.TestCase):
    def jira(self, level, fail_first=False):
        jira = integrations.Jira({"site": "x.atlassian.net", "email": "a@b.c", "token": "t"})
        jira._get = mock.Mock(return_value={"fields": {"issuetype": {"hierarchyLevel": level}, "project": {"key": "ABC"}}})
        calls = []

        def write(path, body):
            calls.append((path, body))
            if fail_first and len(calls) == 1:
                raise integrations.IntegrationError("no such type")
            return {"key": "ABC-9"} if path.endswith("/issue") else {}
        jira._write = write
        return jira, calls

    def test_under_an_epic_it_is_a_child_bug(self):
        jira, calls = self.jira(1)
        self.assertEqual(jira.create_child_bug("ABC-1", "Bug").ref, "ABC-9")
        self.assertEqual((calls[0][1]["fields"]["issuetype"], calls[0][1]["fields"]["parent"]), ({"name": "Bug"}, {"key": "ABC-1"}))
        self.assertEqual(len(calls), 1)

    def test_under_a_story_it_is_a_sub_task(self):
        jira, calls = self.jira(0)
        jira.create_child_bug("ABC-1", "Bug")
        self.assertEqual(calls[0][1]["fields"]["issuetype"], {"name": "Sub-task"})

    def test_when_a_child_cannot_be_made_it_is_a_bug_linked_to_the_ticket(self):
        jira, calls = self.jira(0, fail_first=True)
        jira.create_child_bug("ABC-1", "Bug")
        self.assertEqual(calls[1][1]["fields"]["issuetype"], {"name": "Bug"})
        self.assertNotIn("parent", calls[1][1]["fields"])
        self.assertEqual((calls[2][0], calls[2][1]["inwardIssue"], calls[2][1]["outwardIssue"]), ("/rest/api/3/issueLink", {"key": "ABC-1"}, {"key": "ABC-9"}))


if __name__ == "__main__":
    unittest.main()
