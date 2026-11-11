"""Add Existing Project → GitHub: repositories that are only on GitHub can be listed, downloaded and added."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from tests.test_web_ui import ServerTestCase, UI_HEADERS, ui  # noqa: E402


def fake_gh(answers):
    def run(argv, **kw):
        key = " ".join(argv[1:4])
        for prefix, (code, out, err) in answers.items():
            if key.startswith(prefix):
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")
    return run


REPO = lambda name, owner="me", updated="2026-11-01T00:00:00Z", **kw: {
    "nameWithOwner": f"{owner}/{name}", "name": name, "description": kw.get("description", ""), "isPrivate": kw.get("private", True),
    "updatedAt": updated, "isArchived": False, "isFork": False}


class ListTests(unittest.TestCase):
    def page(self, *repos):
        return json.dumps([{"repo": r[0], "name": r[0].split("/")[1], "description": r[2] if len(r) > 2 else "", "private": True,
                            "archived": False, "fork": False, "updated": r[1]} for r in repos])

    def test_every_repository_the_account_can_open_newest_first(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = "\n".join([self.page(("me/snake", "2026-10-01T00:00:00Z"), ("me/daoc-mobile", "2026-11-09T00:00:00Z", "Unity client")),
                              self.page(("acme/site", "2026-11-05T00:00:00Z"), ("friend/shared", "2026-11-07T00:00:00Z"))])
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        with patch.object(ui.shutil, "which", return_value="/usr/bin/gh"), patch.object(ui.subprocess, "run", run):
            data = ui.github_repos()
        self.assertIn("affiliation=owner,collaborator,organization_member", " ".join(calls[0]))  # not just the ones it owns
        self.assertIn("--paginate", calls[0])
        self.assertEqual([r["repo"] for r in data["repos"]], ["me/daoc-mobile", "friend/shared", "acme/site", "me/snake"])
        self.assertEqual(data["repos"][0]["description"], "Unity client")
        self.assertEqual(data["error"], "")

    def test_signed_out_or_no_gh_says_what_to_do(self):
        with patch.object(ui.shutil, "which", return_value=None):
            self.assertIn("Install the GitHub CLI", ui.github_repos()["error"])
        signed_out = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 401: Bad credentials (gh auth login)")
        with patch.object(ui.shutil, "which", return_value="/usr/bin/gh"), patch.object(ui.subprocess, "run", signed_out):
            data = ui.github_repos()
        self.assertEqual(data["repos"], [])
        self.assertIn("gh auth login", data["error"])


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patch = patch.object(Path, "home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_it_goes_in_the_usual_projects_folder(self):
        self.assertEqual(ui.clone_destination("daoc-mobile"), self.home / "Projects" / "daoc-mobile")
        (self.home / "Developer").mkdir()
        self.assertEqual(ui.clone_destination("daoc-mobile"), self.home / "Developer" / "daoc-mobile")

    def test_a_failed_download_leaves_no_folder_and_an_existing_one_is_never_overwritten(self):
        def failing(argv, **kw):
            Path(argv[4]).mkdir(parents=True)  # gh made the folder before it failed
            return SimpleNamespace(returncode=1, stdout="", stderr="GraphQL: Could not resolve to a Repository")
        with patch.object(ui.subprocess, "run", failing):
            with self.assertRaises(ui.UIError) as caught:
                ui.clone_github_repo("me/gone")
        self.assertIn("Could not resolve", str(caught.exception))
        self.assertFalse((self.home / "Projects" / "gone").exists())
        (self.home / "Projects" / "kept").mkdir(parents=True)
        with patch.object(ui.subprocess, "run", side_effect=AssertionError("must not clone over a folder")):
            with self.assertRaises(ui.UIError):
                ui.clone_github_repo("me/kept")

    def test_it_clones_with_the_github_cli_so_private_repositories_work(self):
        calls = []
        with patch.object(ui.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")):
            result = ui.clone_github_repo("me/daoc-mobile")
        self.assertEqual(calls[0][:4], ["gh", "repo", "clone", "me/daoc-mobile"])
        self.assertEqual(result, {"root": str(self.home / "Projects" / "daoc-mobile"), "name": "daoc-mobile"})


class ScanTests(unittest.TestCase):
    """The folder scan: three folders deep, worktrees found and told apart from the checkout they belong to."""

    def test_deeper_projects_and_worktrees_are_found_and_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, capture_output=True, check=True)
            deep = base / "games" / "unity" / "daoc-mobile"
            deep.mkdir(parents=True)
            run(deep, "init", "-q")
            main = base / "Thirteen"
            main.mkdir()
            run(main, "init", "-q", "-b", "main")
            run(main, "-c", "user.email=t@e.st", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "first")
            run(main, "worktree", "add", "-q", str(base / "Thirteen-handshake"))
            with patch.object(ui, "detect_project_source", return_value=("local", "Local", None)), \
                 patch.object(ui, "get_project_languages", return_value=[]), \
                 patch.object(ui, "load_recent_projects", return_value={"projects": []}):
                found = {Path(p["root"]).name: p for p in ui.scan_for_projects([base])}
        self.assertIn("daoc-mobile", found)  # three folders down
        self.assertIn("Thirteen-handshake", found)  # its .git is a file
        self.assertEqual(Path(found["Thirteen-handshake"]["worktree_of"]).name, "Thirteen")
        self.assertEqual(found["Thirteen"]["worktree_of"], "")
        self.assertEqual(found["Thirteen-handshake"]["folder"], "Thirteen-handshake")

    def test_repositories_inside_a_project_are_not_listed_even_when_reached_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            outer = home / "Projects" / "DAoC-Mobile"
            (outer / ".git").mkdir(parents=True)
            (outer / "OpenDAoC-Core" / ".git").mkdir(parents=True)  # a repository kept inside the project
            with patch.object(ui, "detect_project_source", return_value=("local", "Local", None)), \
                 patch.object(ui, "get_project_languages", return_value=[]), \
                 patch.object(ui, "load_recent_projects", return_value={"projects": []}):
                found = [Path(p["root"]).name for p in ui.scan_for_projects([home / "Projects", home])]  # home reaches it again
        self.assertEqual(found, ["DAoC-Mobile"])

    def test_rows_with_the_same_name_are_told_apart_by_folder(self):
        script = r"""
require(process.argv[1]); require(process.argv[2]);
process.stdout.write(ProjectPicker.renderRows([
  {name: "Thirteen", root: "/u/Thirteen", folder: "Thirteen", configured: true, worktree_of: ""},
  {name: "Thirteen", root: "/u/Thirteen-handshake", folder: "Thirteen-handshake", configured: true, worktree_of: "/u/Thirteen"},
  {name: "Thirteen", root: "/u/Downloads/Thirteen-copy", folder: "Thirteen-copy", configured: true, worktree_of: ""},
]));"""
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        html = subprocess.run(["node", "-e", script, str(static / "html.js"), str(static / "project-picker.js")],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn(">Thirteen-handshake <span class=\"muted project-discovery-note\">· Worktree of Thirteen</span>", html)
        self.assertIn(">Thirteen-copy <span class=\"muted project-discovery-note\">· Thirteen</span>", html)
        self.assertIn('<span class="pill-badge">Worktree</span>', html)


class EndpointTests(ServerTestCase):
    def test_only_owner_slash_name_is_accepted(self):
        for bad in ("daoc-mobile", "me/../../etc", "me/x; rm -rf ~", "", "a/b/c"):
            res, data = self.request("POST", "/api/projects/clone", body={"repo": bad}, headers=UI_HEADERS)
            self.assertEqual(res.status, 400, bad)

    def test_the_page_offers_a_github_tab(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        dialog = app[app.index("function addProjectsDialog"):app.index("const dialogs = {")]
        for bit in ('data-project-tab="github"', 'api("projects/github")', 'api("projects/clone"', "On this computer", "Clone &amp; add"):
            self.assertIn(bit, dialog)

    def test_dropdown_includes_github_repos(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        dropdown = app[app.index("function renderProjectSelect"):app.index("document.addEventListener(\"change\"")]
        self.assertIn("p.github_repos", dropdown)
        self.assertIn("unDownloaded", dropdown)


if __name__ == "__main__":
    unittest.main()
