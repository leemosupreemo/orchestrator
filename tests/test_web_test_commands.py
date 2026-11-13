"""The Tests page's extra test commands and the Delivery page's platform, saved from the web UI into project.json."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_ui import UI_HEADERS, ServerTestCase, ui  # noqa: E402


class WebProjectConfigTests(ServerTestCase):
    def post(self, path, body=None):
        return self.request("POST", path, body or {}, headers=UI_HEADERS)

    def project(self):
        return json.loads((self.root / ".orchestrator" / "project.json").read_text())

    # ---- test commands

    def test_a_command_is_added_changed_and_removed_in_project_json_and_nothing_else_changes(self):
        form = {"name": "server", "command": "python3 t.py", "tier": "manual", "timeout": "900",
                "requires": "docker\ncontainer:srv", "results": "out/*.trx"}
        res, data = self.post("/api/test-commands", form)
        self.assertEqual((res.status, data["count"]), (200, 1))
        saved = self.project()
        self.assertEqual((saved["project_name"], saved["scheme"]), ("Demo", "Demo"))
        self.assertEqual(saved["test_commands"], [{"name": "server", "command": "python3 t.py", "timeout": 900,
                                                   "tier": "manual", "requires": ["docker", "container:srv"], "results": "out/*.trx"}])
        self.post("/api/test-commands", {"name": "unit", "command": "make test"})
        self.post("/api/test-commands", {**form, "original": "server", "name": "srv2", "command": "python3 t2.py"})
        self.assertEqual([c["name"] for c in self.project()["test_commands"]], ["srv2", "unit"])
        self.assertEqual(self.project()["test_commands"][1], {"name": "unit", "command": "make test"})  # defaults aren't written
        self.post("/api/test-commands", {"remove": "srv2"})
        self.post("/api/test-commands", {"remove": "unit"})
        self.assertNotIn("test_commands", self.project())

    def test_bad_commands_are_refused_with_the_reason_and_not_saved(self):
        self.post("/api/test-commands", {"name": "a", "command": "x"})
        for bad, reason in (({"name": "a", "command": "y"}, "used twice"), ({"name": "b c", "command": "x"}, "name may only"),
                            ({"name": "b", "command": ""}, "needs a command"), ({"name": "b", "command": "x", "timeout": "soon"}, "timeout"),
                            ({"name": "b", "command": "x", "retries": "9"}, "retries"), ({"name": "b", "command": "x", "tier": "weekly"}, "tier"),
                            ({"name": "b", "command": "x", "requires": "gpu"}, "unknown requirement"),
                            ({"name": "b", "command": "x", "cwd": "../x"}, "outside the project"),
                            ({"name": "b", "command": "x", "results": "/etc/x.xml"}, "inside the project")):
            res, data = self.post("/api/test-commands", bad)
            self.assertEqual(res.status, 400, bad)
            self.assertIn(reason, data["error"], bad)
        self.assertEqual(len(self.project()["test_commands"]), 1)

    def test_the_tests_page_lists_commands_with_what_is_missing_and_the_latest_result(self):
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({
            "project_name": "Demo", "scheme": "Demo",
            "test_commands": [{"name": "server", "command": "x", "tier": "manual", "requires": ["env:NO_SUCH_VARIABLE_XYZ"]},
                              {"name": "unit", "command": "y"}]}))
        folder = self.root / ".orchestrator" / "output" / "manual-tests" / "20261112-120000"
        folder.mkdir(parents=True)
        (folder / "test_commands.json").write_text(json.dumps([
            {"name": "unit", "status": "passed", "seconds": 1.5, "tests": {"total_run": 10, "failed_count": 0}, "output": "SECRET"}]))
        inventory = {"suites": [], "plans": [], "coverage": None, "frameworks": {}, "commands": [
            {"name": "server", "command": "x", "tier": "manual", "unmet": ["NO_SUCH_VARIABLE_XYZ isn't set"], "last": None, "editable": True},
            {"name": "unit", "command": "y", "tier": "per-job", "unmet": [], "last": None, "editable": True}]}
        with patch.object(ui.UIHandler, "_tests", return_value=inventory):
            res, data = self.request("GET", "/api/tests")
        by_name = {c["name"]: c for c in data["commands"]}
        self.assertIn("isn't set", by_name["server"]["unmet"][0])
        self.assertIsNone(by_name["server"]["last"])
        self.assertEqual((by_name["unit"]["last"]["status"], by_name["unit"]["last"]["job"]), ("passed", ""))
        self.assertNotIn("SECRET", json.dumps(data))  # command output isn't sent to the page

    def test_running_a_command_goes_through_run_tests_and_a_name_cannot_become_an_option(self):
        argv = ui.ACTIONS["test_command"].build({"name": "server"}, self.root)
        self.assertEqual(argv[-3:], ["run-tests", "--", "server"])
        with self.assertRaises(ui.UIError):
            ui.ACTIONS["test_command"].build({"name": "x; rm -rf /"}, self.root)

    # ---- delivery target

    def test_the_delivery_platform_is_saved_validated_and_only_the_chosen_platforms_fields_are_kept(self):
        res, data = self.post("/api/delivery/target", {"delivery_platform": "android", "firebase_app_id": "1:1:android:a",
                                                       "delivery_artifact": "out/*.apk", "delivery_build_command": "make apk",
                                                       "firebase_hosting_target": "ignored", "automatic": True})
        self.assertEqual(res.status, 200)
        saved = self.project()
        self.assertEqual((saved["delivery_platform"], saved["firebase_app_id"], saved["firebase_distribution"], saved["delivery_provider"]),
                         ("android", "1:1:android:a", True, "firebase"))
        self.assertNotIn("firebase_hosting_target", saved)
        self.assertEqual((saved["project_name"], saved["scheme"]), ("Demo", "Demo"))
        self.assertEqual(data["target"]["delivery_artifact"], "out/*.apk")
        res, data = self.post("/api/delivery/target", {"delivery_platform": "ios"})
        saved = self.project()
        self.assertEqual(saved["delivery_platform"], "ios")
        self.assertNotIn("firebase_app_id", saved)  # leaving Android clears its fields
        self.assertTrue(saved["firebase_distribution"])  # iOS keeps the wizard's choice

    def test_incomplete_or_unknown_platforms_are_refused(self):
        for body, reason in (({"delivery_platform": "android"}, "firebase_app_id"), ({"delivery_platform": "web"}, "firebase.json"),
                             ({"delivery_platform": "steam"}, "Choose one of"),
                             ({"delivery_platform": "android", "firebase_app_id": "a", "delivery_artifact": "../x.apk"}, "inside the project")):
            res, data = self.post("/api/delivery/target", body)
            self.assertEqual(res.status, 400, body)
            self.assertIn(reason, data["error"], body)
        self.assertNotIn("delivery_platform", self.project())

    def test_the_delivery_page_reports_the_target_and_android_projects_are_not_told_they_are_not_phone_apps(self):
        self.post("/api/delivery/target", {"delivery_platform": "android", "firebase_app_id": "1:1:android:a", "delivery_artifact": "o/*.apk"})
        with patch.object(ui.shutil, "which", return_value="/usr/bin/firebase"):
            gaps = ui.distribution_gaps(self.root, self.project())
        self.assertEqual(gaps, [])  # complete; automatic delivery is off, which is allowed
        with patch.object(ui.shutil, "which", return_value=None):
            self.assertTrue(any("Firebase CLI" in g for g in ui.distribution_gaps(self.root, self.project())))
        _, overview = self.request("GET", "/api/delivery")
        self.assertEqual(overview["target"]["delivery_platform"], "android")

    def test_a_unity_project_without_an_xcode_project_is_pointed_at_its_own_script_not_the_wizard(self):
        write = lambda rel, text: ((self.root / rel).parent.mkdir(parents=True, exist_ok=True), (self.root / rel).write_text(text))  # noqa: E731
        write("ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.30f1\n")
        write("Assets/Editor/Build.cs", "class B { void Go() { var t = BuildTarget.iOS; } }")
        with patch.object(ui.shutil, "which", return_value="/usr/bin/firebase"):
            gaps = ui.distribution_gaps(self.root, self.project())
        self.assertEqual(len(gaps), 1)
        self.assertIn("This is a Unity project", gaps[0])
        self.assertIn("your own script builds", gaps[0])
        self.assertNotIn("wizard", gaps[0])
        _, overview = self.request("GET", "/api/delivery")
        self.assertTrue(overview["target"]["unity_without_xcode"])
        res, data = self.post("/api/delivery/target", {"delivery_platform": "file", "delivery_artifact": ".local/r-*/ios/*.ipa",
                                                       "firebase_app_id": "1:1:ios:a", "delivery_build_command": "tools/package_ios.sh"})
        self.assertEqual(res.status, 200)
        saved = self.project()
        self.assertEqual((saved["delivery_platform"], saved["firebase_distribution"]), ("file", False))  # no automatic uploads
        with patch.object(ui.shutil, "which", return_value="/usr/bin/firebase"):
            self.assertEqual(ui.distribution_gaps(self.root, saved), [])  # ready to send by hand, with nothing automatic

    def test_members_cannot_change_either(self):
        with patch.object(ui.UIHandler, "_is_owner", return_value=False):
            self.assertEqual(self.post("/api/test-commands", {"name": "a", "command": "x"})[0].status, 403)
            self.assertEqual(self.post("/api/delivery/target", {"delivery_platform": "ios"})[0].status, 403)
        self.assertNotIn("test_commands", self.project())


class PhoneTitleRecursionTests(ServerTestCase):
    def test_filling_the_phone_titles_own_select_does_not_rebuild_the_title(self):
        """setMobileProjectTitle calls renderProjectSelect on its new select; if that rebuilt the title again the page
        would recurse until the stack overflowed and stay blank."""
        _, js = self.request("GET", "/app.js", auth=False)
        js = js.decode() if isinstance(js, bytes) else js
        body = js[js.index("function renderProjectSelect"):js.index("function setMobileProjectTitle")]
        self.assertIn('!select.classList.contains("mobile-project-select")', body)


class ProjectSwitchTests(ServerTestCase):
    def test_picking_a_project_shows_its_skeleton_before_the_server_answers_and_nothing_paints_the_old_page(self):
        _, js = self.request("GET", "/app.js", auth=False)
        js = js.decode() if isinstance(js, bytes) else js
        handler = js[js.index('document.addEventListener("change", async (e) => {\n  if (!e.target.matches("#project-select'):]
        handler = handler[:handler.index("// Shown from the click on a provider")]
        # the switching flag and the skeleton come before the first await on the server
        self.assertLess(handler.index("projectSwitching = mine"), handler.index("await projectDownloads.select(val)"))
        self.assertLess(handler.index("showProjectSwitching(picked)"), handler.index("await projectDownloads.select(val)"))
        route = js[js.index("async function route() {"):][:400]
        self.assertIn("if (projectSwitching) { showProjectSwitching(projectSwitching.name); return; }", route)
        tick = js[js.index("async function tick() {"):][:300]
        self.assertIn("if (projectSwitching) return;", tick)
        self.assertIn('loadingSkeleton("home")', js[js.index("function showProjectSwitching"):][:900])


class ComputerCardStateTests(ServerTestCase):
    def test_computers_are_blue_when_online_and_gray_when_offline_using_existing_colors(self):
        static = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
        account = (static / "account.js").read_text()
        self.assertIn('<article class="account-machine ${m.online ? "is-online" : "is-offline"}">', account)
        css = (static / "style.css").read_text()
        self.assertIn(".account-machine.is-online { background: var(--accent-soft); border-color: var(--accent); }", css)
        offline = css[css.index(".account-machine.is-offline {"):][:700]
        self.assertIn("var(--panel-2)", offline)
        self.assertIn("var(--muted)", offline)


class TesterGroupIsNotClearedByIosTests(ServerTestCase):
    def test_saving_ios_leaves_an_existing_tester_group_alone_but_android_and_own_builds_set_it(self):
        path = self.root / ".orchestrator" / "project.json"
        path.write_text(json.dumps({"project_name": "Demo", "firebase_groups": "qa-team"}))
        res, _ = self.request("POST", "/api/delivery/target", {"delivery_platform": "ios"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(path.read_text())["firebase_groups"], "qa-team")
        self.request("POST", "/api/delivery/target", {"delivery_platform": "file", "delivery_artifact": "o/*.ipa", "firebase_app_id": "1:1:ios:a",
                                                      "firebase_groups": "daoc-testers"}, headers=UI_HEADERS)
        self.assertEqual(json.loads(path.read_text())["firebase_groups"], "daoc-testers")


class PlatformEditorTests(ServerTestCase):
    def post(self, body):
        return self.request("POST", "/api/platforms", body, headers=UI_HEADERS)

    def setUp(self):
        super().setUp()
        (self.root / "app").mkdir()
        (self.root / "server").mkdir()
        self.path = self.root / ".orchestrator" / "project.json"

    def saved(self):
        return json.loads(self.path.read_text())

    def test_platforms_are_saved_cleaned_and_the_rest_of_the_config_is_kept(self):
        res, data = self.post({"platforms": [{"name": "Game app", "kind": "client", "paths": ["app/"]},
                                             {"name": "Game server", "kind": "server", "paths": ["server"]}]})
        self.assertEqual((res.status, data["count"], data["automatic"]), (200, 2, False))
        saved = self.saved()
        self.assertEqual(saved["platforms"][0], {"name": "Game app", "kind": "client", "paths": ["app"]})
        self.assertEqual(saved["project_name"], "Demo")

    def test_going_back_to_automatic_removes_the_list(self):
        self.post({"platforms": [{"name": "A", "paths": ["app"]}]})
        res, data = self.post({"reset": True})
        self.assertEqual((res.status, data["automatic"]), (200, True))
        self.assertNotIn("platforms", self.saved())

    def test_bad_lists_are_refused_with_the_reason_and_nothing_is_saved(self):
        for body, reason in (({"platforms": [{"name": "A", "paths": ["missing"]}]}, "doesn't exist"),
                             ({"platforms": [{"name": "A", "paths": ["../x"]}]}, "inside the project"),
                             ({"platforms": []}, "Add at least one platform"), ({"platforms": "x"}, "list of up to 30")):
            res, data = self.post(body)
            self.assertEqual(res.status, 400, body)
            self.assertIn(reason, data["error"], body)
        self.assertNotIn("platforms", self.saved())

    def test_members_cannot_change_them(self):
        with patch.object(ui.UIHandler, "_is_owner", return_value=False):
            self.assertEqual(self.post({"reset": True})[0].status, 403)

    def test_check_config_reports_a_bad_platform_list(self):
        from orchestrator.config_validation import validate_project_config
        from orchestrator.project_config import load_project_config
        self.path.write_text(json.dumps({"project_name": "Demo", "build_command": "b", "test_command": "t",
                                         "platforms": [{"name": "A", "paths": ["missing"]}]}))
        errors = validate_project_config(load_project_config(self.root))
        self.assertTrue(any("doesn't exist in the project" in e for e in errors), errors)
