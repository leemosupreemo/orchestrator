"""A repository without Orchestrator's labels gets them, so a job's issue shows its status.

On a repository that had never had a job, every `gh issue edit --add-label status:planned` failed with "'status:planned' not
found" and issue creation quietly dropped the labels, so the issue never showed that a job was planned, executing or waiting
for the person. The tests run the real functions against a fake `gh` that refuses unknown labels the way GitHub does."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator/scripts"))
import common  # noqa: E402
import new_job  # noqa: E402
import splinter_job  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_GH_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["calls"].append(args[:2])
def save(): json.dump(state, open(path, "w"))
def flags(name): return [args[i + 1] for i, a in enumerate(args) if a == name]
if args[:2] == ["label", "create"]:
    name = args[2]
    if os.environ.get("FAKE_GH_DENY_CREATE"):
        print("HTTP 403: Resource not accessible by integration", file=sys.stderr); save(); sys.exit(1)
    if name in state["labels"]:
        print(f'label with name "{name}" already exists; use `--force` to update its color and description', file=sys.stderr); save(); sys.exit(1)
    state["labels"][name] = {"color": flags("--color")[0], "description": flags("--description")[0]}
    save(); print("created"); sys.exit(0)
wanted = flags("--add-label") + flags("--remove-label") + flags("--label")
for label in wanted:
    if label not in state["labels"]:
        verb = "could not add label" if args[:2] == ["issue", "create"] else f"failed to update https://github.com/o/r/issues/{args[2]}"
        print(f"{verb}: '{label}' not found", file=sys.stderr); save(); sys.exit(1)
if args[:2] == ["issue", "edit"]:
    issue = state["issues"].setdefault(args[2], [])
    for label in flags("--add-label"):
        if label not in issue: issue.append(label)
    for label in flags("--remove-label"):
        if label in issue: issue.remove(label)
    save(); print(f"https://github.com/o/r/issues/{args[2]}"); sys.exit(0)
if args[:2] == ["issue", "create"]:
    number = str(len(state["issues"]) + 100)
    state["issues"][number] = flags("--label")
    save(); print(f"https://github.com/o/r/issues/{number}"); sys.exit(0)
save(); sys.exit(0)
'''


class FakeGitHub:
    def __init__(self, labels=None, deny_create=False):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.state_path = base / "state.json"
        self.state_path.write_text(json.dumps({"labels": labels or {}, "issues": {}, "calls": []}))
        gh = base / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        env = {"PATH": f"{base}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_STATE": str(self.state_path)}
        if deny_create:
            env["FAKE_GH_DENY_CREATE"] = "1"
        self.patch = patch.dict(os.environ, env)

    def __enter__(self):
        self.patch.start()
        common._labels_ensured.clear()
        common._label_warned.clear()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.dir.cleanup()

    @property
    def state(self):
        return json.loads(self.state_path.read_text())

    def count(self, call):
        return self.state["calls"].count(call)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class StatusUpdateTests(unittest.TestCase):
    def test_missing_labels_are_created_and_the_status_is_shown(self):
        with FakeGitHub() as gh, quiet():
            common.update_issue_status(5, "status:human-needed", ["status:executing"])
            self.assertEqual(gh.state["issues"]["5"], ["status:human-needed"])
            self.assertEqual(gh.state["labels"]["status:human-needed"], {"color": "fbca04", "description": "Where an Orchestrator job is"})
            self.assertIn("status:executing", gh.state["labels"])

    def test_a_label_that_exists_keeps_its_colour_and_is_not_created_again(self):
        with FakeGitHub(labels={"status:planned": {"color": "123456", "description": "mine"}}) as gh, quiet():
            common.update_issue_status(5, "status:planned")
            self.assertEqual(gh.state["labels"]["status:planned"], {"color": "123456", "description": "mine"})
            self.assertEqual(gh.count(["label", "create"]), 0)

    def test_each_label_is_created_once_per_run(self):
        with FakeGitHub() as gh, quiet():
            common.update_issue_status(5, "status:planned")
            common.update_issue_status(6, "status:planned")
            self.assertEqual(gh.count(["label", "create"]), 1)

    def test_when_labels_cannot_be_created_it_says_so_once_and_never_raises(self):
        out = io.StringIO()
        with FakeGitHub(deny_create=True) as gh, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            common.update_issue_status(5, "status:planned")
            common.update_issue_status(5, "status:planned")
            self.assertEqual(gh.state["issues"], {})
        self.assertEqual(out.getvalue().count("Couldn't create the 'status:planned' label"), 1)

    def test_no_issue_or_no_gh_is_still_a_quiet_no_op(self):
        with FakeGitHub() as gh:
            common.update_issue_status(None, "status:planned")
            self.assertEqual(gh.state["calls"], [])
        with patch.object(common.shutil, "which", return_value=None):
            common.update_issue_status(5, "status:planned")
            self.assertEqual(common.ensure_labels("status:planned"), ["status:planned"])

    def test_the_label_is_read_from_gh_s_error_text(self):
        self.assertEqual(common.missing_label("failed to update https://github.com/o/r/issues/15: 'status:planned' not found"), "status:planned")
        self.assertEqual(common.missing_label("could not add label: 'job:quick' not found"), "job:quick")
        self.assertIsNone(common.missing_label("HTTP 502"))


class IssueCreationTests(unittest.TestCase):
    def test_a_new_issue_gets_all_its_labels_on_a_repository_that_lacked_them(self):
        with FakeGitHub() as gh, quiet():
            number = new_job.create_issue("t", "b", ["job:quick", "status:planned"])
            self.assertEqual(gh.state["issues"][str(number)], ["job:quick", "status:planned"])

    def test_when_a_label_cannot_be_created_the_issue_is_still_created_without_it(self):
        with FakeGitHub(deny_create=True) as gh, quiet():
            number = new_job.create_issue("t", "b", ["job:quick", "status:planned"])
            self.assertEqual(gh.state["issues"][str(number)], [])

    def test_a_subtask_issue_gets_its_labels_or_is_created_without_them(self):
        with FakeGitHub() as gh, quiet():
            number = splinter_job.create_subtask_issue("t", "b", 9)
            self.assertEqual(gh.state["issues"][str(number)], ["job:bug", "source:splinter", "status:planned"])
        with FakeGitHub(deny_create=True) as gh, quiet():
            number = splinter_job.create_subtask_issue("t", "b", 9)
            self.assertEqual(gh.state["issues"][str(number)], [])


if __name__ == "__main__":
    unittest.main()
