"""Android (Gradle) and web (Node) projects: default commands, test groups you can switch, line coverage and the check.

Neither Gradle nor an Android SDK is needed: every run goes through a fake runner that writes the report a real run would."""
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT / "orchestrator" / "scripts") not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

import dev_console  # noqa: E402

from orchestrator import line_coverage, test_groups, test_probe  # noqa: E402
from orchestrator.stack_detection import detect_project_stack  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


ANDROID_APP = "plugins { id 'com.android.application' }\nandroid { buildTypes { debug { enableUnitTestCoverage true } } }\n"
KOTLIN_TEST = "import org.junit.Test\nclass FooTest {\n @Test fun a() {}\n @Test fun b() {}\n}\n"
JACOCO = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">
<report name="app"><package name="com/x"><sourcefile name="Foo.kt">
<line nr="3" mi="0" ci="4" mb="0" cb="0"/><line nr="4" mi="2" ci="0" mb="0" cb="0"/><line nr="5" mi="0" ci="1" mb="0" cb="0"/>
</sourcefile><sourcefile name="Missing.kt"><line nr="1" mi="1" ci="0" mb="0" cb="0"/></sourcefile></package></report>'''


class AndroidWorld(unittest.TestCase):
    """A Flutter-style layout: the Gradle root is android/, the app module android/app."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def android(self, build=ANDROID_APP, wrapper=True, tests=True, instrumented=True):
        write(self.root / "android/settings.gradle", "include ':app'\n")
        write(self.root / "android/app/build.gradle", build)
        if wrapper:
            write(self.root / "android/gradlew", "#!/bin/sh\n")
        if tests:
            write(self.root / "android/app/src/test/java/com/x/FooTest.kt", KOTLIN_TEST)
        if instrumented:
            write(self.root / "android/app/src/androidTest/java/com/x/UiTest.kt", "class UiTest {\n @Test fun c() {}\n}\n")
        write(self.root / "android/app/src/main/java/com/x/Foo.kt", "package com.x\nclass Foo {\n  fun a() = 1\n  fun b() = 2\n  fun c() = 3\n}\n")

    def groups(self, config=None):
        suites = dev_console.discover_test_suites(self.root)
        return test_groups.build(self.root, suites, config or {})[0]

    def group(self, framework, config=None):
        return next(g for g in self.groups(config) if g["framework"] == framework)


class StackDetectionTests(AndroidWorld):
    def test_an_android_app_gets_a_debug_build_and_its_unit_tests(self):
        write(self.root / "settings.gradle", "include ':app'\n")
        write(self.root / "gradlew", "#!/bin/sh\n")
        write(self.root / "app/build.gradle", "plugins { id 'org.jetbrains.kotlin.android' }\napply plugin: 'com.android.application'\n")
        stack = detect_project_stack(self.root)
        self.assertEqual((stack.display_name, stack.language, stack.framework), ("Android (Gradle)", "kotlin", "gradle"))
        self.assertEqual((stack.build_command, stack.test_command), ("./gradlew assembleDebug", "./gradlew testDebugUnitTest"))
        self.assertFalse(stack.uses_xcode)

    def test_without_a_wrapper_it_uses_the_installed_gradle_and_java_when_there_is_no_kotlin(self):
        write(self.root / "build.gradle", "plugins { id 'com.android.library' }\n")
        stack = detect_project_stack(self.root)
        self.assertEqual((stack.build_command, stack.test_command, stack.language), ("gradle assembleDebug", "gradle testDebugUnitTest", "java"))

    def test_a_plain_jvm_project_builds_without_running_tests_twice(self):
        write(self.root / "build.gradle.kts", 'plugins { kotlin("jvm") version "1.9.0" }\n')
        write(self.root / "gradlew", "#!/bin/sh\n")
        stack = detect_project_stack(self.root)
        self.assertEqual((stack.display_name, stack.build_command, stack.test_command), ("Kotlin (Gradle)", "./gradlew build -x test", "./gradlew test"))

    def test_a_web_project_with_an_android_folder_is_still_a_node_project(self):
        write(self.root / "package.json", json.dumps({"scripts": {"build": "vite build", "test": "vitest run"}}))
        write(self.root / "android/settings.gradle", "include ':app'\n")
        write(self.root / "android/app/build.gradle", ANDROID_APP)
        self.assertEqual(detect_project_stack(self.root).framework, "npm")


class GradleGroupTests(AndroidWorld):
    def test_unit_and_instrumented_tests_are_separate_groups_with_their_own_command(self):
        self.android()
        unit = self.group("Android unit tests")
        device = self.group("Android instrumented tests")
        self.assertEqual((unit["runner"], unit["toggle"], unit["mode"], unit["folder"]), ("gradle", True, "off", "android/app"))
        self.assertEqual(unit["command"], "android/gradlew -p android :app:testDebugUnitTest")
        self.assertEqual(device["command"], "android/gradlew -p android :app:connectedDebugAndroidTest")
        self.assertEqual((unit["tests"], device["tests"]), (2, 1))

    def test_the_wrapper_at_the_repository_root_is_used_with_the_gradle_folder(self):
        self.android(wrapper=False)
        write(self.root / "gradlew", "#!/bin/sh\n")
        self.assertEqual(self.group("Android unit tests")["command"], "./gradlew -p android :app:testDebugUnitTest")

    def test_with_no_wrapper_anywhere_the_installed_gradle_is_used(self):
        self.android(wrapper=False)
        self.assertEqual(self.group("Android unit tests")["command"], "gradle -p android :app:testDebugUnitTest")

    def test_a_root_module_needs_no_folder_flag_or_path(self):
        write(self.root / "settings.gradle", "")
        write(self.root / "build.gradle", "apply plugin: 'com.android.application'\n")
        write(self.root / "gradlew", "")
        write(self.root / "src/test/java/FooTest.kt", KOTLIN_TEST)
        self.assertEqual(self.group("Android unit tests")["command"], "./gradlew testDebugUnitTest")

    def test_a_plain_jvm_module_runs_test_and_has_no_instrumented_group(self):
        write(self.root / "settings.gradle", "include 'lib'\n")
        write(self.root / "lib/build.gradle", "plugins { id 'java' }\n")
        write(self.root / "gradlew", "")
        write(self.root / "lib/src/test/java/FooTest.java", "class FooTest {\n @Test void a() {}\n}\n")
        group = self.group("JUnit")
        self.assertEqual((group["runner"], group["command"]), ("gradle", "./gradlew :lib:test"))

    def test_a_kotlin_project_with_no_gradle_build_keeps_the_old_listed_only_group(self):
        write(self.root / "src/test/java/FooTest.kt", KOTLIN_TEST)
        group = self.group("JUnit")
        self.assertEqual((group["runner"], group["toggle"]), (None, False))

    def test_plain_gradle_test_counts_as_every_modules_unit_tests_and_a_qualified_task_only_its_own(self):
        self.android()
        unit = self.group("Android unit tests")
        for command, expected in (("./gradlew test", True), ("android/gradlew -p android check", True), ("./gradlew :app:testDebugUnitTest", True),
                                  ("./gradlew :other:testDebugUnitTest", False), ("./gradlew assembleDebug", False), ("npm test", False),
                                  ("./gradlew -p android :app:connectedDebugAndroidTest", False)):
            with self.subTest(command=command):
                self.assertEqual(test_groups._matches(unit, command), expected)
        device = self.group("Android instrumented tests")
        self.assertFalse(test_groups._matches(device, "./gradlew test"))  # unit tests never count as device tests
        self.assertTrue(test_groups._matches(device, "./gradlew :app:connectedDebugAndroidTest"))

    def test_the_main_test_command_decides_whether_a_group_runs_on_every_job(self):
        self.android()
        self.assertEqual(self.group("Android unit tests", {"test_command": "./gradlew testDebugUnitTest"})["mode"], "per-job")
        by_hand = {"test_commands": [{"name": "device", "command": "./gradlew :app:connectedDebugAndroidTest", "tier": "manual"}]}
        state = self.group("Android instrumented tests", by_hand)
        self.assertEqual((state["mode"], state["where"]), ("manual", "extra"))

    def test_switching_a_group_to_by_hand_and_back_edits_project_json_like_any_other_group(self):
        self.android()
        unit = self.group("Android unit tests")
        config = {"test_command": "python3 -m unittest discover tests"}
        changes = test_groups.plan(config, unit, "per-job")
        self.assertEqual(changes["test_commands"], [{"name": unit["id"], "command": unit["command"]}])
        config = {**config, **changes}
        by_hand = test_groups.plan(config, {**unit, **test_groups.state_of(unit, config)}, "manual")
        self.assertEqual(by_hand["test_commands"][0]["tier"], "manual")


class NodeGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def package(self, folder, scripts=None, deps=None, lock=None):
        write(self.root / folder / "package.json", json.dumps({"scripts": scripts or {}, "devDependencies": deps or {}}))
        write(self.root / folder / "src/a.test.ts", "import {test} from 'vitest'\ntest('one', () => {})\ntest('two', () => {})\n")
        if lock:
            write(self.root / folder / lock, "")

    def groups(self, config=None):
        return test_groups.build(self.root, dev_console.discover_test_suites(self.root), config or {})[0]

    def test_a_test_script_becomes_a_group_in_its_package_folder(self):
        self.package("web", {"test": "vitest run"}, {"vitest": "^1"})
        group = self.groups()[0]
        self.assertEqual((group["framework"], group["runner"], group["folder"], group["tool"], group["toggle"]), ("JavaScript tests", "node", "web", "vitest", True))
        self.assertEqual(group["command"], "npm --prefix web test")

    def test_the_package_manager_decides_how_the_folder_is_passed(self):
        for lock, expected in (("pnpm-lock.yaml", "pnpm --dir web test"), ("yarn.lock", "yarn --cwd web test"), (None, "npm --prefix web test")):
            with self.subTest(lock=lock):
                self.setUp()
                self.package("web", {"test": "jest --ci"}, {"jest": "^29"}, lock)
                self.assertEqual(self.groups()[0]["command"], expected)

    def test_a_package_at_the_root_needs_no_folder(self):
        self.package(".", {"test": "vitest run"}, {"vitest": "^1"})
        self.assertEqual(self.groups()[0]["command"], "npm test")

    def test_a_watching_script_is_replaced_by_the_tools_single_run(self):
        self.package("web", {"test": "vitest"}, {"vitest": "^1"})
        self.assertEqual(self.groups()[0]["command"], "npm --prefix web exec -- vitest run")
        self.setUp()
        self.package("web", {"test": "jest --watch"}, {"jest": "^29"})
        self.assertEqual(self.groups()[0]["command"], "npm --prefix web exec -- jest")

    def test_create_react_apps_runner_is_told_not_to_wait_for_keys(self):
        self.package(".", {"test": "react-scripts test"})
        self.assertEqual(self.groups()[0]["command"], "CI=true npm test")

    def test_the_default_npm_placeholder_is_not_a_test_command(self):
        self.package("web", {"test": 'echo "Error: no test specified" && exit 1'})
        group = self.groups()[0]
        self.assertEqual((group["runner"], group["toggle"]), (None, False))

    def test_the_main_test_command_decides_the_mode(self):
        self.package("web", {"test": "vitest run"}, {"vitest": "^1"})
        self.assertEqual(self.groups({"test_command": "npm --prefix web test"})[0]["mode"], "per-job")
        self.assertEqual(self.groups({"test_command": "npm test"})[0]["mode"], "off")  # a root npm test isn't web's
        self.assertEqual(self.groups({"test_command": "pytest && npm --prefix web test"})[0]["mode"], "per-job")

    def test_switching_to_by_hand_moves_it_out_of_the_main_command(self):
        self.package("web", {"test": "vitest run"}, {"vitest": "^1"})
        group = self.groups({"test_command": "pytest && npm --prefix web test"})[0]
        config = {"test_command": "pytest && npm --prefix web test"}
        changes = test_groups.plan(config, group, "manual")
        self.assertEqual(changes["test_command"], "pytest")
        self.assertEqual(changes["test_commands"], [{"name": group["id"], "command": "npm --prefix web test", "tier": "manual"}])


class CoverageParserTests(AndroidWorld):
    def test_lcov_gives_covered_and_coverable_lines_per_project_file(self):
        write(self.root / "web/src/a.ts", "x")
        text = ("TN:\nSF:src/a.ts\nDA:1,3\nDA:2,0\nDA:3,1,abc\nLF:3\nLH:2\nend_of_record\n"
                "SF:node_modules/dep/index.js\nDA:1,1\nend_of_record\nSF:/elsewhere/z.ts\nDA:1,1\nend_of_record\nSF:src/none.ts\nend_of_record\n")
        files = line_coverage.parse_lcov(text, self.root / "web", self.root)
        self.assertEqual(files, {"web/src/a.ts": ({1, 3}, {1, 2, 3})})

    def test_jacoco_lines_are_matched_to_source_files_by_package_and_name(self):
        self.android()
        files = line_coverage.parse_jacoco(JACOCO, self.root, self.root / "android" / "app")
        self.assertEqual(files, {"android/app/src/main/java/com/x/Foo.kt": ({3, 5}, {3, 4, 5})})  # Missing.kt isn't in the project

    def test_a_report_with_an_internal_dtd_subset_or_entities_is_refused(self):
        self.android()
        module = self.root / "android" / "app"
        for evil in ('<?xml version="1.0"?><!DOCTYPE report [<!ENTITY x "y">]><report/>',
                     '<!DOCTYPE report [ ]><report><package name="com/x"/></report>'):
            self.assertEqual(line_coverage.parse_jacoco(evil, self.root, module), {})
        self.assertEqual(line_coverage.parse_jacoco("not xml", self.root, module), {})


class GradleCoverageTests(AndroidWorld):
    def fake(self, report=JACOCO, code=0, output="BUILD SUCCESSFUL", where="build/reports/coverage/test/debug/report.xml"):
        calls = []

        def run(command, cwd, timeout):
            calls.append(command)
            if report and code == 0:
                write(self.root / "android/app" / where, report)
            return code, output, False
        run.calls = calls
        return run

    def test_an_android_module_with_unit_test_coverage_enabled_is_measured(self):
        self.android()
        run = self.fake()
        result = line_coverage.measure_group(self.group("Android unit tests"), self.root, run)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"], {"android/app/src/main/java/com/x/Foo.kt": ({3, 5}, {3, 4, 5})})
        self.assertEqual(run.calls, ["android/gradlew -p android :app:createDebugUnitTestCoverageReport"])

    def test_a_module_that_writes_no_report_is_told_how_to_enable_it(self):
        self.android(build="plugins { id 'com.android.application' }\n")
        result = line_coverage.measure_group(self.group("Android unit tests"), self.root, self.fake())
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("enableUnitTestCoverage = true", result["reason"])

    def test_a_jvm_module_with_the_jacoco_plugin_runs_test_then_the_report(self):
        write(self.root / "settings.gradle", "include 'lib'\n")
        write(self.root / "lib/build.gradle", "plugins { id 'java'\n id 'jacoco' }\n")
        write(self.root / "gradlew", "")
        write(self.root / "lib/src/test/java/FooTest.java", "class FooTest {\n @Test void a() {}\n}\n")
        write(self.root / "lib/src/main/java/com/x/Foo.kt", "package com.x\nclass Foo\n")
        calls = []

        def run(command, cwd, timeout):
            calls.append(command)
            write(self.root / "lib/build/reports/jacoco/test/jacocoTestReport.xml", JACOCO)
            return 0, "", False
        result = line_coverage.measure_group(self.group("JUnit"), self.root, run)
        self.assertEqual((result["status"], calls), ("ok", ["./gradlew :lib:test :lib:jacocoTestReport"]))

    def test_tests_on_a_device_are_not_measured(self):
        self.android()
        result = line_coverage.measure_group(self.group("Android instrumented tests"), self.root, self.fake())
        self.assertEqual(result["status"], "unsupported")

    def test_a_failing_build_gives_gradles_own_reason(self):
        self.android()
        result = line_coverage.measure_group(self.group("Android unit tests"), self.root,
                                             self.fake(code=1, output="FAILURE: Build failed\nerror: SDK location not found"))
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("SDK location not found", result["reason"])

    def test_measure_covers_every_group_a_tool_can_measure_and_skips_the_rest(self):
        self.android()
        measurement = line_coverage.measure(self.groups(), self.root, run=self.fake())
        notes = {g["framework"]: measurement["groups"][g["id"]]["status"] for g in self.groups()}
        self.assertEqual(notes, {"Android unit tests": "ok", "Android instrumented tests": "unsupported"})
        self.assertEqual(measurement["files"], {"android/app/src/main/java/com/x/Foo.kt": [2, 3]})

    def test_the_result_lands_on_the_projects_platform(self):
        self.android()
        from orchestrator import platforms
        measurement = line_coverage.measure(self.groups(), self.root, run=self.fake())
        config = {"platforms": [{"name": "Android app", "paths": ["android/app"]}]}
        applied = line_coverage.apply(platforms.analyze(self.root, config), measurement)
        android = next(p for p in applied["platforms"] if p["name"] == "Android app")
        self.assertEqual((android["measured"]["covered"], android["measured"]["lines"], android["measured"]["percent"]), (2, 3, 67))


class NodeCoverageTests(unittest.TestCase):
    LCOV = "SF:src/a.ts\nDA:1,2\nDA:2,0\nDA:3,5\nend_of_record\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def web(self, tool="vitest", deps=None, installed=True):
        deps = deps if deps is not None else ({"vitest": "^1", "@vitest/coverage-v8": "^1"} if tool == "vitest" else {"jest": "^29"})
        write(self.root / "web/package.json", json.dumps({"scripts": {"test": f"{tool} run" if tool == "vitest" else tool}, "devDependencies": deps}))
        write(self.root / "web/src/a.test.ts", "test('a', () => {})\n")
        write(self.root / "web/src/a.ts", "x")
        if installed:
            (self.root / "web/node_modules").mkdir()
        suites = dev_console.discover_test_suites(self.root)
        return test_groups.build(self.root, suites, {})[0][0]

    def fake(self, code=0, output="", write_report=True):
        calls = []

        def run(command, cwd, timeout):
            calls.append(command)
            match = re.search(r"--(?:coverageDirectory|coverage\.reportsDirectory)=(\S+)", command)
            if write_report and match:
                write(Path(shlex.split(match.group(1))[0]) / "lcov.info", self.LCOV)
            return code, output, False
        run.calls = calls
        return run

    def test_vitest_is_run_once_in_its_folder_and_its_lcov_read(self):
        run = self.fake()
        result = line_coverage.measure_group(self.web("vitest"), self.root, run)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"], {"web/src/a.ts": ({1, 3}, {1, 2, 3})})
        self.assertTrue(run.calls[0].startswith("cd web && npx --no-install vitest run --coverage --coverage.reporter=lcov"))

    def test_jest_works_the_same_way(self):
        run = self.fake()
        result = line_coverage.measure_group(self.web("jest"), self.root, run)
        self.assertEqual(result["status"], "ok")
        self.assertIn("npx --no-install jest --coverage --coverageReporters=lcov", run.calls[0])

    def test_nothing_is_downloaded_or_run_when_the_packages_are_not_installed(self):
        run = self.fake()
        result = line_coverage.measure_group(self.web("vitest", installed=False), self.root, run)
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("npm install", result["reason"])
        self.assertEqual(run.calls, [])

    def test_vitest_without_a_coverage_provider_says_which_to_add(self):
        result = line_coverage.measure_group(self.web("vitest", deps={"vitest": "^1"}), self.root, self.fake())
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("@vitest/coverage-v8", result["reason"])

    def test_a_package_whose_test_tool_is_neither_is_not_measured(self):
        write(self.root / "web/package.json", json.dumps({"scripts": {"test": "mocha"}}))
        write(self.root / "web/src/a.test.ts", "test('a', () => {})\n")
        (self.root / "web/node_modules").mkdir()
        group = test_groups.build(self.root, dev_console.discover_test_suites(self.root), {})[0][0]
        self.assertEqual(line_coverage.measure_group(group, self.root, self.fake())["status"], "unsupported")

    def test_tests_that_fail_still_give_the_coverage_they_reached_and_say_so(self):
        run = self.fake(code=1)
        measurement = line_coverage.measure([self.web("vitest")], self.root, run=run)
        self.assertEqual(measurement["files"], {"web/src/a.ts": [2, 3]})


class ProbeTests(AndroidWorld):
    def test_gradle_is_asked_to_plan_the_tasks_and_run_none(self):
        self.android()
        calls = []
        result = test_probe.probe(self.group("Android unit tests"), self.root, run=lambda c, cwd, t: (calls.append(c) or (0, "", False)))
        self.assertEqual((result["status"], result["tests"]), ("ok", None))
        self.assertEqual(calls, ["android/gradlew -p android :app:testDebugUnitTest --dry-run"])

    def test_a_missing_sdk_is_reported_in_gradles_words(self):
        self.android()
        output = "FAILURE: Build failed with an exception.\n\n* What went wrong:\nSDK location not found. Define a valid SDK location with ANDROID_HOME.\n\n* Try:\n"
        result = test_probe.probe(self.group("Android unit tests"), self.root, run=lambda c, cwd, t: (1, output, False))
        self.assertEqual(result["status"], "needs-setup")
        self.assertTrue(result["reason"].startswith("SDK location not found"))

    def test_without_gradle_or_a_wrapper_it_says_so_without_running_anything(self):
        self.android(wrapper=False)
        ran = []
        result = test_probe.probe(self.group("Android unit tests"), self.root, run=lambda c, cwd, t: ran.append(c), which=lambda n: None)
        self.assertEqual((result["status"], ran), ("needs-setup", []))
        self.assertIn("no gradlew", result["reason"])

    def test_a_package_needs_node_and_its_installed_packages(self):
        write(self.root / "web/package.json", json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "^1"}}))
        write(self.root / "web/src/a.test.ts", "test('a', () => {})\n")
        group = test_groups.build(self.root, dev_console.discover_test_suites(self.root), {})[0][0]
        none = lambda n: None
        self.assertIn("Node.js isn't installed", test_probe.probe(group, self.root, which=none)["reason"])
        self.assertIn("npm install", test_probe.probe(group, self.root, which=lambda n: "/usr/bin/node")["reason"])
        (self.root / "web/node_modules").mkdir()
        self.assertEqual(test_probe.probe(group, self.root, which=lambda n: "/usr/bin/node")["status"], "ok")


class CoverageActionTests(unittest.TestCase):
    """The Tests page's one coverage button, run as the real command, for projects whose package or module isn't at the root.

    It used to measure each platform and then fail the overall step with "Found manifest at 'web/package.json', but expected at
    project root". The per-platform run already measured those tests, so the overall step is skipped."""

    def run_action(self, root: Path):
        import os
        import subprocess
        env = {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root), "PYTHONPATH": str(PACKAGE_ROOT), "ORCHESTRATOR_NONINTERACTIVE": "1"}
        return subprocess.run([sys.executable, "-m", "orchestrator", "script", "job_actions.py", "coverage"], cwd=root, env=env,
                              capture_output=True, text=True, timeout=240)

    def project(self, root: Path, config: dict):
        write(root / ".orchestrator/project.json", json.dumps({"project_name": "P", "base_branch": "main", "build_command": "b", **config}))

    def test_a_web_package_in_a_subfolder_is_measured_and_the_overall_step_is_not_repeated(self):
        import shutil
        if not shutil.which("npx"):
            self.skipTest("npx isn't installed here")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write(root / "web/package.json", json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "^1", "@vitest/coverage-v8": "^1"}}))
            write(root / "web/src/a.test.ts", "import {test} from 'vitest'\ntest('one', () => {})\n")
            write(root / "web/src/a.ts", "export const x = 1\n")
            stub = write(root / "web/node_modules/.bin/vitest", '#!/bin/bash\nfor a in "$@"; do case "$a" in --coverage.reportsDirectory=*) d="${a#*=}"; mkdir -p "$d"; '
                                                                 "printf 'SF:src/a.ts\\nDA:1,2\\nDA:2,0\\nend_of_record\\n' > \"$d/lcov.info\";; esac; done\nexit 0\n")
            stub.chmod(0o755)
            self.project(root, {"test_command": "npm --prefix web test"})
            done = self.run_action(root)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("Saved line coverage for 1 files", done.stdout)
            self.assertIn("comes from the per-platform run above", done.stdout)
            self.assertNotIn("Coverage measurement failed", done.stdout)
            cache = json.loads((root / ".orchestrator/output/line-coverage.json").read_text())
            self.assertEqual(cache["files"], {"web/src/a.ts": [1, 2]})

    def test_a_gradle_module_is_measured_and_the_overall_step_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write(root / "android/settings.gradle", "include ':app'\n")
            write(root / "android/app/build.gradle", ANDROID_APP)
            write(root / "android/app/src/test/java/com/x/FooTest.kt", KOTLIN_TEST)
            write(root / "android/app/src/main/java/com/x/Foo.kt", "package com.x\nclass Foo {\n  fun a() = 1\n  fun b() = 2\n  fun c() = 3\n}\n")
            gradlew = write(root / "android/gradlew", "#!/bin/bash\nD=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\nmkdir -p \"$D/app/build/reports/coverage/test/debug\"\n"
                                                    "cat > \"$D/app/build/reports/coverage/test/debug/report.xml\" <<'XML'\n" + JACOCO + "\nXML\nexit 0\n")
            gradlew.chmod(0o755)
            self.project(root, {"test_command": "android/gradlew -p android :app:testDebugUnitTest"})
            done = self.run_action(root)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("Saved line coverage for 1 files", done.stdout)
            self.assertIn("comes from the per-platform run above", done.stdout)
            self.assertNotIn("Coverage measurement failed", done.stdout)
            cache = json.loads((root / ".orchestrator/output/line-coverage.json").read_text())
            self.assertEqual(cache["files"], {"android/app/src/main/java/com/x/Foo.kt": [2, 3]})

    def test_an_android_module_with_no_coverage_report_set_up_says_how_instead_of_pretending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write(root / "android/settings.gradle", "include ':app'\n")
            write(root / "android/app/build.gradle", "plugins { id 'com.android.application' }\n")
            write(root / "android/app/src/test/java/com/x/FooTest.kt", KOTLIN_TEST)
            write(root / "android/gradlew", "#!/bin/bash\nexit 0\n").chmod(0o755)
            self.project(root, {"test_command": "android/gradlew -p android :app:testDebugUnitTest"})
            done = self.run_action(root)
            self.assertIn("enableUnitTestCoverage = true", done.stdout)
            self.assertNotIn("Saved line coverage", done.stdout)


class SupportedPlatformsClaimTests(unittest.TestCase):
    """What the product says it supports is pinned in one place: iOS, Android and web, on a Mac."""

    def test_the_sign_in_page_says_what_is_supported_with_an_icon_for_each(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app = (static / "app.js").read_text()
        line = app[app.index('<p class="signin-platforms">'):][:700]
        for symbol in ("#i-globe", "#i-apple", "#i-android"):
            self.assertIn(f'href="{symbol}"', line)
            self.assertIn(f'id="{symbol[1:]}"', (static / "index.html").read_text(), "the icon exists in the page's sprite")
        text = re.sub(r"<[^>]+>", "", line)
        self.assertRegex(re.sub(r"\s+", " ", text), r"Fully supports web, iOS, and Android platforms")
        self.assertNotIn("Apple</span>", line)  # iOS only for now: macOS, tvOS, watchOS and visionOS apps aren't delivered

    def test_the_new_project_wizard_offers_only_what_is_supported(self):
        from orchestrator import new_project
        offered = " | ".join(new_project.PLATFORMS)
        for kept in ("iOS app", "Android app", "Web app"):
            self.assertIn(kept, offered)
        for dropped in ("macOS app", "Windows or Linux app"):
            self.assertNotIn(dropped, offered)
            self.assertNotIn(dropped, new_project.PLATFORM_NEEDS)
        # a draft saved before this change that chose one of them still loads, with no setup hints for it
        self.assertEqual(new_project.platform_needs("macOS app, iOS app")[0]["platform"], "iOS app")

    def test_the_readme_and_guides_say_a_mac_and_name_the_three_platforms(self):
        readme = (PACKAGE_ROOT / "README.md").read_text()
        self.assertIn("Supported platforms", readme)
        self.assertIn("fully supports **iOS, Android and web** projects", readme)
        self.assertNotIn("Mac or Linux", readme)
        static_docs = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "docs"
        for name in ("getting-started.md", "user-guide.md"):
            self.assertNotIn("Mac or Linux", (static_docs / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
