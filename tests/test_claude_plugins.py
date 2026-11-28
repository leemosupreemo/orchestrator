"""A project-scoped Claude Code plugin installed inside ~/Documents can make a headless `claude -p` wait forever for a
macOS permission question nobody can answer (it hangs before printing anything, but only inside a git repository). These
checks only look at paths: asking macOS about the folder itself would hang the same way."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import claude_plugins as cp


def home_with(config: dict | str | None, name: str = "home") -> Path:
    home = Path(tempfile.mkdtemp(prefix=f"orchestrator-{name}-"))
    (home / ".claude" / "plugins").mkdir(parents=True)
    if config is not None:
        text = config if isinstance(config, str) else json.dumps(config)
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(text, encoding="utf-8")
    return home


def repo(name: str = "repo") -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"orchestrator-{name}-"))
    (root / ".git").mkdir()
    return root


def project_config(*entries: dict) -> dict:
    return {
        "version": 2,
        "plugins": {entry.pop("name"): [entry] for entry in entries},
    }


class ProtectedProjectPluginsTests(unittest.TestCase):
    def test_a_project_plugin_under_documents_is_flagged(self):
        home = home_with(project_config({"name": "playwright@claude-plugins-official", "scope": "project",
                                         "projectPath": "DOCUMENTS/Thirteen", "version": "unknown"}))
        config_path = home / ".claude" / "plugins" / "installed_plugins.json"
        config = json.loads(config_path.read_text().replace("DOCUMENTS", str(home / "Documents")))

        found = cp.protected_project_plugins(config, home)

        self.assertEqual([f["plugin"] for f in found], ["playwright@claude-plugins-official"])
        self.assertEqual(found[0]["folder"], "Documents")
        self.assertEqual(found[0]["path"], str(home / "Documents" / "Thirteen"))

    def test_desktop_downloads_and_the_folders_themselves_count(self):
        home = home_with(None)
        config = {"plugins": {
            "desk@marketplace": [{"scope": "project", "projectPath": str(home / "Desktop")}],
            "down@marketplace": [{"scope": "project", "projectPath": str(home / "Downloads" / "app")}],
            "docs@marketplace": [{"scope": "project", "projectPath": str(home / "Documents")}],
        }}

        self.assertEqual({f["folder"] for f in cp.protected_project_plugins(config, home)},
                         {"Desktop", "Downloads", "Documents"})

    def test_user_scope_and_other_projects_are_left_alone(self):
        home = home_with(None)
        config = {"plugins": {
            "user@marketplace": [{"scope": "user", "installPath": str(home / "Documents" / "cached")}],
            "projects@marketplace": [{"scope": "project", "projectPath": str(home / "Projects" / "game")}],
            "lookalike@marketplace": [{"scope": "project", "projectPath": str(home / "Documents-old" / "x")}],
            "elsewhere@marketplace": [{"scope": "project", "projectPath": "/Users/someone-else/Documents/x"}],
        }}

        self.assertEqual(cp.protected_project_plugins(config, home), [])

    def test_entries_without_a_path_are_ignored(self):
        home = home_with(None)
        config = {"plugins": {"odd@marketplace": [
            {"scope": "project"}, {"scope": "project", "projectPath": ""}, {"scope": "project", "projectPath": 3},
            "not an entry", {"scope": "project", "projectPath": "~/Documents/" + "x"},
        ]}}

        found = cp.protected_project_plugins(config, home)

        self.assertEqual([f["path"] for f in found], [str(home / "Documents" / "x")])

    def test_a_missing_or_broken_file_means_no_warning(self):
        for config in (None, "{not json", "[]", json.dumps({"plugins": {"p": "not a list"}}), json.dumps({"plugins": None})):
            with self.subTest(config=config):
                self.assertEqual(cp.findings(repo(), home=home_with(config), platform="darwin"), [])

    def test_a_folder_is_only_flagged_inside_a_git_repository(self):
        home = home_with(project_config({"name": "p@m", "scope": "project",
                                         "projectPath": "DOCUMENTS/x"}))
        path = home / ".claude" / "plugins" / "installed_plugins.json"
        path.write_text(path.read_text().replace("DOCUMENTS", str(home / "Documents")))

        plain = Path(tempfile.mkdtemp(prefix="orchestrator-plain-"))
        nested = repo() / "Sources" / "App"
        nested.mkdir(parents=True)

        self.assertEqual(cp.findings(plain, home=home, platform="darwin"), [])
        self.assertEqual(cp.findings(nested, home=home, platform="darwin")[0]["plugin"], "p@m")

    def test_a_worktree_git_file_counts_too(self):
        home = home_with(project_config({"name": "p@m", "scope": "project",
                                         "projectPath": "DOCUMENTS/x"}))
        path = home / ".claude" / "plugins" / "installed_plugins.json"
        path.write_text(path.read_text().replace("DOCUMENTS", str(home / "Documents")))
        worktree = Path(tempfile.mkdtemp(prefix="orchestrator-worktree-"))
        (worktree / ".git").write_text("gitdir: /elsewhere\n")

        self.assertEqual(len(cp.findings(worktree, home=home, platform="darwin")), 1)

    def test_only_macos_has_this_problem(self):
        home = home_with(project_config({"name": "p@m", "scope": "project",
                                         "projectPath": "DOCUMENTS/x"}))
        path = home / ".claude" / "plugins" / "installed_plugins.json"
        path.write_text(path.read_text().replace("DOCUMENTS", str(home / "Documents")))

        for platform in ("linux", "win32", "freebsd"):
            with self.subTest(platform=platform):
                self.assertEqual(cp.findings(repo(), home=home, platform=platform), [])

    def test_the_warning_names_the_plugin_the_folder_and_the_fix(self):
        home = home_with(project_config({"name": "playwright@claude-plugins-official", "scope": "project",
                                         "projectPath": "DOCUMENTS/Thirteen"}))
        path = home / ".claude" / "plugins" / "installed_plugins.json"
        path.write_text(path.read_text().replace("DOCUMENTS", str(home / "Documents")))

        (warning,) = cp.warnings(repo(), home=home, platform="darwin")

        self.assertIn("playwright@claude-plugins-official", warning)
        self.assertIn(str(home / "Documents" / "Thirteen"), warning)
        self.assertIn("Documents", warning)
        self.assertIn("installed_plugins.json", warning)


if __name__ == "__main__":
    unittest.main()
