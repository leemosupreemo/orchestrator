from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

from orchestrator import ux_review as ux  # noqa: E402


class ProductReviewProgressTests(unittest.TestCase):
    def test_review_reports_each_real_stage_before_doing_the_work(self):
        import ux_review_run as runner
        from orchestrator.web.run_progress import RunProgress

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            observed = []

            def observe_stage(*args, **kwargs):
                progress = RunProgress()
                progress.feed(output.getvalue().encode())
                observed.append(progress.snapshot()["step"])
                return ux.parse_result('{}'), '{}', 'fixture-model'

            def capture_stage(*args, **kwargs):
                progress = RunProgress()
                progress.feed(output.getvalue().encode())
                observed.append(progress.snapshot()["step"])
                return [], []

            with (
                patch.object(runner, "ROOT", root),
                patch.object(runner, "OUTPUT_DIR", root / "output"),
                patch.object(runner, "project_settings", return_value=ux.settings({})),
                patch.object(runner, "git_out", return_value=""),
                patch.object(runner, "capture", side_effect=capture_stage),
                patch.object(runner, "review", side_effect=observe_stage),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(runner.product_pass(False, "fixture-model"), 0)

            self.assertEqual(observed, [1, 2, 3])
            progress = RunProgress()
            progress.feed(output.getvalue().encode())
            self.assertEqual((progress.snapshot()["step"], progress.snapshot()["total"]), (4, 4))
            self.assertEqual(progress.snapshot()["label"], "Save report")
            self.assertEqual(len(list((root / "output" / "ux-pass").glob("*/result.json"))), 1)


class ScreenshotEvidenceTests(unittest.TestCase):
    def run_change(self, *, review_enabled=True, review_error=False):
        import ux_review_run as runner
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "job.json"
        path.write_text(json.dumps({"job_id": "20261009-feature-1", "branch": "feature", "base_branch": "main", "title": "New settings screen"}))
        cfg = ux.settings({"ui_review": {"url": "http://localhost:3000", "review_changes": review_enabled}})
        shot = {"file": "settings-390-light.png", "route": "/settings", "width": 390, "dark": False}

        def capture(_cfg, folder, **kwargs):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / shot["file"]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            return [shot], []

        with patch.object(runner, "ROOT", root), patch.object(runner, "OUTPUT_DIR", root / "output"), \
             patch.object(runner, "project_settings", return_value=cfg), \
             patch.object(runner, "git_out", return_value="src/pages/Settings.tsx"), \
             patch.object(runner, "capture", side_effect=capture) as captured, \
             patch.object(runner, "review", side_effect=RuntimeError("Reviewer unavailable") if review_error else None,
                          return_value=(ux.parse_result('{}'), '{}', 'fixture')) as reviewer, \
             contextlib.redirect_stdout(io.StringIO()):
            runner.job_review(path)
        return root, json.loads(path.read_text()), captured.call_count, reviewer.call_count

    def test_capture_is_saved_even_if_the_reviewer_fails(self):
        root, job, captures, _ = self.run_change(review_error=True)
        evidence = json.loads((root / "output/20261009-feature-1/ux-review/screens.json").read_text())
        self.assertEqual(evidence["screens"][0]["route"], "/settings")
        self.assertEqual(job["ux_review"]["screens"], 1)
        self.assertEqual(captures, 1)

    def test_disabling_ai_review_does_not_disable_screenshot_evidence(self):
        root, job, captures, reviews = self.run_change(review_enabled=False)
        self.assertEqual((captures, reviews), (1, 0))
        self.assertTrue((root / "output/20261009-feature-1/ux-review/screens.json").exists())
        self.assertEqual(job["ux_review"]["screens"], 1)

    def test_colliding_route_names_archive_as_distinct_screens(self):
        import ux_review_run as runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "job.json"
            path.write_text(json.dumps({"job_id": "route-collision", "branch": "feature", "base_branch": "main"}))
            cfg = ux.settings({"ui_review": {"url": "http://localhost:3000", "routes": ["/settings/profile", "/settings-profile", "/settings/profile"], "widths": [390, 390], "dark_mode": False, "review_changes": False}})
            with patch.object(runner, "ROOT", root), patch.object(runner, "OUTPUT_DIR", root / "output"), \
                 patch.object(runner, "project_settings", return_value=cfg), \
                 patch.object(runner, "git_out", return_value="src/pages/Settings.tsx"), \
                 patch.object(ux, "find_browser", return_value="fixture"), patch.object(ux, "reachable", return_value=True), \
                 patch.object(ux, "Browser") as browser, contextlib.redirect_stdout(io.StringIO()):
                browser.return_value.__enter__.return_value.screenshot.return_value = b"fixture PNG"
                runner.job_review(path)
                runner.job_review(path)
            folder = root / "output/route-collision/ux-review"
            records = [json.loads(p.read_text()) for p in (folder / "captures").glob("*.json")]
            self.assertEqual(len(records), 2)
            files = [s["file"] for record in records for s in record["screens"]]
            self.assertEqual(len(files), 4)
            self.assertEqual(len(set(files)), 4)
            self.assertTrue(all((folder / "screens" / name).is_file() for name in files))

    def test_failed_screen_state_keeps_successful_captures_and_reports_missing_state(self):
        cfg = ux.capture_plan(ux.settings({"ui_review": {"url": "http://localhost:3000", "widths": [390], "dark_mode": False}}),
                              {"screens": [{"name": "Edit dialog", "route": "/", "actions": [{"click": "#missing"}]}]})
        with tempfile.TemporaryDirectory() as tmp, patch.object(ux, "find_browser", return_value="fixture"), \
             patch.object(ux, "reachable", return_value=True), patch.object(ux, "Browser") as browser:
            browser.return_value.__enter__.return_value.screenshot.side_effect = [b"fixture PNG", ux.BrowserError("missing selector")]
            shots, problem = ux.capture_web(Path(tmp), cfg, Path(tmp), log=lambda *_: None)
            self.assertEqual(len(shots), 1)
            self.assertTrue((Path(tmp) / shots[0]["file"]).is_file())
            self.assertIn("Missing screenshots: Edit dialog", problem)

    def test_navigation_error_does_not_capture_a_browser_error_page(self):
        browser = ux.Browser("fixture", "fixture", settle=0)
        with patch.object(browser, "call", side_effect=[{}, {}, {"errorText": "net::ERR_CONNECTION_REFUSED"}]) as calls:
            with self.assertRaisesRegex(ux.BrowserError, "Navigation failed"):
                browser.screenshot("http://localhost:3000", 390, False)
            self.assertNotIn("Page.captureScreenshot", [call.args[0] for call in calls.call_args_list])

    def test_capture_plan_adds_new_routes_and_modal_states(self):
        cfg = ux.settings({"ui_review": {"url": "http://localhost:3000", "routes": ["/"]}})
        expanded = ux.capture_plan(cfg, {"routes": ["/settings"], "screens": [
            {"name": "New item dialog", "route": "/settings", "actions": [{"click": "button.new-item"}]}]})
        self.assertEqual(expanded["routes"], ["/", "/settings"])
        self.assertEqual(expanded["scenarios"][0]["name"], "New item dialog")
        self.assertEqual(cfg["routes"], ["/"])
        with self.assertRaises(ux.ConfigError):
            ux.capture_plan(cfg, {"screens": [{"route": "https://other.example", "actions": []}]})


class FrontendDetectionTests(unittest.TestCase):
    def test_interface_files_are_picked_out_of_a_diff(self):
        changed = ["orchestrator/web/static/app.js", "orchestrator/web/server.py", "App/Views/HomeView.swift",
                   "App/Model/User.swift", "src/components/Button.tsx", "lib/util.ts", "styles/site.scss", "README.md"]
        self.assertEqual(ux.frontend_files(changed),
                         ["orchestrator/web/static/app.js", "App/Views/HomeView.swift", "src/components/Button.tsx", "styles/site.scss"])

    def test_configured_patterns_always_count(self):
        self.assertEqual(ux.frontend_files(["lib/theme.py"], ["lib/theme*"]), ["lib/theme.py"])


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        cfg = ux.settings({})
        self.assertEqual((cfg["url"], cfg["routes"], cfg["widths"], cfg["dark_mode"], cfg["review_changes"]),
                         ("", ["/"], [390, 1440], True, True))

    def test_values_that_cannot_work_are_explained(self):
        for bad in ({"url": "localhost:3000"}, {"routes": "/"}, {"widths": [100]}, {"paths": "src"}, "yes"):
            with self.assertRaises(ux.ConfigError):
                ux.settings({"ui_review": bad})

    def test_urls_and_file_names_for_each_screen(self):
        self.assertEqual(ux.screen_url("http://x:1/", "#/jobs"), "http://x:1#/jobs")
        self.assertEqual(ux.screen_url("http://x:1", "settings"), "http://x:1/settings")
        self.assertEqual(ux.screen_url("http://x:1", "https://other/"), "https://other/")
        self.assertEqual(ux.shot_name("#/jobs?filter=all", 390, True), "jobs-filter-all-390-dark.png")
        self.assertEqual(ux.shot_name("/", 1440, False), "home-1440-light.png")

    def test_the_projects_own_conventions_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(ux.conventions_text(root), ("", ""))
            (root / "docs").mkdir()
            (root / "docs" / "web-ui-conventions.md").write_text("One primary action per screen.")
            self.assertEqual(ux.conventions_text(root), ("docs/web-ui-conventions.md", "One primary action per screen."))
            self.assertEqual(ux.conventions_text(root, "../outside.md"), ("docs/web-ui-conventions.md", "One primary action per screen."))


class PromptAndResultTests(unittest.TestCase):
    def test_the_prompt_lists_every_checklist_item_the_code_knows(self):
        prompt = (PACKAGE_ROOT / "orchestrator" / "prompts" / "ux_reviewer.md").read_text()
        for item in ux.CHECK_IDS:
            self.assertIn(f"`{item}`", prompt, item)

    def test_every_item_the_prompt_asks_about_is_one_the_code_keeps(self):
        import re
        prompt = (PACKAGE_ROOT / "orchestrator" / "prompts" / "ux_reviewer.md").read_text()
        asked = set(re.findall(r"^- `((?:ux|design)\.[a-z-]+)`", prompt, re.M))
        self.assertEqual(asked, ux.CHECK_IDS)  # an item only in the prompt would be dropped from every result

    def test_the_card_navigation_and_content_principles_are_on_the_checklist(self):
        for item in ("ux.grouping", "ux.decision-context", "ux.menus", "ux.no-terminal", "ux.back", "ux.action-labels", "ux.long-content", "design.section-headers",
                     "design.header-anatomy", "design.fields", "design.native-controls"):
            self.assertIn(item, ux.CHECK_IDS)

    def test_screens_and_conventions_reach_the_prompt(self):
        text = ux.build_prompt("CHECKLIST", scope="One change", ask="Add a filter", conventions=("docs/ui.md", "Rule"),
                               shots=[{"file": "home-390-light.png", "route": "/", "width": 390, "dark": False}],
                               shot_dir=Path("/tmp/screens"), code="diff", limits=["No dark mode"])
        for part in ("/tmp/screens/home-390-light.png", "docs/ui.md", "Rule", "Add a filter", "No dark mode"):
            self.assertIn(part, text)

    def test_the_answer_is_normalised(self):
        answer = "Here you go:\n```json\n" + json.dumps({
            "summary": "Mostly fine.",
            "checklist": [{"id": "ux.one-primary", "status": "FAIL", "note": "Two filled buttons"},
                          {"id": "made.up", "status": "pass"}, {"id": "design.contrast", "status": "weird"}],
            "findings": [{"area": "ux", "item": "ux.one-primary", "severity": 9, "screen": "Home", "problem": "Two primaries", "fix": "Demote one"},
                         {"area": "design", "severity": "x", "problem": "Cramped"}, {"problem": ""}],
            "limits": "No dark mode"}) + "\n```"
        result = ux.parse_result(answer)
        by_id = {c["id"]: c for c in result["checklist"]}
        self.assertEqual(len(result["checklist"]), len(ux.CHECK_IDS))  # every item once, unknown ones dropped
        self.assertEqual((by_id["ux.one-primary"]["status"], by_id["design.contrast"]["status"], by_id["ux.forms"]["note"]),
                         ("fail", "n/a", "not reported"))
        self.assertEqual([(f["severity"], f["area"]) for f in result["findings"]], [(4, "ux"), (2, "design")])
        counts = ux.counts(result)
        self.assertEqual((counts["findings"], counts["major"], counts["failed"]), (2, 1, 1))

    def test_an_unreadable_answer_says_so(self):
        result = ux.parse_result("I looked and it is nice.")
        self.assertTrue(result["unreadable"])
        self.assertEqual(result["findings"], [])

    def test_report_and_fix_job(self):
        result = ux.parse_result(json.dumps({"summary": "S", "findings": [
            {"area": "design", "item": "design.overlap", "severity": 3, "screen": "New job, phone", "element": "+ button", "problem": "Covers a field", "fix": "Remove it"},
            {"area": "ux", "severity": 0, "problem": "Tiny thing"}]}))
        report = ux.report_markdown(result, title="Pass", scope="Whole product",
                                    shots=[{"file": "a.png", "route": "/", "width": 390, "dark": True}], when="2026-10-04 10:00")
        for part in ("# Pass", "## UX findings", "## Design findings", "**major** · New job, phone · + button (`design.overlap`): Covers a field Fix: Remove it",
                     "## Checklist", "(screens/a.png)"):
            self.assertIn(part, report)
        fix = ux.fix_job_text(result["findings"])
        self.assertIn("[major] New job, phone: Covers a field", fix)
        self.assertNotIn("Tiny thing", fix)  # cosmetic findings don't make the job


class CaptureTests(unittest.TestCase):
    def test_without_a_browser_capture_explains_what_to_do(self):
        cfg = ux.settings({"ui_review": {"url": "http://127.0.0.1:9/"}})
        with patch.object(ux, "find_browser", return_value=None), tempfile.TemporaryDirectory() as tmp:
            shots, problem = ux.capture_web(Path(tmp), cfg, Path(tmp) / "s", log=lambda *_: None)
        self.assertEqual(shots, [])
        self.assertIn("ORCHESTRATOR_BROWSER", problem)

    def test_an_app_that_is_not_running_and_has_no_start_command_is_explained(self):
        cfg = ux.settings({"ui_review": {"url": "http://127.0.0.1:9/"}})
        with patch.object(ux, "find_browser", return_value="/bin/true"), tempfile.TemporaryDirectory() as tmp:
            shots, problem = ux.capture_web(Path(tmp), cfg, Path(tmp) / "s", log=lambda *_: None)
        self.assertIn("isn't answering", problem)

    def test_each_model_cli_is_given_the_screenshots_its_own_way(self):
        from llm import image_args
        images = [Path("/tmp/s/a.png"), Path("/tmp/s/b.png")]
        self.assertEqual(image_args("codex exec", images), " -i /tmp/s/a.png -i /tmp/s/b.png")
        self.assertEqual(image_args("opencode run --model x", images), " -f /tmp/s/a.png -f /tmp/s/b.png")
        self.assertEqual(image_args("claude -p --model y", images), " --add-dir /tmp/s")
        self.assertEqual(image_args("gemini --model z --skip-trust --prompt - --yolo", images), " --include-directories /tmp/s")
        self.assertEqual(image_args("ollama run qwen", images), "")
        self.assertEqual(image_args("codex exec", None), "")


if __name__ == "__main__":
    unittest.main()
