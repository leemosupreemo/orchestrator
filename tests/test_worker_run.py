from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator/scripts'))
import worker_run as worker
from run_builder import BuilderClarificationNeeded


class WorkerRunTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    @contextlib.contextmanager
    def pipeline(self, extra=None, results=None, states=None, interactive=False, config=None):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            path = root / 'jobs/job.json'
            path.parent.mkdir()
            job = {'job_id': 'job-1', 'title': 'Fix demo', 'type': 'bug-fix', 'builder': 'fake', 'reviewer': 'fake',
                   'plan': {'summary': 'Plan', 'likely_files': []}, **(extra or {})}
            worker.write_json(path, job)
            settings = SimpleNamespace(code_host_mode='git', uses_github=False, firebase_distribution=False,
                                       delivery_method=None, test_command='all tests', base_branch='main', git_remote=None)
            for key, value in (config or {}).items():
                setattr(settings, key, value)
            stack.enter_context(patch.multiple(worker, ROOT=root, OUTPUT_DIR=root / 'output', LOGS_DIR=root / 'logs', PROJECT_CONFIG=settings))
            mocks = {}
            for name, kwargs in {
                'StatusBar': {}, 'print_phase': {}, 'prepare_git_branch': {'return_value': ('work', 'main')},
                'update_issue_status': {}, 'gh_comment': {}, 'run_shell': {}, 'send_notifications': {},
                'open_or_update_pr': {'return_value': (12, 'https://example.com/pr/12')},
                'push_branch_for_review': {'return_value': 'https://example.com/merge'},
                'update_product_requirements': {}, 'print_status_report': {}, 'print_clarification_report': {},
                'interactive': {'return_value': interactive},
                'get_repo_state': {'side_effect': states or [{'modified': [], 'untracked': []}] * 20},
                'run_builder': {'side_effect': results or [(True, True, Path('summary'), {'summary': 'Done', 'hypothesis': 'Cause'})]},
            }.items():
                mocks[name] = stack.enter_context(patch.object(worker, name, **kwargs))
            mocks['analytics'] = stack.enter_context(patch('orchestrator.analytics.track_job_run'))
            mocks['event'] = stack.enter_context(patch('orchestrator.integrations_sync.notify_job_event'))
            mocks['debug'] = stack.enter_context(patch('debug_job.run_debug_iteration'))
            stack.enter_context(patch('orchestrator.run_check.runs_from', return_value=False))
            yield path, mocks

    def test_success_tracks_only_new_changes_and_uses_plain_git_review(self):
        states = [{'modified': ['user.py'], 'untracked': ['user.txt']},
                  {'modified': ['user.py', 'app.py'], 'untracked': ['user.txt', 'new.py']}]
        with self.pipeline(states=states) as (path, calls):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job['status'], 'review-needed')
            self.assertEqual(job['ai_modified_files'], ['app.py'])
            self.assertEqual(job['ai_untracked_files'], ['new.py'])
            self.assertEqual(job['builder_summary'], 'Done')
            self.assertEqual(job['builder_hypothesis'], 'Cause')
            self.assertEqual(job['merge_request_url'], 'https://example.com/merge')
            shell = [c.args[0] for c in calls['run_shell'].call_args_list]
            self.assertTrue(any('fix: Fix demo' in command for command in shell))
            self.assertTrue(any('--branch work --base main' in command for command in shell))
            self.assertEqual(calls['analytics'].call_args.kwargs['status'], 'succeeded')

    def test_interactive_clarification_answers_and_resumes(self):
        with self.pipeline(results=[BuilderClarificationNeeded('Which color?'), (True, True, Path('summary'), {})], interactive=True) as (path, calls):
            with patch('builtins.input', side_effect=['blue', '']):
                worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['status'], 'review-needed')
            self.assertEqual(calls['run_builder'].call_count, 2)
            self.assertFalse(calls['run_builder'].call_args.kwargs['resume'])

    def test_completed_feature_tasks_do_not_require_a_builder_result(self):
        task = {'title': 'Finished', 'description': 'done', 'acceptance_criteria': [], 'likely_files': []}
        with self.pipeline({'type': 'feature-plan', 'approved': True, 'plan': {'tasks': [task]}, 'completed_task_indices': [0]}) as (path, calls):
            worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['status'], 'review-needed')
            calls['run_builder'].assert_not_called()

    def test_branch_modes_and_existing_new_branch_checkout(self):
        config = SimpleNamespace(base_branch='main', git_remote='https://example.com/repo')
        with patch.object(worker, 'PROJECT_CONFIG', config), patch.object(worker, 'run') as run, \
             patch.object(worker, 'current_git_branch', return_value='existing'), \
             patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as proc:
            self.assertEqual(worker.prepare_git_branch({'title': 'Demo', 'job_id': 'id-a', 'branch_mode': 'manual'}, None), ('existing', 'main'))
            run.assert_not_called()
            self.assertEqual(worker.prepare_git_branch({'title': 'Demo', 'job_id': 'id-a', 'branch_mode': 'current'}, None), ('existing', 'main'))
            self.assertEqual(run.call_args.args[0], ['git', 'pull', '--ff-only', 'origin', 'existing'])
            self.assertEqual(worker.prepare_git_branch({'title': 'Demo', 'job_id': 'id-a'}, 12)[0], 'ai/issue-12-demo')
            self.assertEqual(proc.call_args.args[0], ['git', 'pull', 'origin', 'ai/issue-12-demo'])
        config.git_remote = None
        with patch.object(worker, 'PROJECT_CONFIG', config), patch.object(worker, 'run') as run, \
             patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            worker.prepare_git_branch({'title': 'Demo', 'job_id': 'id-a', 'branch': 'explicit'}, None)
            self.assertEqual(run.call_args.args[0], ['git', 'checkout', '-f', '-b', 'explicit'])
            worker.prepare_git_branch({'title': 'Demo', 'job_id': 'id-a', 'branch': 'explicit', 'branch_mode': 'current'}, None)
        with patch.object(worker.subprocess, 'check_output', return_value=b' branch\n'):
            self.assertEqual(worker.current_git_branch(), 'branch')
        self.assertEqual(worker.normalize_branch_mode('manual (no git actions)'), 'manual')
        self.assertEqual(worker.normalize_branch_mode('current-branch (git pull)'), 'current')
        for active in (True, False):
            with patch.object(worker.os, 'kill', side_effect=None if active else OSError()):
                self.assertEqual(worker.is_process_running(123), active)

    def test_reports_use_builder_file_summary(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'OUTPUT_DIR', Path(tmp)):
            directory = Path(tmp) / 'job-1'
            directory.mkdir()
            (directory / 'builder_summary.md').write_text('{"summary": "File accomplishment"}')
            worker.print_status_report({'job_id': 'job-1'}, True, True)
        self.assertIn('File accomplishment', self.output.getvalue())

    def test_reports_cover_success_failure_and_clarification_details(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'OUTPUT_DIR', Path(tmp)), \
             patch.object(worker.os, 'get_terminal_size', return_value=(100, 30)):
            job = {'job_id': 'job-1', 'type': 'feature-plan', 'builder_summary': 'Built the feature'}
            worker.print_status_report(job, True, True, 12, 'https://example.com/pr', 'SUCCESS today')
            worker.print_status_report({'job_id': 'job-1', 'debug_history': [{'action': 'Fixed issue'}]}, True, True, 12)
            worker.print_status_report({'job_id': 'job-1', 'merge_request_url': 'https://example.com/merge'}, True, True)
            worker.print_status_report({'job_id': 'job-1', 'plan': {'summary': 'Planned fix'}}, True, True)
            directory = Path(tmp) / 'job-1'
            directory.mkdir()
            (directory / 'builder_summary.md').write_text('not json')
            worker.print_status_report({'job_id': 'job-1'}, True, True)
            worker.print_status_report({'job_id': 'job-1'}, False, True, distributed_status='FAILED')
            worker.print_status_report({'job_id': 'job-1'}, True, False)
            worker.print_clarification_report({'job_id': 'job-1'}, 'Need a decision')
        with patch.object(worker.os, 'get_terminal_size', side_effect=OSError()):
            worker.print_clarification_report({'job_id': 'job-1', 'human_clarification_question': 'Which screen?'})
        for text in (None, 'SUCCESS details', 'FAILED details', 'Skipped task', 'Not Triggered', 'custom'):
            result = worker.format_distributed_status(text)
            self.assertIn(text or '', __import__('re').sub(r'\x1b\[[0-9;]*m', '', result))
        self.assertIn('Need a decision', self.output.getvalue())
        self.assertIn('Planned fix', self.output.getvalue())

    def test_followups_notifications_checkpoint_and_stale_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'job.json'
            for kind, logs, recent in (('feature-plan', 'invalid', 'log.txt'), ('bug-fix', ['log.txt'], 'log.txt'), ('bug-fix', [], None)):
                job = {'job_id': 'job-1', 'type': kind, 'last_manual_log_paths': logs}
                with patch.object(worker, 'find_latest_runtime_log', return_value=recent), patch('debug_job.run_debug_iteration') as debug:
                    worker.trigger_followup_iteration(path, job, 'Feedback')
                self.assertEqual(job['iteration'], 1)
                debug.assert_called_once_with(path)
            with patch.object(worker.subprocess, 'run') as process:
                worker.send_notifications({}, 'Title', 'Message')
                worker.send_notifications({'job_id': 'job-1'}, 'Title', 'Message', 'Summary')
                self.assertEqual(process.call_args.args[0][-1], 'Summary')
            with patch.object(worker.subprocess, 'run', side_effect=OSError()):
                worker.send_notifications({}, 'Title', 'Message')
            job = {}
            worker.checkpoint_task(job, 0, 1, 'Demo', [])
            self.assertEqual(job, {})
            with patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(stdout='abc123\n')) as process:
                worker.checkpoint_task(job, 0, 1, 'Demo', ['app.py'])
                self.assertEqual(process.call_count, 3)
                self.assertEqual(job['task_commits'], {'0': 'abc123'})
            with patch.object(worker.subprocess, 'run', side_effect=OSError()):
                worker.checkpoint_task({}, 0, 1, 'Demo', ['app.py'])
            stale = root / 'job_task_1.json'
            stale.write_text('scratch')
            keep = root / 'other_task_1.json'
            keep.write_text('other')
            worker.remove_task_views(path)
            self.assertFalse(stale.exists())
            self.assertTrue(keep.exists())
            stale.mkdir()
            worker.remove_task_views(path)
            self.assertTrue(stale.is_dir())
        self.assertEqual(worker.unpack_builder_result((True, False, Path('summary')))[-1], {})

    def test_resume_existing_worker_and_scoped_routing(self):
        with self.pipeline({'worker_pid': os.getpid() + 1}) as (path, calls):
            with patch.object(worker, 'is_process_running', return_value=True), \
                 patch.object(worker.subprocess, 'run', side_effect=KeyboardInterrupt):
                worker.execute_job(path, resume=True)
            calls['run_builder'].assert_not_called()
        with self.pipeline({'type': 'design', 'worker_pid': os.getpid()}) as (path, calls):
            with patch('nonproduction_job.execute_nonproduction_job') as scoped:
                worker.execute_job(path, resume=True)
            scoped.assert_called_once_with(path)
            calls['run_builder'].assert_not_called()
        with self.pipeline({'worker_pid': os.getpid() + 1}) as (path, calls):
            with patch.object(worker, 'is_process_running', return_value=False):
                worker.execute_job(path, resume=True)
            self.assertEqual(calls['run_builder'].call_count, 1)

    def test_self_checkout_guard_stops_before_branch_changes(self):
        with self.pipeline() as (path, calls):
            with patch('orchestrator.run_check.runs_from', return_value=True), \
                 patch('orchestrator.run_check.switches_branches', return_value=True):
                worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['status'], 'human-needed')
            calls['prepare_git_branch'].assert_not_called()

    def feature_job(self, **extra):
        return {'type': 'feature-plan', 'approved': True, 'plan': {'tasks': [
            {'title': 'Add screen', 'description': 'Screen', 'acceptance_criteria': ['Works'],
             'likely_files': ['app.py'], 'tests': ['test_screen']}],
            'test_cases': [{'task': 1}, {'task': 2}, {}, 'invalid']}, **extra}

    def test_feature_task_success_records_builder_sessions_and_commit(self):
        states = [{'modified': [], 'untracked': []}, {'modified': ['app.py'], 'untracked': ['test.py']},
                  {'modified': [], 'untracked': []}]
        with self.pipeline(self.feature_job(), states=states) as (path, calls):
            def builder(task_path, resume=False):
                task = worker.read_json(task_path)
                self.assertEqual(task['plan']['test_cases'], [{'task': 1}, {}])
                task['actual_builder_used'] = 'selected'
                task['llm_sessions'] = [{'id': 'session'}]
                worker.write_json(task_path, task)
                return True, True, Path('summary'), {'action': 'patch', 'test_command': 'all tests'}
            calls['run_builder'].side_effect = builder
            with patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(stdout='abc123')):
                worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job['completed_task_indices'], [0])
            self.assertEqual(job['actual_builder_used'], 'selected')
            self.assertEqual(job['llm_sessions'], [{'id': 'session'}])
            self.assertEqual(job['task_commits'], {'0': 'abc123'})
            self.assertFalse(list(path.parent.glob('*_task_*.json')))

    def two_task_job(self, **extra):
        task = lambda n: {'title': f'Task {n}', 'description': 'd', 'acceptance_criteria': ['ok'], 'likely_files': ['app.py'], 'tests': ['test_screen']}  # noqa: E731
        return {'type': 'feature-plan', 'approved': True, 'plan': {'tasks': [task(1), task(2)], 'test_cases': []}, **extra}

    GROWN = {'reasons': ['added 950 lines (about 400 is typical)'], 'added': 950, 'files': ['a.py', 'b.py', 'c.py', 'd.py']}
    EDITED = [{'modified': [], 'untracked': []}] + [{'modified': ['app.py'], 'untracked': []}] * 10
    PATCHED = (True, True, Path('summary'), {'action': 'patch'})

    def test_a_task_that_outgrew_its_plan_pauses_the_job_before_the_next_task(self):
        with self.pipeline(self.two_task_job(), results=[self.PATCHED] * 2, states=self.EDITED) as (path, calls), \
             patch.object(worker, 'checkpoint_task'), patch.object(worker, 'check_task_growth', return_value=self.GROWN):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(calls['run_builder'].call_count, 1)  # Task 2 was not started
            self.assertEqual((job['status'], job['completed_task_indices'], job['worker_pid']), ('human-needed', [0], None))
            self.assertEqual(job['task_growth']['task'], 0)
            self.assertEqual(job['task_growth']['text'], 'Task 1 added 950 lines (about 400 is typical).')
            self.assertFalse(job.get('last_error'))  # a pause to decide, not a failure
            calls['send_notifications'].assert_called_once()
            calls['debug'].assert_not_called()

    def test_a_task_inside_its_plan_goes_straight_on_to_the_next(self):
        with self.pipeline(self.two_task_job(), results=[self.PATCHED] * 2, states=self.EDITED) as (path, calls), \
             patch.object(worker, 'checkpoint_task'), patch.object(worker, 'check_task_growth', return_value=None) as check:
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(calls['run_builder'].call_count, 2)
            self.assertEqual(job['completed_task_indices'], [0, 1])
            self.assertNotIn('task_growth', job)
            check.assert_called_once()  # not after the last task: review's scope check covers it

    def test_resuming_a_paused_job_is_the_person_saying_keep_going(self):
        paused = self.two_task_job(status='human-needed', completed_task_indices=[0], task_growth={'task': 0, 'text': 'Task 1 grew.'})
        with self.pipeline(paused, results=[self.PATCHED], states=self.EDITED) as (path, calls), \
             patch.object(worker, 'checkpoint_task'), patch.object(worker, 'check_task_growth') as check:
            worker.execute_job(path, resume=True)
            job = worker.read_json(path)
            self.assertEqual(calls['run_builder'].call_count, 1)  # only Task 2
            self.assertEqual(job['completed_task_indices'], [0, 1])
            self.assertNotIn('task_growth', job)
            check.assert_not_called()  # Task 1 is not measured again, and Task 2 is the last

    def test_growth_is_measured_from_the_commit_the_task_made(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = lambda *a: sp.run(['git', *a], cwd=root, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            (root / 'README').write_text('x\n'); git('add', '.'); git('commit', '-qm', 'base')
            (root / 'app.py').write_text('x = 1\n' * 900)
            for n in range(5):
                (root / f'extra{n}.py').write_text('y = 1\n')
            git('add', '.'); git('commit', '-qm', 'task')
            job = {'task_commits': {'0': git('rev-parse', 'HEAD')}}
            task = {'title': 't', 'likely_files': ['app.py']}
            with patch.object(worker, 'ROOT', root):
                grown = worker.check_task_growth(job, 0, task)
                self.assertEqual(len(grown['reasons']), 2)  # 905 lines, and 5 files nobody planned
                self.assertIsNone(worker.check_task_growth(job, 1, task))  # no commit recorded: nothing to measure
                self.assertIsNone(worker.check_task_growth({'task_commits': {'0': 'deadbeef'}}, 0, task))  # not in this repository

    def test_feature_validation_refuses_phantom_or_unrelated_test_success(self):
        cases = [
            ({'action': 'patch'}, ['other.py'], True, 'expected files'),
            ({'action': 'patch', 'test_command': 'unrelated'}, ['app.py'], True, 'unrelated tests'),
            ({'action': 'investigate'}, [], True, 'not implemented'),
            ({'action': 'patch'}, ['app.py'], False, None),
        ]
        for output, files, success, error in cases:
            with self.subTest(output=output, success=success), \
                 self.pipeline(self.feature_job(), results=[(success, success, Path('summary'), output)],
                               states=[{'modified': [], 'untracked': []}, {'modified': files, 'untracked': []},
                                       {'modified': [], 'untracked': []}]) as (path, calls):
                worker.execute_job(path)
                job = worker.read_json(path)
                self.assertEqual(job['completed_task_indices'], [])
                if error:
                    self.assertIn(error, job['last_error'])
                calls['debug'].assert_called_once_with(path)

    def test_feature_builder_infers_changes_accepts_named_tests_and_handles_missing_metadata(self):
        for output in ({'test_command': 'pytest test_screen'}, {'action': 'add_logging'}, {}):
            with self.subTest(output=output), \
                 self.pipeline(self.feature_job(), results=[(True, True, Path('summary'), output)],
                               states=[{'modified': [], 'untracked': []}, {'modified': ['app.py'], 'untracked': []},
                                       {'modified': [], 'untracked': []}]) as (path, calls):
                if output:
                    def builder(task_path, resume=False):
                        task_path.unlink()
                        return True, True, Path('summary'), output
                    calls['run_builder'].side_effect = builder
                with patch.object(worker, 'checkpoint_task'):
                    worker.execute_job(path)
                self.assertEqual(worker.read_json(path)['completed_task_indices'], [0])

    def test_feature_clarification_and_automation_errors_pause_without_debugging(self):
        for failure, active, answer in ((RuntimeError('offline'), False, None),
                                       (BuilderClarificationNeeded('Which?'), False, None),
                                       (BuilderClarificationNeeded('Which?'), True, ''),
                                       (BuilderClarificationNeeded('Which?'), True, EOFError())):
            with self.subTest(failure=failure, active=active), \
                 self.pipeline(self.feature_job(), results=[failure], interactive=active) as (path, calls):
                with patch('builtins.input', side_effect=answer if isinstance(answer, Exception) else [answer]):
                    worker.execute_job(path)
                self.assertEqual(worker.read_json(path)['status'], 'human-needed')
                calls['debug'].assert_not_called()
        with self.pipeline(self.feature_job(), results=[BuilderClarificationNeeded('Which?'),
                           (True, True, Path('summary'), {'action': 'patch'})], interactive=True,
                           states=[{'modified': [], 'untracked': []}, {'modified': ['app.py'], 'untracked': []},
                                   {'modified': [], 'untracked': []}]) as (path, calls):
            with patch('builtins.input', side_effect=['blue', '']), patch.object(worker, 'checkpoint_task'):
                worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['completed_task_indices'], [0])
            self.assertEqual(calls['run_builder'].call_count, 2)

    def test_single_builder_clarification_pause_headless_yolo_and_input_errors(self):
        for active, extra, answer in ((False, {}, None), (True, {'headless': True}, None),
                                     (True, {'is_yolo': True}, None), (True, {}, ''), (True, {}, KeyboardInterrupt())):
            with self.subTest(extra=extra, active=active), \
                 self.pipeline(extra, results=[BuilderClarificationNeeded('Which?')], interactive=active) as (path, calls):
                with patch('builtins.input', side_effect=answer if isinstance(answer, BaseException) else [answer]):
                    worker.execute_job(path)
                self.assertEqual(worker.read_json(path)['status'], 'human-needed')
                calls['debug'].assert_not_called()

    def test_validation_failure_debug_child_and_history_updates(self):
        for child, history in ((True, []), (False, [{'result': 'pending'}]), (False, [{'result': 'done'}])):
            with self.subTest(child=child, history=history), \
                 self.pipeline({'debug_history': history}, results=[(False, True, Path('summary'))]) as (path, calls):
                with patch.dict(os.environ, {'AI_DEBUG_CHILD': '1' if child else '0'}):
                    worker.execute_job(path)
                job = worker.read_json(path)
                self.assertEqual(job['status'], 'debugging')
                self.assertEqual(job['max_iterations'], 8)
                self.assertEqual(calls['debug'].call_count, int(not child))
                if history and history[-1]['result'] == 'pending':
                    self.assertFalse(job['debug_history'][-1]['result']['build_ok'])

    def test_success_github_delivery_and_best_effort_linked_apps(self):
        for delivery, error in ((0, None), (2, None), (None, OSError('delivery failed'))):
            with self.subTest(delivery=delivery), \
                 self.pipeline({'issue_number': 7, 'plan': {'likely_files': ['App.swift']}},
                               config={'uses_github': True, 'firebase_distribution': True, 'delivery_method': 'enterprise'}) as (path, calls):
                with patch.object(worker.subprocess, 'run', side_effect=error, return_value=SimpleNamespace(returncode=delivery)), \
                     patch('orchestrator.integrations_sync.notify_job_event', side_effect=RuntimeError('offline')):
                    worker.execute_job(path)
                job = worker.read_json(path)
                self.assertEqual(job['pr_number'], 12)
                self.assertEqual(job['status'], 'review-needed')
                distribution = calls['print_status_report'].call_args.kwargs['distributed_status']
                self.assertIn('SUCCESS' if delivery == 0 else 'FAILED', distribution)
        with self.pipeline(config={'firebase_distribution': True}) as (path, calls):
            worker.execute_job(path)
            self.assertIn('Non-iOS', calls['print_status_report'].call_args.kwargs['distributed_status'])

    def test_debug_success_preserves_debugging_and_records_iteration(self):
        for history in ([], [{'result': 'pending'}]):
            with self.subTest(history=history), self.pipeline({'status': 'debugging', 'iteration': 2, 'debug_history': history},
                                                           config={'uses_github': True}) as (path, calls):
                worker.execute_job(path)
                job = worker.read_json(path)
                self.assertEqual(job['status'], 'debugging')
                self.assertEqual(job['debug_phase'], 'propose')
                if history:
                    self.assertTrue(job['debug_history'][-1]['result']['build_ok'])
                self.assertTrue(any('debug(iter 2)' in c.args[0] for c in calls['run_shell'].call_args_list))

    def test_followup_prompts_features_bugs_and_interrupted_input(self):
        for kind, answer in (('feature', 'Needs button'), ('bug-fix', 'Still broken'), ('bug-fix', EOFError())):
            with self.subTest(kind=kind), self.pipeline({'type': kind}, interactive=True) as (path, calls):
                with patch('builtins.input', side_effect=answer if isinstance(answer, BaseException) else [answer]), \
                     patch.object(worker, 'trigger_followup_iteration') as follow:
                    worker.execute_job(path)
                self.assertEqual(follow.call_count, int(not isinstance(answer, BaseException)))
        with self.pipeline({'is_yolo': True}) as (path, calls):
            worker.execute_job(path)
            self.assertIn('All tasks completed', self.output.getvalue())

    def test_critical_failure_persists_human_needed_even_when_reporting_fails(self):
        for reporting_failure in (False, True):
            with self.subTest(reporting_failure=reporting_failure), self.pipeline(results=[RuntimeError('offline')]) as (path, calls):
                if reporting_failure:
                    calls['update_issue_status'].side_effect = [None, RuntimeError('report failed')]
                    calls['analytics'].side_effect = RuntimeError('analytics failed')
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    worker.execute_job(path)
                job = worker.read_json(path)
                self.assertEqual(job['status'], 'human-needed')
                self.assertEqual(job['last_error'], 'offline')
                self.assertIsNone(job['worker_pid'])
                self.assertEqual(calls['send_notifications'].call_args.args[1], 'Job Complete: FAILED')

    def test_remaining_tasks_continue_automatically_in_both_worker_modes(self):
        for kind, automatic in (('feature-plan', True), ('feature-plan', False), ('feature', True)):
            with self.subTest(kind=kind, automatic=automatic), \
                 self.pipeline({'type': kind, 'is_yolo': automatic, 'plan': {'tasks': [{'title': 'Pending'}]}},
                               states=[{'modified': [], 'untracked': []}, {'modified': ['new.py'], 'untracked': []}]) as (path, calls):
                execute = worker.execute_job
                with patch.object(worker, 'execute_job') as continuation, patch.object(worker.time, 'sleep'):
                    execute(path)
                self.assertEqual(continuation.call_count, int(automatic))
                if automatic:
                    continuation.assert_called_once_with(path, resume=True)
                self.assertTrue(any('feat: Fix demo' in c.args[0] for c in calls['run_shell'].call_args_list))

    def test_existing_iteration_limit_and_delivery_awaiting_final_yolo_task(self):
        with self.pipeline({'max_iterations': 3}, results=[(True, False, Path('summary'), {})]) as (path, calls):
            worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['max_iterations'], 3)
        with self.pipeline({'yolo': True, 'plan': {'likely_files': ['App.swift'], 'tasks': [{}]}},
                           config={'firebase_distribution': True}) as (path, calls):
            worker.execute_job(path)
            self.assertIn('awaiting final task', calls['print_status_report'].call_args.kwargs['distributed_status'])
        with self.pipeline() as (path, calls):
            calls['analytics'].side_effect = RuntimeError('offline')
            worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['status'], 'review-needed')

    def test_outer_clarification_handler_preserves_pause_details(self):
        with self.pipeline() as (path, calls):
            calls['prepare_git_branch'].side_effect = BuilderClarificationNeeded('Choose base branch')
            worker.execute_job(path)
            self.assertEqual(worker.read_json(path)['human_clarification_question'], 'Choose base branch')
            calls['print_clarification_report'].assert_called_once()
            self.assertEqual(calls['send_notifications'].call_args.args[1], 'Job Paused: Clarification Needed')

    def test_followup_keeps_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'find_latest_runtime_log', return_value=None), \
             patch('debug_job.run_debug_iteration'):
            job = {'job_id': 'job-1', 'debug_history': [{'iteration': 1}], 'iteration': 1}
            worker.trigger_followup_iteration(Path(tmp) / 'job.json', job, 'More feedback')
            self.assertEqual(len(job['debug_history']), 2)
            self.assertEqual(job['iteration'], 2)

    def test_product_requirement_update_is_best_effort_and_asks_in_scratch_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'job.json'
            worker.write_json(path, {'reviewer': 'review', 'allowed_models': ['review']})
            with patch('orchestrator.prd.Prd'), patch('orchestrator.prd.should_check', return_value=False), \
                 patch('orchestrator.prd.run_update') as update:
                worker.update_product_requirements(path)
            update.assert_not_called()
            for status in ('updated', 'nochange', 'rejected', 'error', 'skipped'):
                worker.write_json(path, {'reviewer': 'review', 'allowed_models': ['review']})
                def answer(model, prompt, **kwargs):
                    self.assertEqual(model, 'review')
                    self.assertTrue(kwargs['cwd'].is_dir())
                    self.assertNotEqual(kwargs['cwd'], worker.ROOT)
                    return 'model reply', 'review', 'session'
                def update(doc, job, ask):
                    self.assertEqual(ask('Check requirements'), 'model reply')
                    return {'status': status, 'summary': 'New requirement'}
                with self.subTest(status=status), patch('orchestrator.prd.Prd'), \
                     patch('orchestrator.prd.should_check', return_value=True), \
                     patch('orchestrator.prd.run_update', side_effect=update), patch('llm.run_llm', side_effect=answer):
                    worker.update_product_requirements(path)
                self.assertEqual(worker.read_json(path).get('prd_checked', False), status != 'skipped')
            with patch('orchestrator.prd.Prd', side_effect=OSError('document unavailable')):
                worker.update_product_requirements(path)
            self.assertIn('document unavailable', self.output.getvalue())

    def test_main_and_module_entrypoint_handle_stream_capabilities(self):
        with patch.object(sys, 'argv', ['worker', 'job.json', '--resume']), patch.object(worker, 'execute_job') as execute:
            worker.main()
            execute.assert_called_once_with(Path('job.json'), resume=True)
        class BrokenStream(io.StringIO):
            def reconfigure(self, **kwargs):
                raise OSError('unsupported stream')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'job.json'
            path.write_text('{"type": "design"}')
            for stdout in (io.StringIO(), BrokenStream()):
                with patch.object(sys, 'argv', ['worker', str(path)]), \
                     patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', io.StringIO()), \
                     patch('nonproduction_job.execute_nonproduction_job') as scoped:
                    runpy.run_module('worker_run', run_name='__main__')
                scoped.assert_called_once_with(path)
