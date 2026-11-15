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
    git_auth = over.get("git_auth", (True, "Non-interactive git access verified"))
    with patch.object(sc, "github_cli_state", return_value=gh), \
         patch.object(sc, "ready_llm_providers", return_value=providers), \
         patch.object(sc, "git_remote_auth_state", return_value=git_auth):
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
        (root / ".orchestrator" / "project.json").write_text(json.dumps({
            "project_name": "App",
            "build_command": "make build",
            "test_command": "make test",
        }))
        (root / ".orchestrator" / "config" / "machines.json").write_text(
            json.dumps({"machines": [{"name": "local", "execution_mode": "local", "models": ["claude-x"]}]}))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)

    def test_fresh_folder_is_missing_every_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            result = _checklist(Path(d), gh={"installed": False, "user": None}, providers=[])
            items = _by_id(result)
            for key in ("project", "git", "llm", "machines"):
                self.assertTrue(items[key]["required"] and not items[key]["done"], key)
            self.assertFalse(result["complete"])
            # No GitHub remote, so plain git: GitHub isn't asked for, and a remote is optional.
            self.assertNotIn("github_cli", items)
            self.assertFalse(items["remote"]["required"])
            self.assertTrue(all(not items[k]["required"] for k in ("firebase", "email", "prompts")))

    def test_no_ai_yet_offers_to_set_up_a_free_one_in_one_click(self):
        with tempfile.TemporaryDirectory() as d:
            llm = _by_id(_checklist(Path(d), providers=[]))["llm"]
            self.assertEqual(llm["action"], {"type": "free_ai", "label": "Set up free AI"})
            self.assertIn("one click", llm["detail"])
            ready = _by_id(_checklist(Path(d), providers=["opencode"]))["llm"]  # one is ready: the usual page
            self.assertEqual(ready["action"]["type"], "route")

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
            self.assertFalse(_by_id(result)["llm"]["done"])
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

    def test_unconfigured_project_offers_one_click_configure(self):
        with tempfile.TemporaryDirectory() as d:
            items = _by_id(_checklist(Path(d), gh={"installed": True, "user": "dev"}, providers=[]))
            self.assertFalse(items["project"]["done"])
            self.assertEqual(items["project"]["action"], {"type": "configure_project", "label": "Configure"})

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

    def test_build_and_test_commands_checked_in_setup_and_job_blockers(self):
        from orchestrator.web import server

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "App"}))

            result = _checklist(root)
            items = _by_id(result)
            self.assertIn("build_test_commands", items)
            self.assertTrue(items["build_test_commands"]["required"])
            self.assertFalse(items["build_test_commands"]["done"])
            self.assertFalse(result["complete"])

            blockers = server.job_blockers(root, {"status": "planned", "type": "feature"})
            build_blockers = [b for b in blockers if b.get("id") == "build_test_commands"]
            self.assertEqual(len(build_blockers), 1)
            self.assertEqual(build_blockers[0]["route"], "#/checkup")

            # Option 1: explicit build_command and test_command in project.json
            (root / ".orchestrator" / "project.json").write_text(json.dumps({
                "project_name": "App",
                "build_command": "make build",
                "test_command": "make test",
            }))
            self.assertTrue(_by_id(_checklist(root))["build_test_commands"]["done"])
            self.assertFalse(any(
                b.get("id") == "build_test_commands"
                for b in server.job_blockers(root, {"status": "planned", "type": "feature"})
            ))

            # Option 2: documented in docs/build-test-commands.md
            (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "App"}))
            (root / "docs").mkdir(parents=True, exist_ok=True)
            (root / "docs" / "build-test-commands.md").write_text(
                "## Build\n```bash\nmake build\n```\n\n## Tests\n```bash\nmake test\n```\n"
            )
            self.assertTrue(_by_id(_checklist(root))["build_test_commands"]["done"])

            # Option 3: Apple project auto-detection (Package.swift or .xcodeproj)
            (root / "docs" / "build-test-commands.md").unlink()
            self.assertFalse(_by_id(_checklist(root))["build_test_commands"]["done"])
            (root / "Package.swift").write_text("// swift-tools-version: 5.9\n")
            self.assertTrue(_by_id(_checklist(root))["build_test_commands"]["done"])

            # Option 4: non-Apple stack auto-detection (e.g. Cargo.toml)
            (root / "Package.swift").unlink()
            self.assertFalse(_by_id(_checklist(root))["build_test_commands"]["done"])
            (root / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n')
            self.assertTrue(_by_id(_checklist(root))["build_test_commands"]["done"])

    def test_ensure_gh_git_credentials_runs_setup_git_once(self):
        from orchestrator import setup_checklist
        with patch.object(setup_checklist, "_gh_git_wired", False), \
             patch("orchestrator.setup_checklist.shutil.which", return_value="/usr/local/bin/gh"), \
             patch("orchestrator.setup_checklist._run") as mock_run:
            mock_run.return_value = unittest.mock.MagicMock(returncode=0)
            self.assertTrue(setup_checklist.ensure_gh_git_credentials())
            self.assertTrue(setup_checklist.ensure_gh_git_credentials())
            mock_run.assert_called_once_with(
                ["gh", "auth", "setup-git", "--hostname", "github.com"],
                timeout=5,
            )

    def test_cli_ready_rejects_expired_oauth_status(self):
        from orchestrator import setup_checklist
        with patch("orchestrator.setup_checklist.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("orchestrator.setup_checklist._run", return_value=unittest.mock.MagicMock(
                 returncode=0, stdout="OAuth access token has expired. Re-authenticate to continue.", stderr=""
             )):
            self.assertFalse(setup_checklist._cli_ready("claude"))

    def test_git_remote_auth_state_flags_missing_credentials(self):
        from orchestrator import setup_checklist
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch("orchestrator.setup_checklist.subprocess.run", return_value=unittest.mock.MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            )):
                ok, detail = setup_checklist.git_remote_auth_state(
                    root, "https://github.com/org/repo.git", fresh=True
                )
            self.assertFalse(ok)
            self.assertIn("non-interactively", detail)

    def test_llm_item_fails_when_ready_provider_not_enabled_on_machines(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_project(root)
            # Machine has only ['codex'] enabled, while ready provider is ['opencode']
            (root / ".orchestrator" / "config" / "machines.json").write_text(json.dumps({
                "machines": [{"name": "local", "enabled": True, "models": ["codex"]}]
            }))
            result = _checklist(root, providers=["opencode"])
            items = _by_id(result)
            self.assertFalse(items["llm"]["done"])
            self.assertIn("Computers", items["llm"]["detail"])
            self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
