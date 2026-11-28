from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator' / 'scripts'))
import nonproduction_job as runner
import new_job
import run_builder
import reference_artifacts
import schedule_job


class NonproductionJobTests(unittest.TestCase):
    def run_job(self, root, kind, files, command=None, dirty=(), result=None):
        out = root / '.orchestrator' / 'output'
        job_path = root / 'job.json'
        if not job_path.exists():
            job_path.write_text(json.dumps({'job_id': 'job-1', 'type': kind, 'title': 'Scoped output', 'builder': 'fake', 'plan': {}}))
        config = SimpleNamespace(test_command=command, stack=SimpleNamespace(test_command=None))

        def answer(*args, **kwargs):
            self.prompts = [*getattr(self, 'prompts', []), args[1]]
            self.assertEqual(kwargs['protected_paths'], [root])
            return json.dumps(result if result is not None else {'summary': 'Scoped result', 'files': files}), 'fake', 'session'

        with ExitStack() as stack:
            stack.enter_context(patch.multiple(runner, ROOT=root, OUTPUT_DIR=out, PROJECT_CONFIG=config))
            stack.enter_context(patch.multiple(reference_artifacts, ROOT=root, OUTPUT_DIR=out))
            stack.enter_context(patch.object(runner, 'get_repo_state', return_value={'modified': list(dirty), 'untracked': []}))
            stack.enter_context(patch.object(runner, 'run_llm', side_effect=answer))
            stack.enter_context(patch.object(runner, 'remeasure_coverage'))  # measuring is tested on its own below
            runner.execute_nonproduction_job(job_path)
        return json.loads(job_path.read_text())

    def test_production_proposal_is_rejected_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'app.py').write_text('original')
            job = self.run_job(root, 'test-audit', [
                {'path': 'tests/test_app.py', 'content': 'test'},
                {'path': 'app.py', 'content': 'changed'},
            ])
            self.assertEqual(job['status'], 'human-needed')
            self.assertEqual((root / 'app.py').read_text(), 'original')
            self.assertFalse((root / 'tests/test_app.py').exists())

    def test_design_returns_previewable_mockups_without_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'app.py').write_text('original')
            job = self.run_job(root, 'feature-design', [{'path': 'screen.html', 'content': '<html>Visual mockup</html>'},
                                                       {'path': 'screen.svg', 'content': '<svg xmlns="http://www.w3.org/2000/svg"/>'}])
            self.assertEqual(job['status'], 'review-needed')
            self.assertEqual(job['type'], 'feature-design')
            self.assertEqual(job['output_scope'], 'design-mockups')
            self.assertEqual((root / 'app.py').read_text(), 'original')
            self.assertEqual(job['design_artifacts'], ['docs/product/designs/job-1/screen.html', 'docs/product/designs/job-1/screen.svg'])
            self.assertEqual(len(job['reference_artifacts']), 2)
            artifact = job['reference_artifacts'][0]
            self.assertEqual(artifact['role'], 'design')
            self.assertEqual((root / artifact['path']).read_text(), '<html>Visual mockup</html>')
            self.assertIsNone(job['worker_pid'])

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_validation_cannot_write_production_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'app.py').write_text('original')
            import shlex
            command = shlex.join([sys.executable, '-c', 'import os; from pathlib import Path; Path(os.environ["PROTECTED_TEST_SOURCE"]).write_text("changed")'])
            with patch.dict('os.environ', {'PROTECTED_TEST_SOURCE': str(root / 'app.py')}):
                job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': 'test'}], command)
            self.assertEqual(job['status'], 'human-needed')
            self.assertFalse(job['test_validation']['passed'])
            self.assertEqual((root / 'app.py').read_text(), 'original')
            self.assertNotIn('debug_phase', job)

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_test_job_runs_tests_and_stops_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            command = shlex.join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
            test = 'import unittest\nclass Demo(unittest.TestCase):\n def test_ok(self): self.assertEqual(2 + 2, 4)\n'
            job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': test}], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertTrue(job['test_validation']['passed'])
            self.assertEqual(job['output_scope'], 'tests-only')
            self.assertNotIn('pr_number', job)

    def test_validation_commands_skip_nested_sandboxes_and_environment_failures_are_told_apart(self):
        self.assertEqual(runner.sandbox_safe('xcodebuild test -scheme A'),
                         "xcodebuild -IDEPackageSupportDisableManifestSandbox=1 -IDEPackageSupportDisablePluginExecutionSandbox=1 "
                         "-skipMacroValidation -skipPackagePluginValidation OTHER_SWIFT_FLAGS='$(inherited) -disable-sandbox' test -scheme A")
        self.assertEqual(runner.sandbox_safe('python3 -c "print(1)  # xcodebuild: error"'), 'python3 -c "print(1)  # xcodebuild: error"')  # only a command is rewritten, not text that mentions it
        self.assertIn("&& xcodebuild -IDEPackageSupportDisableManifestSandbox=1", runner.sandbox_safe('cd app && xcodebuild test'))
        self.assertEqual(runner.sandbox_safe('swift test && swift build'), 'swift test --disable-sandbox && swift build --disable-sandbox')
        self.assertEqual(runner.sandbox_safe(runner.sandbox_safe('swift test')), 'swift test --disable-sandbox')
        self.assertIn('Could not resolve package dependencies', runner.environment_failure('x\nxcodebuild: error: Could not resolve package dependencies:\ny'))
        self.assertEqual(runner.environment_failure('FAILED test_x\nAssertionError: 1 != 2'), '')

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_environment_failure_keeps_tests_and_try_again_checks_them_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            marker = root / 'ready'
            command = shlex.join([sys.executable, '-c',
                f'import sys, pathlib; ready = pathlib.Path({str(marker)!r}).exists(); print("" if ready else "xcodebuild: error: Could not resolve package dependencies:"); sys.exit(0 if ready else 1)'])
            job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': 'test'}], command)
            self.assertEqual(job['status'], 'human-needed')
            self.assertEqual(job['test_validation']['kind'], 'environment')
            self.assertIn("couldn't run", job['last_error'])
            self.assertTrue((root / 'tests/test_app.py').exists())  # the new tests stay
            marker.write_text('')
            (root / 'tests/test_app.py').write_text('test  # the person fixed something in it')  # an edit to a saved file doesn't force a rewrite
            with patch.object(runner, 'run_llm', side_effect=AssertionError('no model on a retry')):
                job = self.run_job(root, 'test-audit', [], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertTrue(job['test_validation']['passed'])

    def test_a_missing_simulator_is_an_environment_failure_not_a_failed_test(self):
        self.assertIn('Unable to find a device matching', runner.environment_failure(
            'xcodebuild: error: Unable to find a device matching the provided destination specifier:\n{ platform:iOS Simulator, name:iPhone 15 Pro }'))

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_validation_tries_an_installed_simulator_when_the_configured_one_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            script = ('import sys; a = " ".join(sys.argv); '
                      'print("xcodebuild: error: Unable to find a device matching the provided destination specifier:" if "name=Old" in a else "ok"); '
                      'sys.exit(1 if "name=Old" in a else 0)')
            command = shlex.join([sys.executable, '-c', script, 'xcodebuild', '-destination', 'platform=iOS Simulator,name=Old'])
            with patch.object(runner, 'get_fallback_simulator_destinations',
                              return_value=['platform=iOS Simulator,name=Old', 'platform=iOS Simulator,name=New']):
                job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': 'test'}], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertTrue(job['test_validation']['passed'])
            self.assertEqual(job['test_validation']['destination'], 'platform=iOS Simulator,name=New')

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_a_pass_that_never_ran_the_new_swift_tests_is_not_called_a_pass(self):
        swift = 'import XCTest\nfinal class ParserTests: XCTestCase {\n func testA() {}\n}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            ran = shlex.join([sys.executable, '-c', 'print("Test Suite \'Other\' passed")'])
            job = self.run_job(root, 'test-audit', [{'path': 'Tests/ParserTests.swift', 'content': swift}], ran)
            self.assertEqual(job['status'], 'human-needed')
            self.assertEqual((job['test_validation']['kind'], job['test_validation']['not_run']), ('unregistered', ['Tests/ParserTests.swift']))
            self.assertFalse(job['test_validation']['passed'])
            self.assertIn('never ran: ParserTests.swift', job['last_error'])
            self.assertTrue((root / 'Tests/ParserTests.swift').exists())  # saved, so Try again needs no model
            ok = shlex.join([sys.executable, '-c', 'print("Test Suite \'ParserTests\' passed")'])
            with patch.object(runner, 'run_llm', side_effect=AssertionError('no model on a retry')):
                job = self.run_job(root, 'test-audit', [], ok)
            self.assertEqual((job['status'], job['test_validation']['kind']), ('review-needed', 'passed'))

    @unittest.skipUnless(shutil.which('ruby') and shutil.which('git')
                         and runner.subprocess.run(['ruby', '-e', "require 'xcodeproj'"], capture_output=True).returncode == 0, 'needs ruby with xcodeproj, and git')
    def test_new_tests_join_the_real_projects_test_target_only_over_an_untouched_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = ("require 'xcodeproj'; p = Xcodeproj::Project.new(ARGV[0] + '/App.xcodeproj'); t = p.new_target(:unit_test_bundle, 'AppTests', :ios); "
                      "g = p.main_group.new_group('AppTests', 'AppTests'); t.source_build_phase.add_file_reference(g.new_file('Old.swift')); p.save")
            runner.subprocess.run(['ruby', '-e', script, str(root)], check=True)
            (root / 'AppTests').mkdir()
            for name in ('Old', 'New', 'Newer'):
                (root / f'AppTests/{name}.swift').write_text('x')
            for args in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init']):
                runner.subprocess.run(['git', *args], cwd=root, check=True)
            project = 'App.xcodeproj/project.pbxproj'
            with patch.multiple(runner, ROOT=root, OUTPUT_DIR=root / '.orchestrator' / 'output'):
                (root / '.orchestrator').mkdir()
                job = {'test_validation': {}}
                runner.keep_registration(job, ['AppTests/New.swift'])  # the project file is clean: added
                self.assertEqual(job['test_validation']['registered'], ['AppTests/New.swift'])
                self.assertEqual(job['ai_modified_files'], [project])
                job2 = {'test_validation': {}}
                runner.keep_registration(job2, ['AppTests/Newer.swift'])  # dirty only with our own earlier edit: added too
                self.assertEqual(job2['test_validation']['registered'], ['AppTests/Newer.swift'])
                (root / project).write_text((root / project).read_text() + '\n// my own edit\n')
                job3 = {'test_validation': {}}
                runner.keep_registration(job3, ['AppTests/Old.swift', 'AppTests/Other.swift'])  # the person's edit: left alone
                self.assertNotIn('registered', job3['test_validation'])
                self.assertIn('uncommitted changes', job3['test_validation']['note'])

    def test_failing_tests_are_read_from_the_run_output(self):
        xctest = ("/p/Tests/ParserTests.swift:10: error: -[Suite.ParserTests testCRLF] : XCTAssertEqual failed: (\"1\") is not equal to (\"2\")\n"
                  "/p/Tests/ParserTests.swift:11: error: -[Suite.ParserTests testCRLF] : XCTAssertEqual failed: ([a]) is not equal to ([a, a])\n"
                  "/p/Tests/OtherTests.swift:5: error: -[OtherTests testB] : XCTAssertTrue failed\n")
        found = runner.failing_tests(xctest)
        self.assertEqual([f['test'] for f in found], ['ParserTests.testCRLF', 'OtherTests.testB'])
        self.assertIn('is not equal to ("2"); XCTAssertEqual failed', found[0]['message'])  # both complaints about one test, once
        self.assertEqual(runner.failing_tests('FAILED tests/test_a.py::test_x - assert 1 == 2\nFAILED tests/test_a.py::test_y\n'),
                         [{'test': 'tests/test_a.py::test_x', 'message': 'assert 1 == 2'}, {'test': 'tests/test_a.py::test_y', 'message': ''}])
        self.assertEqual(runner.failing_tests('all fine'), [])

    def test_defects_are_trimmed_kept_across_attempts_and_replaced_by_the_same_test(self):
        first = runner.clean_defects([{'test': 'testCRLF', 'summary': 'CRLF is not a line break', 'expected': '2 bullets', 'actual': '1', 'code': 'func testCRLF() {}'},
                                      {'summary': ''}, 'junk'])
        self.assertEqual([d['summary'] for d in first], ['CRLF is not a line break'])
        again = runner.clean_defects([{'test': 'testCRLF', 'summary': 'Windows line endings are ignored'}, {'test': 'testB', 'summary': 'B'}], first)
        self.assertEqual([d['summary'] for d in again], ['Windows line endings are ignored', 'B'])

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_a_rewrite_is_told_which_tests_failed_and_why_and_records_the_defects_it_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            failing = shlex.join([sys.executable, '-c', 'import sys; print("FAILED tests/test_app.py::test_crlf - assert 1 == 2"); sys.exit(1)'])
            self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': 'test'}], failing)
            job_file = root / 'job.json'
            job = json.loads(job_file.read_text())
            self.assertEqual(job['test_validation']['failures'], [{'test': 'tests/test_app.py::test_crlf', 'message': 'assert 1 == 2'}])
            job['debug_feedback'] = 'the CRLF case is the code, I think'
            job_file.write_text(json.dumps(job))
            self.prompts = []
            passing = shlex.join([sys.executable, '-c', 'print("ok")'])
            result = {'summary': 'Reworked', 'files': [{'path': 'tests/test_app.py', 'content': 'test2'}],
                      'defects': [{'test': 'test_crlf', 'summary': 'CRLF is not treated as a line break', 'expected': 'two items', 'actual': 'one', 'code': 'def test_crlf(): ...'}]}
            job = self.run_job(root, 'test-audit', [], passing, result=result)
            prompt = self.prompts[-1]
            self.assertIn('tests/test_app.py::test_crlf: assert 1 == 2', prompt)
            self.assertIn('Decide for each failure whether the TEST or the CODE is wrong', prompt)
            self.assertIn('the CRLF case is the code, I think', prompt)
            self.assertEqual(job['status'], 'review-needed')
            self.assertEqual(job['production_defects'][0]['summary'], 'CRLF is not treated as a line break')
            self.assertNotIn('debug_feedback', job)  # said once

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_a_run_stopped_while_checking_the_tests_resumes_at_checking_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            command = shlex.join([sys.executable, '-c', 'raise SystemExit(0)'])
            with patch.object(runner, 'run_validation', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': 'test'}], command)
            saved = json.loads((root / 'job.json').read_text())
            self.assertEqual(saved['test_validation']['kind'], 'pending')  # persisted before the check began
            with patch.object(runner, 'run_llm', side_effect=AssertionError('no model on a resume')):
                job = self.run_job(root, 'test-audit', [], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertTrue(job['test_validation']['passed'])

    def test_model_cannot_reclassify_design_or_coverage_as_production(self):
        for requested, expected in [('design', 'feature-design'), ('coverage', 'test-audit')]:
            with self.subTest(kind=requested):
                payload = new_job.issue_payload_for_job(requested, {'recommended_job_type': 'feature-plan'}, None, None, False)
                self.assertEqual(payload[-1], expected)

    def test_untracked_test_files_are_preserved_even_in_new_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runner.subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'tests/new').mkdir(parents=True)
            original = root / 'tests/new/test_app.py'
            original.write_text('user work')
            job = self.run_job(root, 'test-audit', [{'path': 'tests/new/test_app.py', 'content': 'changed'}])
            self.assertEqual(job['status'], 'human-needed')
            self.assertEqual(original.read_text(), 'user work')

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_retry_updates_owned_tests_but_preserves_later_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            failing = shlex.join([sys.executable, '-c', 'raise SystemExit(1)'])
            passing = shlex.join([sys.executable, '-c', 'print("passed")'])
            path = 'tests/test_app.py'
            first = self.run_job(root, 'test-audit', [{'path': path, 'content': '# first test'}], failing)
            self.assertEqual(first['status'], 'human-needed')
            second = self.run_job(root, 'test-audit', [{'path': path, 'content': '# revised test'}], passing, dirty=[path])
            self.assertEqual(second['status'], 'review-needed')
            (root / path).write_text('# user edit')
            third = self.run_job(root, 'test-audit', [{'path': path, 'content': '# unwanted replacement'}], passing, dirty=[path])
            self.assertEqual(third['status'], 'human-needed')
            self.assertEqual((root / path).read_text(), '# user edit')

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_build_outputs_are_confined_to_validation_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import shlex
            code = 'from pathlib import Path; Path(".build").mkdir(); Path(".build/result").write_text("compiled")'
            command = shlex.join([sys.executable, '-c', code])
            job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': '# test'}], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertFalse((root / '.build').exists())

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_web_launch_environment_uses_the_validation_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            import os
            import shlex
            code = '''import os
from pathlib import Path
work = Path.cwd()
assert Path(os.environ['ORCHESTRATOR_PROJECT_ROOT']) == work
assert Path(os.environ['ORCHESTRATOR_RUNTIME_DIR']) == work / '.orchestrator'
assert Path(os.environ['AI_RUNTIME_DIR']) == work / '.orchestrator'
assert 'ORCHESTRATOR_CONFIG' not in os.environ
assert os.environ['PYTHONPATH'].split(os.pathsep)[0] == str(work / 'orchestrator/scripts')
'''
            command = shlex.join([sys.executable, '-c', code])
            inherited = {'ORCHESTRATOR_PROJECT_ROOT': str(root), 'ORCHESTRATOR_RUNTIME_DIR': str(root / '.orchestrator'),
                         'AI_RUNTIME_DIR': str(root / '.orchestrator'), 'ORCHESTRATOR_CONFIG': str(root / '.orchestrator/project.json'),
                         'PYTHONPATH': str(root / 'orchestrator/scripts') + os.pathsep + '/other/package'}
            with patch.dict('os.environ', inherited):
                job = self.run_job(root, 'test-audit', [{'path': 'tests/test_app.py', 'content': '# test'}], command)
            self.assertEqual(job['status'], 'review-needed', job.get('last_error'))

    def test_clarification_and_malformed_outputs_stop_for_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            job = self.run_job(root, "feature-design", [], result={"clarification_needed": "Which screen?"})
            self.assertEqual(job["human_clarification_question"], "Which screen?")
            self.assertEqual(job["status"], "human-needed")
            self.assertIsNone(job["worker_pid"])
            job = self.run_job(root, "feature-design", [], result={"files": None})
            self.assertIn("must return", job["last_error"])

    def test_uppercase_visual_outputs_remain_valid_design_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            job = self.run_job(root, "feature-design", [{"path": "SCREEN.HTML", "content": "mockup"}])
            self.assertEqual(job["status"], "review-needed")
            self.assertEqual(job["design_artifacts"], ["docs/product/designs/job-1/SCREEN.HTML"])
            self.assertEqual(len(job["reference_artifacts"]), 1)
            self.assertEqual(job["reference_artifacts"][0]["role"], "design")

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_validation_copies_configuration_dependencies_and_coverage_reports(self):
        import os
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".orchestrator").mkdir()
            (root / ".orchestrator/project.json").write_text('{"name": "demo"}')
            (root / "node_modules").mkdir()
            (root / "node_modules/package.txt").write_text("dependency")
            (root / "nested").mkdir()
            (root / "nested/fixture.txt").write_text("fixture")
            code = 'import os\nfrom pathlib import Path\nassert Path(".orchestrator/project.json").is_file()\nassert Path("node_modules").is_symlink()\nassert Path("node_modules/package.txt").read_text() == "dependency"\nassert Path("nested/fixture.txt").read_text() == "fixture"\nPath(os.environ["COVERAGE_FILE"]).write_text("coverage database")\nPath("coverage.xml").write_text("xml report")\nPath("lcov.info").write_text("lcov report")\nPath("coverage").mkdir()\nPath("coverage/lcov.info").write_text("nested lcov report")\n'
            command = shlex.join([sys.executable, "-c", code])
            with patch("common.extract_commands", return_value=("unused", command)), \
                 patch.dict(os.environ, {"PYTHONPATH": ""}):
                job = self.run_job(root, "test-audit", [{"path": "tests/test_new.py", "content": "# test"}])
            self.assertEqual(job["status"], "review-needed", job.get("last_error"))
            reports = root / ".orchestrator/output/job-1/coverage"
            self.assertEqual((reports / ".coverage").read_text(), "coverage database")
            self.assertEqual((reports / "coverage.xml").read_text(), "xml report")
            self.assertEqual((reports / "lcov.info").read_text(), "nested lcov report")

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_tracked_tests_and_missing_owned_output_are_recorded(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runner.subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "tests").mkdir()
            (root / "tests/test_app.py").write_text("# previous")
            runner.subprocess.run(["git", "add", "tests/test_app.py"], cwd=root, check=True)
            job_path = root / "job.json"
            job_path.write_text(json.dumps({"job_id": "job-1", "type": "test-audit", "title": "Tests", "builder": "fake",
                                            "output_digests": {"tests/missing.py": "old-digest"}}))
            job = self.run_job(root, "test-audit", [{"path": "tests/test_app.py", "content": "# replacement"}],
                               shlex.join([sys.executable, "-c", "pass"]))
            self.assertEqual(job["status"], "review-needed", job.get("last_error"))
            self.assertEqual(job["ai_modified_files"], ["tests/test_app.py"])
            self.assertEqual(job["ai_untracked_files"], [])

    def test_worker_routes_scoped_jobs_before_the_production_pipeline(self):
        import worker_run
        for kind in ("feature-design", "test-audit", "design", "coverage"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "job.json"
                path.write_text(json.dumps({"type": kind}))
                with patch.object(runner, "execute_nonproduction_job") as execute, \
                     patch.object(worker_run, "run_builder") as builder:
                    worker_run.execute_job(path)
                execute.assert_called_once_with(path)
                builder.assert_not_called()

    def test_generic_builder_refuses_nonproduction_jobs(self):
        for kind in ('feature-design', 'test-audit'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'job.json'
                path.write_text(json.dumps({'type': kind}))
                with self.assertRaisesRegex(RuntimeError, 'production builder is disabled'):
                    run_builder.run_builder(path)

    def test_remote_dispatch_is_refused_before_syncing_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'job.json'
            path.write_text(json.dumps({'type': 'test-audit'}))
            with patch.object(schedule_job, 'sync_code_to_remote') as sync:
                with self.assertRaisesRegex(RuntimeError, 'local worker'):
                    schedule_job.dispatch_job(path, {'execution_mode': 'ssh'}, {}, False)
            sync.assert_not_called()

    def test_scoped_scheduler_rejects_a_remote_only_machine_pool(self):
        with self.assertRaisesRegex(RuntimeError, 'local worker'):
            schedule_job.schedule_ready_group({'type': 'feature-design'}, [{'execution_mode': 'ssh'}])


class PlannedCaseTests(unittest.TestCase):
    CASES = [{'id': 'TC-81-01', 'title': 'Produce fresh baseline coverage measurement', 'type': 'integration', 'expected': 'a figure'},
             {'id': 'TC-81-05', 'title': 'Parser handles empty lists', 'type': 'functionality', 'expected': 'no crash'},
             {'id': 'TC-81-13', 'title': 'Upload rejects oversize payloads', 'type': 'integration', 'expected': 'an error'},
             {'id': 'TC-81-14', 'title': 'Loader resets after failures', 'type': 'functionality', 'expected': 'idle'}]

    def run_rounds(self, answers):
        """Run a Tests job whose builder gives `answers` in turn; the checking of the files is stubbed out."""
        self.prompts = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            job_path = root / 'job.json'
            job_path.write_text(json.dumps({'job_id': 'job-1', 'type': 'test-audit', 'title': 'Audit', 'builder': 'fake',
                                            'plan': {'test_cases': self.CASES}}))

            def answer(*args, **kwargs):
                self.prompts.append(args[1])
                return json.dumps(answers[len(self.prompts) - 1]), 'fake', 'session'

            def validate(job, out):
                job['test_validation'] = {'kind': 'passed', 'passed': True}
            config = SimpleNamespace(test_command=None, stack=SimpleNamespace(test_command=None))
            with ExitStack() as stack:
                stack.enter_context(patch.multiple(runner, ROOT=root, OUTPUT_DIR=root / '.orchestrator' / 'output', PROJECT_CONFIG=config))
                stack.enter_context(patch.multiple(reference_artifacts, ROOT=root, OUTPUT_DIR=root / '.orchestrator' / 'output'))
                stack.enter_context(patch.object(runner, 'get_repo_state', return_value={'modified': [], 'untracked': []}))
                stack.enter_context(patch.object(runner, 'run_llm', side_effect=answer))
                stack.enter_context(patch.object(runner, 'run_validation', side_effect=validate))
                runner.execute_nonproduction_job(job_path)
            return json.loads(job_path.read_text())

    def test_cases_left_without_a_test_get_one_more_round_and_the_rest_are_explained(self):
        job = self.run_rounds([
            {'summary': 'first', 'files': [{'path': 'tests/test_a.py', 'content': '# TC-81-05\ndef test_a(): pass\n'}]},
            {'summary': 'second', 'files': [{'path': 'tests/test_b.py', 'content': '# TC-81-13\ndef test_b(): pass\n'}],
             'skipped': [{'case': 'TC-81-14', 'reason': 'It needs a production change to be testable'}]}])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(job['status'], 'review-needed')
        listed = self.prompts[0].split('## Planned test cases')[1]
        self.assertTrue(all(case in listed for case in ('TC-81-05', 'TC-81-13', 'TC-81-14')))
        self.assertNotIn('TC-81-01', listed)  # a measurement is not a test to write
        self.assertIn('TC-81-13', self.prompts[1].split('## Planned test cases')[1])
        self.assertNotIn('TC-81-05', self.prompts[1].split('## Planned test cases')[1])  # already covered
        self.assertEqual(job['skipped_cases'], [{'case': 'TC-81-14', 'reason': 'It needs a production change to be testable'}])
        self.assertEqual(sorted(job['output_digests']), ['tests/test_a.py', 'tests/test_b.py'])

    def test_when_every_case_is_covered_or_explained_there_is_no_second_round(self):
        job = self.run_rounds([{'summary': 'one', 'files': [{'path': 'tests/test_a.py', 'content': '# TC-81-05 TC-81-13\ndef test_a(): pass\n'}],
                                'skipped': [{'case': 'TC-81-14', 'reason': 'Manual UI behaviour'}]}])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(job['status'], 'review-needed')
        self.assertFalse(job.get('cases_round'))

    def test_a_second_round_that_cannot_help_skips_the_rest_with_its_reason_and_keeps_the_job_going(self):
        job = self.run_rounds([
            {'summary': 'first', 'files': [{'path': 'tests/test_a.py', 'content': '# TC-81-05\ndef test_a(): pass\n'}]},
            {'clarification_needed': 'These need the view model split from the view'}])
        self.assertEqual(job['status'], 'review-needed')
        self.assertEqual({c['case'] for c in job['skipped_cases']}, {'TC-81-13', 'TC-81-14'})
        self.assertIn('view model', job['skipped_cases'][0]['reason'])

    def test_skipped_reasons_are_plain_trimmed_and_only_for_real_case_ids(self):
        out = runner.clean_skipped([{'case': 'TC-1-01', 'reason': '  needs   a change '}, {'case': 'nope', 'reason': 'x'}, {'case': 'TC-1-02'}, 'junk'],
                                   [{'case': 'TC-1-01', 'reason': 'old'}])
        self.assertEqual(out, [{'case': 'TC-1-01', 'reason': 'needs a change'}])


class RemeasureCoverageTests(unittest.TestCase):
    def run_it(self, saved_before, saved_after, returncode=0, raises=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'out'
            out.mkdir()
            state = root / '.orchestrator' / 'state'
            state.mkdir(parents=True)
            (state / 'coverage.json').write_text(json.dumps({'overall_coverage_pct': saved_before}))

            def measure(argv, **kwargs):
                self.call = (argv, kwargs)
                if raises:
                    raise raises
                (state / 'coverage.json').write_text(json.dumps({'overall_coverage_pct': saved_after}))
                return SimpleNamespace(returncode=returncode, stdout='measured', stderr='')
            job = {}
            with patch.object(runner, 'ROOT', root), patch.object(runner.subprocess, 'run', side_effect=measure):
                runner.remeasure_coverage(job, out)
            return job['coverage']

    def test_passing_tests_trigger_a_new_measurement_of_this_project_and_record_before_and_after(self):
        record = self.run_it(61.5, 70.25)
        self.assertEqual((record['before'], record['after']), (61.5, 70.25))
        self.assertEqual(Path(self.call[1]['env']['ORCHESTRATOR_PROJECT_ROOT']), self.call[1]['cwd'])  # never the checkout the orchestrator runs from
        self.assertEqual(self.call[0][-1], 'coverage')

    def test_a_measurement_that_fails_is_noted_and_never_stops_the_job(self):
        failed = self.run_it(61.5, 50, returncode=1)
        self.assertEqual(failed['after'], None)
        self.assertIn("couldn't be measured", failed['error'])
        self.assertIn("wasn't measured", self.run_it(61.5, 50, raises=OSError('no python'))['error'])


if __name__ == '__main__':
    unittest.main()
