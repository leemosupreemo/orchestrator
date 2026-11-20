from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.scripts import web_preview
from orchestrator.web import server as ui

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class WebPreviewScriptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_branch_names_become_valid_channel_ids(self):
        self.assertEqual(web_preview.channel_name("feature/Login_Page"), "feature-login-page")
        self.assertEqual(web_preview.channel_name("///"), "preview")
        self.assertLessEqual(len(web_preview.channel_name("a" * 100)), 36)

    def test_it_reads_the_url_from_firebases_reply(self):
        reply = 'i  hosting: deploying\n{"status": "success", "result": {"site": {"site": "site", "url": "https://site--b-x.web.app", "expireTime": "2026-11-16T00:00:00Z"}}}'
        self.assertEqual(web_preview.preview_url(reply), ("https://site--b-x.web.app", "2026-11-16T00:00:00Z"))
        with self.assertRaises(ValueError):
            web_preview.preview_url('{"status": "error", "error": "Not signed in"}')

    def test_it_builds_with_the_projects_own_build(self):
        self.assertEqual(web_preview.build_commands(self.root, {}), [])  # plain static site: nothing to build
        (self.root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
        self.assertEqual(web_preview.build_commands(self.root, {}), [["npm", "install"], ["npm", "run", "build"]])
        (self.root / "package-lock.json").write_text("{}")
        self.assertEqual(web_preview.build_commands(self.root, {})[0], ["npm", "ci"])
        self.assertEqual(web_preview.build_commands(self.root, {"preview_build_command": "make site"}), [["/bin/sh", "-c", "make site"]])

    def test_firebase_project_comes_from_project_json_or_firebaserc(self):
        self.assertEqual(web_preview.firebase_project(self.root, {}), "")
        (self.root / ".firebaserc").write_text(json.dumps({"projects": {"default": "my-site"}}))
        self.assertEqual(web_preview.firebase_project(self.root, {}), "my-site")
        self.assertEqual(web_preview.firebase_project(self.root, {"firebase_project": "other"}), "other")


class WebProjectDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_web_projects_are_told_apart_from_phone_apps(self):
        self.assertFalse(ui.is_web_app(self.root, {}))
        (self.root / "package.json").write_text("{}")
        self.assertTrue(ui.is_web_app(self.root, {}))
        (self.root / "App.xcodeproj").mkdir()  # a phone app with a package.json is still a phone app
        self.assertFalse(ui.is_web_app(self.root, {}))

    def test_gaps_say_what_to_set_up_and_clear_when_ready(self):
        with patch.object(ui.shutil, "which", return_value=None):
            gaps = ui.web_preview_gaps(self.root, {})
        self.assertTrue(any("npm install -g firebase-tools" in g for g in gaps))
        self.assertTrue(any("firebase init hosting" in g for g in gaps))
        (self.root / "firebase.json").write_text(json.dumps({"hosting": {"public": "dist"}}))
        (self.root / ".firebaserc").write_text(json.dumps({"projects": {"default": "my-site"}}))
        with patch.object(ui.shutil, "which", return_value="/usr/local/bin/firebase"):
            self.assertEqual(ui.web_preview_gaps(self.root, {}), [])

    def test_the_action_takes_a_branch_and_confirms_first(self):
        argv = ui.build_web_preview({"branch": "feature/x"}, self.root)
        self.assertEqual(argv[-5:], ["web_preview.py", "--branch", "feature/x", "--root", str(self.root)])
        with self.assertRaises(ui.UIError):
            ui.build_web_preview({"branch": "--upload-pack=evil"}, self.root)
        self.assertIn("anyone with the link", ui.ACTIONS["web_preview"].confirm)

    def test_delivery_shows_a_preview_in_place_of_sending_to_devices(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('<h2>${web ? "Preview" : "Send to my phone"}</h2>', app)
        self.assertIn("${web ? previewBlock : !testers.configured ?", app)  # a web project shows the preview, never the send setup
        self.assertIn('runAction("web_preview", { branch: selected_branch })', app)
        self.assertIn('act("web_preview", { branch: s.branch })', app)  # the job's Share menu too


if __name__ == "__main__":
    unittest.main()


class ProjectsPageHeaderTests(unittest.TestCase):
    """The Projects page: no sentence under the title, and the filters (All, GitHub, Other Git hosts, On this computer) on a line
    of their own below "Your projects" instead of beside it."""

    def test_there_is_no_line_under_the_title(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app = (static / "app.js").read_text()
        self.assertNotIn("Manage and switch between codebases", app)
        view = app[app.index('title: "Projects",'):]
        self.assertTrue(view.startswith('title: "Projects",\n    sub: "",'))

    def test_the_filters_sit_under_the_heading(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        rule = css[css.index(".projects-head {"):].splitlines()[0]
        self.assertIn("flex-direction: column", rule)
        self.assertIn("align-items: flex-start", rule)
        self.assertNotIn("space-between", rule)  # not pushed to the far side of the heading


class ProjectKindTests(unittest.TestCase):
    """Projects are labelled Web, Desktop or Mobile wherever they're named."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def unity_project(self, folder="DaocUnity", targets=""):
        unity = self.root / folder
        (unity / "ProjectSettings").mkdir(parents=True)
        (unity / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.6f1\n")
        (unity / "Assets/Editor").mkdir(parents=True)
        (unity / "Assets/Editor/Build.cs").write_text(targets)
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"puppeteer": "^25.16.0"}}))

    def test_nested_unity_mobile_build_beats_node_tooling(self):
        self.unity_project(targets="target = BuildTarget.iOS; target = BuildTarget.StandaloneOSX;")
        self.assertEqual(ui.project_kind(self.root, {}), "mobile")
        self.assertTrue(ui.is_mobile_app(self.root, {}))
        self.assertFalse(ui.is_web_app(self.root, {}))
        self.assertEqual(ui.project_kind(self.root, {"project_kind": "desktop"}), "desktop")

    def test_unity_desktop_build_is_not_a_web_or_phone_app(self):
        self.unity_project(folder=".", targets="target = BuildTarget.StandaloneOSX;")
        self.assertEqual(ui.project_kind(self.root, {}), "desktop")
        self.assertFalse(ui.is_mobile_app(self.root, {}))
        self.assertFalse(ui.is_web_app(self.root, {}))

    def test_unity_without_build_targets_does_not_infer_web_from_tooling(self):
        self.unity_project()
        self.assertEqual(ui.project_kind(self.root, {}), "")
        self.assertFalse(ui.is_web_app(self.root, {}))

    def test_unity_webgl_build_can_be_a_web_app(self):
        self.unity_project(targets="target = BuildTarget.WebGL;")
        self.assertEqual(ui.project_kind(self.root, {}), "web")
        self.assertTrue(ui.is_web_app(self.root, {}))

    def test_unity_android_build_is_mobile_and_commented_targets_are_ignored(self):
        self.unity_project(targets="// BuildTarget.iOS\n/* BuildTarget.StandaloneOSX */\ntarget = BuildTarget.Android;")
        self.assertEqual(ui.project_kind(self.root, {}), "mobile")
        self.assertTrue(ui.is_mobile_app(self.root, {}))
        self.assertFalse(ui.is_web_app(self.root, {}))

    def test_commented_unity_build_target_does_not_choose_a_platform(self):
        self.unity_project(targets="// BuildTarget.iOS\n/* BuildTarget.StandaloneOSX */")
        self.assertEqual(ui.project_kind(self.root, {}), "")

    def xcode(self, sdk):
        (self.root / "App.xcodeproj").mkdir(exist_ok=True)
        (self.root / "App.xcodeproj" / "project.pbxproj").write_text(f"SDKROOT = {sdk};\n")

    def test_apple_projects_follow_the_platform_they_build_for(self):
        self.xcode("iphoneos")
        self.assertEqual(ui.project_kind(self.root, {}), "mobile")
        self.xcode("macosx")
        self.assertEqual(ui.project_kind(self.root, {}), "desktop")

    def test_package_json_tells_desktop_mobile_and_web_apart(self):
        (self.root / "package.json").write_text(json.dumps({"devDependencies": {"electron": "^30"}}))
        self.assertEqual(ui.project_kind(self.root, {}), "desktop")
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"react-native": "0.74"}}))
        self.assertEqual(ui.project_kind(self.root, {}), "mobile")
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"react": "19"}}))
        self.assertEqual(ui.project_kind(self.root, {}), "web")

    def test_android_and_unknown_projects_and_the_override(self):
        self.assertEqual(ui.project_kind(self.root, {}), "")  # nothing to go on: no label
        (self.root / "android").mkdir()
        self.assertEqual(ui.project_kind(self.root, {}), "mobile")
        self.assertEqual(ui.project_kind(self.root, {"project_kind": "Desktop"}), "desktop")  # project.json wins

    def test_the_label_shows_in_the_sidebar_picker_phone_header_and_project_cards(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app = (static / "app.js").read_text()
        self.assertIn('const KIND_LABELS = { web: "Web app", desktop: "Desktop app", mobile: "Mobile app" };', app)
        css = (static / "style.css").read_text()
        # Centred on the line it sits in, not dropped to the lowercase letters' middle
        self.assertIn(".project-switch > .label { display: flex; align-items: center;", css)
        self.assertIn(".mobile-title { display: flex; align-items: center; justify-content: center;", css)
        self.assertIn('<span class="mobile-title-name">', app)
        self.assertIn('${KIND_LABELS[o.kind] ? ` · ${KIND_LABELS[o.kind]}` : ""}', app)  # project picker
        self.assertEqual(app.count("setMobileProjectTitle("), 5)  # phone header also names a project while it downloads or switches (4 callers + definition)
        self.assertIn('<div class="project-card-badges">\n          ${kindPill(p.kind)}', app)
        self.assertIn('<span id="project-kind"></span>', (static / "index.html").read_text())
        server = (PACKAGE_ROOT / "orchestrator" / "web" / "server.py").read_text()
        self.assertGreaterEqual(server.count('"kind": project_kind('), 5)  # active project, picker entries, project cards

    def test_home_names_the_project_larger_and_switches_it_like_the_sidebar(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app, css = (static / "app.js").read_text(), (static / "style.css").read_text()
        self.assertIn('titleHtml: homeProjectTitle(p)', app)
        fn = app[app.index("function homeProjectTitle"):][:900]
        self.assertIn('class="home-project-select"', fn)
        self.assertIn('aria-label="Switch project"', fn)
        self.assertIn('renderProjectSelect($(".home-project-select"))', app)
        self.assertIn('e.target.matches("#project-select, .home-project-select, .mobile-project-select")', app)
        self.assertIn('body[data-page="home"] #page-title { font-size: var(--type-home); }', css)
        # Top bar titles are the page size plus 10% (the line under them is not), and Home's is 10% over that
        self.assertIn('--type-title: calc(var(--type-page) * 1.1);', css)
        self.assertIn('--type-home: calc(var(--type-title) * 1.1);', css)
        self.assertIn('.topbar h1 { font-size: var(--type-title); font-weight: var(--weight-bold); }', css)
        # No page heading on Product, Activity or Tests: the sidebar and the tab already say where you are
        self.assertIn('body[data-page="product"] #page-title, body[data-page="activity"] #page-title, body[data-page="tests"] #page-title { display: none; }', css)

    def test_the_phone_header_name_switches_projects_like_the_sidebar(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app, css = (static / "app.js").read_text(), (static / "style.css").read_text()
        fn = app[app.index("function setMobileProjectTitle"):][:700]
        self.assertIn('class="mobile-project-select"', fn)
        self.assertIn('aria-label="Switch project"', fn)
        self.assertIn('renderProjectSelect($(".mobile-project-select"))', fn)
        self.assertIn(".mobile-project-select { position: absolute; inset: 0;", css)

    def test_the_tests_page_counts_suites_in_their_card_and_says_how_to_measure_coverage(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        page = app[app.index("pages.tests = async"):][:9000]
        self.assertNotIn('sub: `${withTests.length} suites', page)
        self.assertIn('<h2>Suites <span class="count">${plural(withTests.length, "suite")} · ${plural(total, "test")}</span></h2>', page)
        self.assertIn('Not measured yet. ${matchMedia("(pointer: coarse)").matches ? "Tap" : "Click"} Calculate to populate.', page)
        self.assertIn('class="btn small${measured ? "" : " primary"}" id="recalc-coverage"', page)

    def test_the_phone_tab_bar_grows_the_chosen_icon_smoothly(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".tabbar a.active .icon { transform: scale(1.14); }", css)
        self.assertIn(".tabbar .icon { transform-origin: 50% 60%; transition: transform var(--motion-medium)", css)  # off with reduced motion


class QuietExportTests(unittest.TestCase):
    """Exporting a job's bundle is an errand, not work on the job: it runs in the background and the job stays idle."""

    def test_export_runs_do_not_make_the_job_busy(self):
        self.assertIn("export_job", ui.SIDE_ACTIONS)
        server = (PACKAGE_ROOT / "orchestrator" / "web" / "server.py").read_text()
        self.assertIn("s.action not in SIDE_ACTIONS", server)
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('runs.find((r) => r.running && !SIDE_RUNS.has(r.action))', app)
        self.assertIn('const SIDE_RUNS = new Set(["export_job"', app)
        dialog = app[app.index("async export_job_bundle(params)"):app.index("async run_visual_check(params)")]
        self.assertNotIn('runAction("export_job"', dialog)  # no run page
        self.assertIn("toast(`Exported to ${where}`)", dialog)


class FailedTestSummaryTests(unittest.TestCase):
    """A failed test run says which tests failed, and why, in the summary on top."""

    def test_names_and_reasons_come_from_common_runners(self):
        from orchestrator import run_result
        unittest_out = ["FAIL: test_adds (tests.t.MathTests.test_adds)", "Traceback (most recent call last):",
                        "AssertionError: 1 != 2", "", "Ran 4 tests in 0.01s", "", "FAILED (failures=1)"]
        self.assertEqual(run_result.tests(unittest_out, 1)["details"], ["test_adds: AssertionError: 1 != 2"])
        pytest_out = ["FAILED tests/test_a.py::test_login - AssertionError: expected 200", "=== 1 failed, 3 passed in 0.12s ==="]
        self.assertEqual(run_result.tests(pytest_out, 1)["details"], ["test_login: AssertionError: expected 200"])
        xctest_out = ["/x/LoginTests.swift:12: error: -[AppTests.LoginTests testSignIn] : XCTAssertTrue failed - not signed in",
                      "Test Case '-[AppTests.LoginTests testSignIn]' failed (0.010 seconds).",
                      "Executed 10 tests, with 1 failure (0 unexpected) in 0.3 seconds"]
        result = run_result.tests(xctest_out, 1)
        self.assertEqual((result["headline"], result["details"]), ("1 of 10 tests failed", ["testSignIn: XCTAssertTrue failed - not signed in"]))

    def test_the_summary_on_top_uses_them_once(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('detail: r.result?.details?.length ? r.result.details.join(" · ")', app)
        self.assertIn("fromResult: Boolean(r.result?.details?.length),", app)
        self.assertIn("!guidance?.fromResult", app)  # not repeated in the card below


class JobNamedByTitleTests(unittest.TestCase):
    """Run pages name a job by its title, not an ID like "job-1"."""

    def test_runs_carry_the_job_title_and_the_page_shows_it(self):
        server = (PACKAGE_ROOT / "orchestrator" / "web" / "server.py").read_text()
        self.assertIn('"job": self.job_id, "job_title": self.job_title,', server)
        self.assertIn('session.job_title = job_title if job_id else ""', server)
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn("${esc((targetJob === r.job && r.job_title) || targetJob)}", app)
        export = (PACKAGE_ROOT / "orchestrator" / "scripts" / "export_job.py").read_text()
        self.assertIn('print_phase("exporting", subtext=job.get("title") or job.get("summary") or job_id)', export)


class OneAddProjectButtonTests(unittest.TestCase):
    """One "Add existing project": a folder already set up is added; one that isn't goes through setup."""

    def test_add_routes_folders_that_are_not_set_up_to_setup(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertNotIn("Import existing project", app)
        self.assertIn("!(await api(`projects/import-info?path=${encodeURIComponent(root)}`)).has_config", app)
        self.assertEqual(app.count("if (await needsImport("), 2)  # discovered folders and a typed path; downloads use their own Home flow
        self.assertIn("location.hash = `#/import-project?path=${encodeURIComponent(result.importPath)}`", app)
        setup = app[app.index('pages["import-project"] = async (_, query)'):]
        self.assertIn('let path = query?.get("path") || "";', setup[:400])
        self.assertIn('data-then="#/">Switch</button>', app)  # not "Switch & Open"


class ProductRequirementsStatusTests(unittest.TestCase):
    def test_the_card_says_how_complete_the_requirements_are(self):
        import json as _json, subprocess as _sp
        app = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"
        source = app.read_text()
        self.assertIn("<h2>Product requirements</h2>${prdStatusPill(p.sections || [])}", source)
        fn = source[source.index("const prdStatusPill"):source.index("const pill = (tone, label)")]
        script = fn + """
const s = (n, of) => Array.from({length: of}, (_, i) => ({filled: i < n}));
console.log(JSON.stringify([prdStatusPill(s(0, 6)), prdStatusPill(s(3, 6)), prdStatusPill(s(6, 6)), prdStatusPill([])]));"""
        out = _json.loads(_sp.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        for html, label in zip(out, ["Needs completion", "Partially completed", "Completed", "Needs completion"]):
            self.assertIn(f">{label}</span>", html)
