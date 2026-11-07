from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import cli
from orchestrator.web import local_server, ui_build

REPO_ROOT = Path(__file__).resolve().parents[1]


class UIBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="orch-ui-build-")
        self.addCleanup(tmp.cleanup)
        self.web = Path(tmp.name) / "web"
        self.dist = Path(tmp.name) / "dist"
        (self.web / "src").mkdir(parents=True)
        (self.web / "package.json").write_text("{}")
        (self.web / "package-lock.json").write_text("{}")
        self.source = self.web / "src" / "main.tsx"
        self.source.write_text("app")
        self.index = self.dist / "index.html"

    def set_age(self, path: Path, seconds_ago: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        stamp = self.source.stat().st_mtime - seconds_ago
        os.utime(path, (stamp, stamp))

    def fake_build(self, command, cwd, check, timeout):
        self.commands.append(command[1:])
        if command[1:] == ["run", "build"]:
            self.dist.mkdir(exist_ok=True)
            self.index.write_text("built")

    def ensure(self, npm: str | None = "/bin/npm", run=None):
        self.commands: list[list[str]] = []
        with patch.object(ui_build.shutil, "which", return_value=npm), patch.object(
            ui_build.subprocess, "run", run or self.fake_build
        ):
            ui_build.ensure_ui_built(self.web, self.dist)

    def test_fresh_checkout_installs_dependencies_and_builds(self) -> None:
        self.ensure()
        self.assertEqual(self.commands, [["ci"], ["run", "build"]])
        self.assertTrue(self.index.is_file())

    def test_current_build_is_left_alone(self) -> None:
        self.index.parent.mkdir()
        self.index.write_text("built")
        self.set_age(self.source, 10)
        self.source.touch()
        os.utime(self.index, (self.source.stat().st_mtime + 10,) * 2)
        self.ensure()
        self.assertEqual(self.commands, [])

    def test_changed_source_rebuilds_without_reinstalling(self) -> None:
        self.set_age(self.index, 100)
        self.set_age(self.web / "package-lock.json", 100)
        self.set_age(self.web / "node_modules" / ".package-lock.json", 50)
        self.ensure()
        self.assertEqual(self.commands, [["run", "build"]])

    def test_changed_lockfile_reinstalls_dependencies(self) -> None:
        self.set_age(self.index, 100)
        self.set_age(self.web / "node_modules" / ".package-lock.json", 50)
        self.ensure()
        self.assertEqual(self.commands, [["ci"], ["run", "build"]])

    def test_no_node_and_no_build_explains_the_fix(self) -> None:
        with self.assertRaisesRegex(ui_build.UIUnavailable, "Install Node.js"):
            self.ensure(npm=None)

    def test_no_node_keeps_older_build(self) -> None:
        self.set_age(self.index, 100)
        self.ensure(npm=None)
        self.assertEqual(self.commands, [])

    def test_failed_build_keeps_older_build(self) -> None:
        self.set_age(self.index, 100)
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "npm")
        self.ensure(run=fail)
        self.assertTrue(self.index.is_file())

    def test_failed_build_without_older_build_raises(self) -> None:
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "npm")
        with self.assertRaisesRegex(ui_build.UIUnavailable, "build failed"):
            self.ensure(run=fail)

    def test_install_without_source_uses_bundled_build(self) -> None:
        self.source.unlink()
        self.index.parent.mkdir()
        self.index.write_text("built")
        self.ensure()
        self.assertEqual(self.commands, [])

    def test_install_without_source_or_build_explains_reinstall(self) -> None:
        self.source.unlink()
        with self.assertRaisesRegex(ui_build.UIUnavailable, "pipx install --force"):
            self.ensure()

    def test_cli_reports_missing_ui_cleanly(self) -> None:
        with patch.object(local_server, "ensure_ui_built", side_effect=ui_build.UIUnavailable("needs Node")), patch(
            "builtins.print"
        ) as printed:
            self.assertEqual(cli.main(["ui", "--no-open"]), 1)
        self.assertIn("needs Node", " ".join(str(call) for call in printed.call_args_list))

    def test_background_server_builds_before_starting(self) -> None:
        with patch.object(local_server, "_BACKGROUND_SERVER", None), patch.object(
            local_server, "ensure_ui_built", side_effect=ui_build.UIUnavailable("needs Node")
        ), self.assertRaises(ui_build.UIUnavailable):
            local_server.get_or_start_background_server()

    def test_build_output_paths_stay_in_step(self) -> None:
        relative = ui_build.DIST_DIR.relative_to(REPO_ROOT).as_posix()
        self.assertEqual(ui_build.WEB_SOURCE_DIR, REPO_ROOT / "web")
        self.assertEqual(local_server.get_static_dir(), ui_build.DIST_DIR)
        self.assertEqual(json.loads((REPO_ROOT / "firebase.json").read_text())["hosting"]["public"], relative)
        self.assertIn(f"--outDir ../{relative}", json.loads((REPO_ROOT / "web/package.json").read_text())["scripts"]["build"])
        self.assertIn("web/dist/assets/*", (REPO_ROOT / "pyproject.toml").read_text())
