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
    def test_explicit_recheck_discards_cached_tool_and_login_results(self):
        with tempfile.TemporaryDirectory() as d:
            sc._cache["gh"] = (0, {"installed": False, "user": None})
            sc._cache["llm"] = (0, [])
            with patch.object(sc, "github_cli_state", return_value={"installed": False, "user": None}), \
                 patch.object(sc, "ready_llm_providers", return_value=[]):
                sc.setup_checklist(Path(d), Path(d) / ".orchestrator", fresh=True)
            self.assertNotIn("gh", sc._cache)
            self.assertNotIn("llm", sc._cache)

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
            self.assertTrue(all(not items[k]["required"] for k in ("firebase", "email", "prompts")))

    def test_github_project_needs_github(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            items = _by_id(_checklist(root, gh={"installed": False, "user": None}))
            self.assertTrue(items["github_cli"]["required"] and not items["github_cli"]["done"])
            self.assertEqual(items["github_cli"]["hint"], "brew install gh")

    def test_disabled_machines_do_not_satisfy_job_requirements(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            (root / ".orchestrator/config/machines.json").write_text(json.dumps({"machines": [
                {"name": "paused", "enabled": False, "models": ["claude-x"]}]}))
            result = _checklist(root)
            self.assertFalse(_by_id(result)["machines"]["done"])
            self.assertFalse(_by_id(result)["models"]["done"])
            self.assertFalse(result["complete"])

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
            self.assertEqual(items["github_cli"]["action"], {"type": "github"})  # offers to sign in, in the app
            self.assertFalse(result["complete"])

    def test_uninitialized_folder_offers_git_init_action(self):
        with tempfile.TemporaryDirectory() as d:
            result = _checklist(Path(d), gh={"installed": True, "user": "dev"}, providers=[])
            items = _by_id(result)
            self.assertFalse(items["git"]["done"])
            self.assertEqual(items["git"]["action"], {"type": "git_init", "label": "Initialize"})

    def test_git_repo_without_origin_offers_github_create_when_signed_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root, origin=None)
            result = _checklist(root, gh={"installed": True, "user": "dev"})
            items = _by_id(result)
            self.assertTrue(items["git"]["done"])
            self.assertIsNone(items["git"]["action"])
            self.assertEqual(items["remote"]["action"], {"type": "github_create", "label": "Create repo"})

    def test_github_mode_without_origin_offers_github_create_when_signed_in(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root, origin=None)
            (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "App", "code_host": "github"}))
            result = _checklist(root, gh={"installed": True, "user": "dev"})
            items = _by_id(result)
            self.assertEqual(items["github_remote"]["action"], {"type": "github_create", "label": "Create repo"})

    def test_python_project_includes_coverage_py_item(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            (root / "requirements.txt").write_text("pytest\n")
            result = _checklist(root)
            items = _by_id(result)
            self.assertIn("coverage_py", items)
            self.assertFalse(items["coverage_py"]["required"])
            self.assertEqual(items["coverage_py"]["action"]["action"], "coverage")


if __name__ == "__main__":
    unittest.main()
