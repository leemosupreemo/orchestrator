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
            self.assertEqual(kwargs['protected_paths'], [root])
            return json.dumps(result if result is not None else {'summary': 'Scoped result', 'files': files}), 'fake', 'session'

        with ExitStack() as stack:
            stack.enter_context(patch.multiple(runner, ROOT=root, OUTPUT_DIR=out, PROJECT_CONFIG=config))
            stack.enter_context(patch.multiple(reference_artifacts, ROOT=root, OUTPUT_DIR=out))
            stack.enter_context(patch.object(runner, 'get_repo_state', return_value={'modified': list(dirty), 'untracked': []}))
            stack.enter_context(patch.object(runner, 'run_llm', side_effect=answer))
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
                         'xcodebuild -IDEPackageSupportDisableManifestSandbox=1 -IDEPackageSupportDisablePluginExecutionSandbox=1 test -scheme A')
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
            with patch.object(runner, 'run_llm', side_effect=AssertionError('no model on a retry')):
                job = self.run_job(root, 'test-audit', [], command)
            self.assertEqual(job['status'], 'review-needed')
            self.assertTrue(job['test_validation']['passed'])

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


if __name__ == '__main__':
    unittest.main()
