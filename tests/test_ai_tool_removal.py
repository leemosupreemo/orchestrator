"""Removal tests use disposable installations and mocked package managers only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import ai_tool_removal as removal
from tests.test_web_ui import ServerTestCase, UI_HEADERS


class AiToolRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def executable(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def resolve_tools(self, binary, manager=None):
        def which(name, **kwargs):
            if name in ("npm", "brew"):
                return str(manager) if manager else None
            return str(binary) if binary.exists() else None
        return patch.object(removal.shutil, "which", side_effect=which)

    def test_native_binary_removed_but_settings_and_models_are_kept(self):
        binary = self.executable(".local/bin/agy")
        settings = self.home / ".gemini/settings.json"
        settings.parent.mkdir();settings.write_text('{"keep":true}')
        with self.resolve_tools(binary), patch.object(removal.subprocess, "run") as run:
            result = removal.uninstall_provider("agy")
        self.assertFalse(binary.exists())
        self.assertTrue(settings.exists())
        self.assertFalse(result["remaining"])
        run.assert_not_called()

    def test_native_claude_link_and_current_binary_removed_without_history(self):
        target = self.executable(".local/share/claude/versions/1.0")
        binary = self.home / ".local/bin/claude"
        binary.parent.mkdir(parents=True);binary.symlink_to(target)
        history = self.home / ".claude/history.jsonl";history.parent.mkdir();history.write_text("history")
        with self.resolve_tools(binary):removal.uninstall_provider("claude")
        self.assertFalse(binary.is_symlink());self.assertFalse(target.exists());self.assertTrue(history.exists())

    def test_npm_uses_the_detected_global_prefix_and_fixed_package(self):
        target = self.executable("npm/lib/node_modules/@google/gemini-cli/bin/gemini.js")
        (target.parent.parent / "package.json").write_text(json.dumps({"name":"@google/gemini-cli"}))
        binary = self.home / "npm/bin/gemini";binary.parent.mkdir(parents=True);binary.symlink_to(target)
        manager = self.executable("npm/bin/npm")
        with self.resolve_tools(binary, manager), patch.object(removal.subprocess, "run") as run:
            run.return_value.returncode = 0
            removal.uninstall_provider("gemini")
        self.assertEqual(run.call_args.args[0], [str(manager), "uninstall", "--global", "--ignore-scripts", "--prefix", str(self.home / "npm"), "@google/gemini-cli"])

    def test_homebrew_uses_own_prefix_and_does_not_zap_data(self):
        target=self.executable("brew/Cellar/opencode/1.0/bin/opencode")
        binary=self.home / "brew/bin/opencode";binary.parent.mkdir(parents=True);binary.symlink_to(target)
        manager=self.executable("brew/bin/brew")
        with self.resolve_tools(binary,manager),patch.object(removal.subprocess,"run") as run:
            run.return_value.returncode=0;removal.uninstall_provider("opencode")
        self.assertEqual(run.call_args.args[0],[str(manager),"uninstall","--formula","opencode"])

    def test_unknown_provider_or_unrecognized_installation_cannot_remove_files(self):
        binary=self.executable("custom/gemini")
        with self.resolve_tools(binary),patch.object(removal.subprocess,"run") as run:
            for provider in ("gemini; touch /tmp/bad", "gemini"):
                with self.assertRaises(ValueError):removal.uninstall_provider(provider)
        self.assertTrue(binary.exists());run.assert_not_called()

    def test_native_link_cannot_remove_an_unrelated_target(self):
        target=self.executable("unrelated/my-files")
        binary=self.home / ".local/bin/agy";binary.parent.mkdir(parents=True);binary.symlink_to(target)
        with self.resolve_tools(binary),self.assertRaises(ValueError):removal.uninstall_provider("agy")
        self.assertTrue(target.exists());self.assertTrue(binary.is_symlink())

    def test_failed_package_manager_is_reported(self):
        target=self.executable("brew/Cellar/opencode/1.0/bin/opencode")
        binary=self.home / "brew/bin/opencode";binary.parent.mkdir(parents=True);binary.symlink_to(target)
        manager=self.executable("brew/bin/brew")
        with self.resolve_tools(binary,manager),patch.object(removal.subprocess,"run") as run:
            run.return_value.returncode=1;run.return_value.stderr="Permission denied"
            with self.assertRaisesRegex(RuntimeError,"Permission denied"):removal.uninstall_provider("opencode")
        self.assertTrue(binary.exists())


class AiToolRemovalEndpointTests(ServerTestCase):
    def test_uninstall_requires_explicit_confirmation_and_known_provider(self):
        with patch.object(removal, "uninstall_provider") as uninstall:
            for body in ({"provider":"codex"}, {"provider":"codex","confirmed":"true"},
                         {"provider":"bad;command","confirmed":True}):
                response, _ = self.request("POST", "/api/ai-providers/uninstall", body=body, headers=UI_HEADERS)
                self.assertEqual(response.status, 400)
        uninstall.assert_not_called()

    def test_active_work_blocks_uninstall(self):
        release = self.server.gate.hold()
        try:
            with patch.object(removal, "uninstall_provider") as uninstall:
                response, data = self.request("POST", "/api/ai-providers/uninstall", body={"provider":"codex","confirmed":True}, headers=UI_HEADERS)
                self.assertEqual(response.status, 409, data)
            uninstall.assert_not_called()
        finally:
            release()

    def test_confirmed_uninstall_runs_in_a_task_and_releases_admission(self):
        result={"provider":"Codex","detail":"Uninstalled","remaining":False}
        with patch.object(removal, "uninstall_provider", return_value=result) as uninstall:
            response, data = self.request("POST", "/api/ai-providers/uninstall", body={"provider":"codex","confirmed":True}, headers=UI_HEADERS)
            self.assertEqual(response.status, 202, data)
            with self.server.tasks.condition:
                self.server.tasks.condition.wait_for(lambda:self.server.tasks._items[data["task"]]["status"] != "running", timeout=3)
            task = self.server.tasks.get(data["task"])
            self.assertEqual(task["result"], result)
            self.assertEqual(self.server.gate.snapshot()["active"], 0)
            uninstall.assert_called_once_with("codex")
