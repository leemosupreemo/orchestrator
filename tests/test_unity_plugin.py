from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import cli, unity_plugin as plugin
from orchestrator.web import server as ui
from tests.test_web_ui import UI_HEADERS, ServerTestCase


def make_unity_project(root: Path) -> Path:
    (root / "ProjectSettings").mkdir(parents=True)
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.10f1\n")
    (root / "Assets").mkdir()
    return root


class PackageTests(unittest.TestCase):
    def test_package_describes_itself(self):
        data = json.loads((plugin.source_dir() / "package.json").read_text())
        self.assertEqual(data["name"], plugin.PACKAGE_NAME)
        self.assertEqual(plugin.package_version(plugin.source_dir()), data["version"])
        for asmdef in plugin.source_dir().rglob("*.asmdef"):
            self.assertEqual(json.loads(asmdef.read_text())["name"], asmdef.stem)

    def test_every_file_and_folder_has_a_meta_file_with_its_own_guid(self):
        package = plugin.source_dir()
        guids = []
        for path in package.rglob("*"):
            if path.suffix == ".meta" or path.name == ".DS_Store" or "__pycache__" in path.parts:
                continue
            meta = path.with_name(path.name + ".meta")
            self.assertTrue(meta.is_file(), f"{path.relative_to(package)} has no .meta file (python3 -c 'from orchestrator import unity_plugin as u; u.write_meta_files(u.source_dir())')")
            text = meta.read_text()
            self.assertEqual(text, plugin.meta_text(str(path.relative_to(package)), path.is_dir(), path.suffix))
            guids.append(next(l for l in text.splitlines() if l.startswith("guid: ")))
        self.assertEqual(len(guids), len(set(guids)))

    def test_cs_files_are_not_left_without_the_namespace(self):
        for path in (plugin.source_dir() / "Editor").glob("*.cs"):
            if path.name != "AssemblyInfo.cs":
                self.assertIn("namespace Orchestrator.UnityBridge", path.read_text(), path.name)


class InstallTests(unittest.TestCase):
    def test_installs_into_packages_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_unity_project(Path(tmp))
            first = plugin.install(root)
            self.assertEqual(first["status"], "installed")
            target = plugin.installed_dir(root)
            self.assertTrue((target / "package.json").is_file())
            self.assertTrue((target / "Editor" / "OrchestratorWindow.cs.meta").is_file())
            self.assertEqual(plugin.install(root)["status"], "current")

    def test_refuses_to_overwrite_an_edited_copy_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_unity_project(Path(tmp))
            plugin.install(root)
            edited = plugin.installed_dir(root) / "Editor" / "Settings.cs"
            edited.write_text("// my change\n")
            with self.assertRaises(plugin.PluginError):
                plugin.install(root)
            self.assertEqual(edited.read_text(), "// my change\n")
            self.assertEqual(plugin.install(root, force=True)["status"], "updated")
            self.assertNotEqual(edited.read_text(), "// my change\n")

    def test_refuses_what_is_not_a_unity_project_or_is_already_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(plugin.PluginError):
                plugin.install(Path(tmp))
            root = make_unity_project(Path(tmp) / "game")
            (root / "Packages").mkdir()
            (root / "Packages" / "manifest.json").write_text(json.dumps({"dependencies": {plugin.PACKAGE_NAME: "1.0.0"}}))
            with self.assertRaises(plugin.PluginError):
                plugin.install(root)
            self.assertFalse(plugin.installed_dir(root).exists())

    def test_command_reports_the_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_unity_project(Path(tmp))
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["unity-plugin", "install", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("Installed the Orchestrator Unity plugin", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["unity-plugin", "install", str(Path(tmp) / "elsewhere")])
            self.assertEqual(code, 1)
            self.assertIn("not a Unity project", out.getvalue())


class EditorRequestTests(ServerTestCase):
    """The requests the Editor window makes (Editor/Client.cs and Editor/JobRequest.cs), against the real server."""

    # JobRequest.NewBug("Score resets", "1. Play", "Keeps score", "### Unity console\n```\nError: x\n```", false)
    BODY = ('{"action":"new_job","params":{"type":"bug","summary":"Score resets","repro":"1. Play","expected":"Keeps score",'
            '"spec":"### Unity console\\n```\\nError: x\\n```","no_dispatch":true}}')

    def test_a_bug_from_the_editor_becomes_a_planned_job_that_waits(self):
        session = MagicMock()
        session.summary.return_value = {"id": "run-1"}
        with patch.object(self.server.sessions, "start", return_value=session) as start, \
             patch.object(ui.UIHandler, "_watch_for_created_job"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("POST", "/api/runs", body=self.BODY.encode(), headers={"Authorization": "Bearer test-token", **UI_HEADERS})
            res = conn.getresponse()
            data = json.loads(res.read())
            conn.close()
        self.assertEqual(res.status, 201, data)
        argv = start.call_args.args[2]
        self.assertIn("--no-dispatch", argv)
        self.assertIn("Score resets", argv)
        spec = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertIn("SUMMARY: Score resets", spec)
        self.assertIn("1. Play", spec)
        self.assertIn("### Unity console\n```\nError: x\n```", spec)

    def test_the_editor_reads_the_project_and_jobs(self):
        res, state = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(state["project"]["name"], "Demo")
        res, jobs = self.request("GET", "/api/jobs")
        self.assertEqual(res.status, 200)
        for job in jobs["jobs"]:
            self.assertIn("title", job)
            self.assertIn("label", job["state"])

    def test_a_wrong_token_is_the_401_the_editor_explains(self):
        res, _ = self.request("GET", "/api/state", auth=False)
        self.assertEqual(res.status, 401)



if __name__ == "__main__":
    unittest.main()
