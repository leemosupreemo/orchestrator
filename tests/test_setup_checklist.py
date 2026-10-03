from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import setup_checklist as sc


def _checklist(root: Path, **over):
    gh = over.get("gh", {"installed": True, "user": "dev"})
    providers = over.get("providers", ["claude"])
    with patch.object(sc, "github_cli_state", return_value=gh), patch.object(sc, "ready_llm_providers", return_value=providers):
        return sc.setup_checklist(root, root / ".orchestrator")


def _by_id(result):
    return {i["id"]: i for i in result["items"]}


class SetupChecklistTests(unittest.TestCase):
    def make_project(self, root: Path, origin: str | None = "git@github.com:me/app.git") -> None:
        (root / ".orchestrator" / "config").mkdir(parents=True)
        (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "App"}))
        (root / ".orchestrator" / "config" / "machines.json").write_text(
            json.dumps({"machines": [{"name": "local", "execution_mode": "local", "models": ["claude-x"]}]}))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)

    def test_fresh_folder_is_missing_every_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            result = _checklist(Path(d), gh={"installed": False, "user": None}, providers=[])
            items = _by_id(result)
            for key in ("project", "git", "llm", "machines", "models"):
                self.assertTrue(items[key]["required"] and not items[key]["done"], key)
            self.assertFalse(result["complete"])
            # No GitHub remote, so plain git: GitHub isn't asked for, and a remote is optional.
            self.assertNotIn("github_cli", items)
            self.assertFalse(items["remote"]["required"])
            self.assertTrue(all(not items[k]["required"] for k in ("firebase", "email", "workers", "prompts")))

    def test_github_project_needs_github(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            items = _by_id(_checklist(root, gh={"installed": False, "user": None}))
            self.assertTrue(items["github_cli"]["required"] and not items["github_cli"]["done"])
            self.assertEqual(items["github_cli"]["hint"], "brew install gh")

    def test_fully_configured_project_is_complete(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            result = _checklist(root)
            self.assertTrue(result["complete"], [i["id"] for i in result["items"] if i["required"] and not i["done"]])
            self.assertEqual(result["required_done"], result["required_total"])

    def test_gitlab_project_is_complete_without_github(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root, origin="https://gitlab.com/me/app.git")
            result = _checklist(root, gh={"installed": False, "user": None})
            items = _by_id(result)
            self.assertTrue(result["complete"], [i["id"] for i in result["items"] if i["required"] and not i["done"]])
            self.assertTrue(items["remote"]["done"])
            self.assertIn("GitLab", items["remote"]["detail"])
            self.assertNotIn("github_cli", items)

    def test_choosing_github_mode_requires_github_even_on_another_host(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root, origin="https://gitlab.com/me/app.git")
            (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "App", "code_host": "github"}))
            result = _checklist(root, gh={"installed": True, "user": None})
            items = _by_id(result)
            self.assertFalse(items["github_remote"]["done"])
            self.assertFalse(items["github_cli"]["done"])
            self.assertEqual(items["github_cli"]["action"]["params"], {"menu": "github"})  # offers to sign in
            self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
