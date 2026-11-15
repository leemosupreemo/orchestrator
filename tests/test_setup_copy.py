from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import setup_copy  # noqa: E402


class SetupCopyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime = Path(self._tmp.name) / ".orchestrator"
        (self.runtime / "config").mkdir(parents=True)

    def write(self, rel, data):
        path = self.runtime / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def read(self, rel):
        return json.loads((self.runtime / rel).read_text())

    def test_export_shares_settings_but_never_keys_or_local_paths(self):
        self.write("project.json", {"build_command": "make", "asc_key_path": "/Users/me/AuthKey.p8", "asc_key_id": "K1",
                                    "derived_data_path": "/Users/me/dd", "xcode_workspace": "/Users/me/App.xcworkspace",
                                    "xcode_project": "App.xcodeproj", "remote_logs": {"token": "secret"}})
        self.write("config/settings.json", {"openai_api_key": "sk-secret", "smtp_password": "pw", "notification_webhook": "https://hooks/x",
                                            "notification_emails": ["a@b.co"], "default_models": {"builder": "claude-sonnet-4-6"}})
        profile = setup_copy.export_profile(self.runtime)
        self.assertEqual(profile["project"], {"build_command": "make", "xcode_project": "App.xcodeproj"})
        self.assertEqual(sorted(profile["settings"]), ["default_models", "notification_emails"])
        self.assertNotIn("sk-secret", json.dumps(profile))
        self.assertNotIn("secret", json.dumps(profile))

    def test_export_includes_custom_prompts_only(self):
        self.write("prompts/reviewer.md", "Be strict.")
        self.write("prompts/notes.txt", "ignored")
        self.write("prompts/empty.md", "  ")
        self.assertEqual(setup_copy.export_profile(self.runtime)["prompts"], {"reviewer.md": "Be strict."})

    def test_apply_writes_whitelisted_values_and_keeps_local_ones(self):
        self.write("project.json", {"build_command": "old", "asc_key_path": "/local/key.p8"})
        self.write("config/settings.json", {"openai_api_key": "mine"})
        profile = {"project": {"build_command": "make", "asc_key_path": "/theirs/key.p8", "xcode_project": "/abs/App.xcodeproj"},
                   "settings": {"notification_emails": ["a@b.co"], "openai_api_key": "theirs"},
                   "prompts": {"reviewer.md": "Be strict.", "../evil.md": "x"}}
        applied = setup_copy.apply_profile(self.runtime, profile, "Studio", {"items": [{"id": "llm", "done": True}]}, now=5)
        self.assertEqual(applied, {"project": ["build_command"], "settings": ["notification_emails"], "prompts": ["reviewer.md"]})
        self.assertEqual(self.read("project.json"), {"build_command": "make", "asc_key_path": "/local/key.p8"})
        settings = self.read("config/settings.json")
        self.assertEqual((settings["openai_api_key"], settings["notification_emails"]), ("mine", ["a@b.co"]))
        self.assertEqual(settings["setup_copied_from"], {"name": "Studio", "at": 5, "ready": ["llm"]})
        self.assertEqual((self.runtime / "prompts" / "reviewer.md").read_text(), "Be strict.\n")
        self.assertFalse((self.runtime.parent / "evil.md").exists())

    def test_apply_refuses_an_empty_profile(self):
        with self.assertRaises(ValueError):
            setup_copy.apply_profile(self.runtime, {"project": {}, "settings": {}, "prompts": {}}, "Studio")

    def test_further_along_computers_come_first_and_those_with_nothing_to_copy_are_left_out(self):
        mine = {"required_done": 3, "required_total": 6, "complete": False}
        sources = [{"name": "Behind", "has_config": True, "progress": {"required_done": 1, "required_total": 6}, "synced_at": 1},
                   {"name": "Empty", "has_config": False, "progress": {"required_done": 6, "required_total": 6, "complete": True}},
                   {"name": "Ahead", "has_config": True, "progress": {"required_done": 5, "required_total": 6}, "synced_at": 1}]
        listed = setup_copy.with_ahead(sources, mine)
        self.assertEqual([(s["name"], s["ahead"]) for s in listed], [("Ahead", True), ("Behind", False)])

    def test_annotate_flags_what_is_done_on_the_source_but_not_here(self):
        setup = {"items": [{"id": "project", "done": True, "detail": "ok"}, {"id": "github_cli", "done": False, "detail": "Not signed in"}]}
        setup_copy.annotate(setup, {"setup_copied_from": {"name": "Studio", "at": 1, "ready": ["project", "github_cli"]}})
        self.assertEqual(setup["items"][0]["detail"], "ok")
        self.assertIn("Done on Studio", setup["items"][1]["detail"])
        self.assertTrue(setup["items"][1]["done_on_source"])
        self.assertEqual(setup["copied_from"]["name"], "Studio")
        untouched = {"items": [{"id": "x", "done": False, "detail": "d"}]}
        self.assertEqual(setup_copy.annotate(untouched, {}), {"items": [{"id": "x", "done": False, "detail": "d"}]})


class ServerHelperTests(unittest.TestCase):
    """The web server's side: publish, list and copy go through the account with this computer's pairing."""

    def setUp(self):
        from orchestrator.web import server as ui
        self.ui = ui
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / ".orchestrator" / "config").mkdir(parents=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"build_command": "make", "test_command": "make test"}))
        ui._setup_published.clear()
        self.calls = []

    def fake_call(self, replies):
        def call(path, body=None, machine=None, timeout=15):
            self.calls.append((path, body))
            return replies[path]
        return call

    def test_without_an_account_nothing_is_shared_or_offered(self):
        from unittest.mock import patch
        with patch.object(self.ui.account, "load_machine", return_value=None):
            self.ui.publish_setup_async(self.root, {"items": []})
            self.assertEqual(self.ui.setup_sources(self.root), {"enabled": False, "sources": []})
            with self.assertRaises(self.ui.UIError):
                self.ui.copy_setup_from(self.root, "m1")

    def test_copy_applies_the_sources_profile_and_remembers_it(self):
        from unittest.mock import patch
        reply = {"name": "Studio", "progress": {"items": [{"id": "github_cli", "done": True}]},
                 "profile": {"project": {"build_command": "xcodebuild"}, "settings": {"notification_emails": ["a@b.co"]}}}
        with patch.object(self.ui.account, "load_machine", return_value={"machine_id": "me"}), \
                patch.object(self.ui.account, "call", self.fake_call({"machine/setup/get": reply})):
            result = self.ui.copy_setup_from(self.root, "studio-id")
        self.assertEqual((result["ok"], result["from"], result["applied"]["project"]), (True, "Studio", ["build_command"]))
        runtime = self.root / ".orchestrator"
        self.assertEqual(json.loads((runtime / "project.json").read_text())["build_command"], "xcodebuild")
        settings = json.loads((runtime / "config" / "settings.json").read_text())
        self.assertEqual((settings["setup_copied_from"]["name"], settings["setup_copied_from"]["ready"]), ("Studio", ["github_cli"]))
        self.assertEqual(self.calls[0][1]["machine_id"], "studio-id")


if __name__ == "__main__":  # noqa: E305
    unittest.main()
