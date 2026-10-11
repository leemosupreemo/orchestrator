from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator/scripts'))
import schedule_job as scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.machine = {'name': 'local', 'models': ['fake'], 'execution_mode': 'local', 'priority': 0}
        self.probe = {'reachable': True, 'repo_exists': True, 'disk_free_gb': 100}

    def test_load_and_normalize_jobs(self):
        with patch.object(scheduler, 'read_json', return_value={'machines': [self.machine, {'enabled': False}]}):
            self.assertEqual(scheduler.load_machines(), [self.machine])
        with patch.object(scheduler, 'read_json', return_value={'job_id': 'a'}):
            self.assertEqual(scheduler.load_job(Path('a.json')), {'job_id': 'a'})
        for source, kind in (({'source': 'manual'}, 'bug-fix'), ({'plan': {'repro_steps': []}}, 'bug-fix'),
                             ({'source': 'quick'}, 'quick-fix'), ({'job_id': 'quick-a'}, 'quick-fix'), ({}, 'feature-plan')):
            with self.subTest(source=source):
                normalized = scheduler.normalize_job(source)
                self.assertEqual(normalized['type'], kind)
                self.assertNotIn('task_groups', source)
                self.assertEqual(len(normalized['task_groups']), 1)
        for job, expected in (({}, 'planning'), ({'type': 'feature-plan', 'approved': True}, 'implementation'),
                              ({'type': 'bug-investigate'}, 'investigation'), ({'type': 'quick-fix'}, 'implementation')):
            self.assertEqual(scheduler.synthetic_group_for_job(job)['kind'], expected)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            scheduler.normalize_job({'task_groups': [{'resource_profile': 'invalid'}]})
        normalized = scheduler.normalize_job({'type': 'bug-fix', 'task_groups': [{'group_id': 'a'}]})
        self.assertEqual(normalized['task_groups'][0]['status'], 'pending')

    def test_dependencies_and_assignment(self):
        job = scheduler.normalize_job({'task_groups': [
            {'group_id': 'done', 'status': 'completed'}, {'group_id': 'a', 'depends_on': ['done']},
            {'group_id': 'b', 'depends_on': ['a']}, {'group_id': 'running', 'status': 'executing'}]})
        self.assertEqual([g['group_id'] for g in scheduler.ready_groups(job)], ['a'])
        job['task_groups'][1]['resource_profile'] = 'reasoning'
        assignment = scheduler.assign_group(job, 'a', self.machine, self.probe, 'fake')
        self.assertEqual(job['assigned_machine'], 'local')
        self.assertEqual(job['task_groups'][1]['status'], 'scheduled')
        self.assertEqual(job['dispatch_history'], [assignment])
        with self.assertRaises(KeyError):
            scheduler.assign_group(job, 'missing', self.machine, self.probe, 'fake')

    def test_model_preferences_fallback_and_restrictions(self):
        group = {'resource_profile': 'reasoning', 'preferred_models': ['missing', 'fake']}
        self.assertEqual(scheduler.choose_model(group, self.machine, {}), 'fake')
        group['preferred_models'] = []
        self.assertEqual(scheduler.choose_model(group, self.machine, {'planner': 'fake'}), 'fake')
        self.assertEqual(scheduler.choose_model(group, self.machine, {'planner': 'missing', 'builder': 'fake'}), 'fake')
        self.assertEqual(scheduler.choose_model(group, self.machine, {'allowed_models': ['missing', 'fake']}), 'fake')
        self.assertEqual(scheduler.choose_model(group, self.machine, {}), 'fake')
        self.assertFalse(scheduler.model_allowed('fake', ['missing']))
        for machine, allowed in (({'name': 'none', 'models': []}, None), (self.machine, ['missing'])):
            with self.assertRaisesRegex(RuntimeError, 'No compatible'):
                scheduler.choose_model(group, machine, {'allowed_models': allowed})

    def test_machine_scoring_rewards_capacity_and_penalizes_load(self):
        group = {'resource_profile': 'implementation', 'preferred_roles': ['worker']}
        self.assertEqual(scheduler.score_machine(self.machine, {}, group)[0], -9999)
        self.assertEqual(scheduler.score_machine(self.machine, {'reachable': True}, group)[0], -9999)
        self.assertEqual(scheduler.score_machine(self.machine, {**self.probe, 'disk_free_gb': 0}, group)[0], -9999)
        machine = {**self.machine, 'priority': 10, 'supports_xcode': True, 'roles': ['worker'],
                   'tags': ['tailscale', 'interactive'], 'max_concurrent_jobs': 1, 'interactive_reserved': True}
        probe = {**self.probe, 'disk_free_gb': 20, 'hw': {'logical_cores': 8, 'cpu_model': 'Apple M3 Pro'},
                 'mem_free_mb': 30000, 'active_ai_jobs': 2, 'active_xcodebuild_count': 1,
                 'active_simulator_count': 1, 'cpu_load_1m': 2}
        score, components = scheduler.score_machine(machine, probe, group, previously_assigned='local')
        self.assertEqual(score, -50)
        self.assertTrue(any('stickiness' in c for c in components))
        for cpu in ('M1', 'M2', 'Intel'):
            score, components = scheduler.score_machine(self.machine, {**self.probe, 'hw': {'cpu_model': cpu}}, {'resource_profile': 'review'})
            self.assertEqual(score, 20 if cpu != 'Intel' else 0)
        score, components = scheduler.score_machine(machine, self.probe, {'resource_profile': 'reasoning'})
        self.assertEqual(score, 25)
        score, components = scheduler.score_machine(self.machine, self.probe, group, previously_assigned='other')
        self.assertEqual(score, -50)

    def test_selection_rejects_missing_models_binaries_and_unreachable_hosts(self):
        group = {'resource_profile': 'implementation'}
        machines = [{**self.machine, 'name': name} for name in ('denied', 'model', 'binary', 'offline', 'slow', 'winner')]
        job = {'allowed_machines': ['model', 'binary', 'offline', 'slow', 'winner']}
        def choose(group, machine, job):
            if machine['name'] == 'model':
                raise RuntimeError('No model')
            return 'fake'
        def probe(machine):
            return {**self.probe, 'binaries': {'cli': machine['name'] != 'binary'}}
        def score(machine, probe, group, previously_assigned=None):
            return {'offline': (-9999, []), 'slow': (-600, ['busy']), 'winner': (10, ['ready'])}[machine['name']]
        with patch.object(scheduler, 'choose_model', side_effect=choose), \
             patch.object(scheduler, 'probe_machine', side_effect=probe), \
             patch('model_registry.get_model', return_value=SimpleNamespace(required_clis=['cli'])), \
             patch.object(scheduler, 'score_machine', side_effect=score):
            machine, result, model = scheduler.choose_machine_and_model(job, group, machines)
        self.assertEqual(machine['name'], 'winner')
        self.assertEqual(model, 'fake')
        self.assertIn('#2', self.output.getvalue())
        self.assertIn('Reason: busy', self.output.getvalue())
        with patch.object(scheduler, 'probe_machine', return_value=self.probe), \
             patch('model_registry.get_model', return_value=None):
            self.assertEqual(scheduler.choose_machine_and_model({}, group, [self.machine])[0], self.machine)
        for machines in ([], [{'name': 'bad', 'models': []}]):
            with patch.object(scheduler, 'probe_machine', return_value=self.probe), self.assertRaisesRegex(RuntimeError, 'No eligible'):
                scheduler.choose_machine_and_model({}, group, machines)
        self.assertEqual(scheduler.format_ineligible_machines([]), '')
        self.assertIn('bad', scheduler.format_ineligible_machines([({'name': 'bad'}, 'offline')]))

    def test_scheduling_filters_scoped_jobs_and_rejects_invalid_groups(self):
        job = scheduler.normalize_job({'type': 'test-audit'})
        remote = {**self.machine, 'execution_mode': 'ssh'}
        with patch.object(scheduler, 'choose_machine_and_model', return_value=(self.machine, self.probe, 'fake')) as choose:
            scheduler.schedule_ready_group(job, [remote, self.machine])
        self.assertEqual(choose.call_args.args[2], [self.machine])
        with self.assertRaisesRegex(RuntimeError, 'local worker'):
            scheduler.schedule_ready_group(job, [remote])
        for groups, message in (([], 'No ready'), ([{'group_id': 'a', 'status': 'pending'}, {'group_id': 'b', 'status': 'pending'}], 'single-group')):
            with self.assertRaisesRegex(RuntimeError, message):
                scheduler.schedule_ready_group({'task_groups': groups}, [])
        job = scheduler.normalize_job({'assigned_machine': 'previous'})
        with patch.object(scheduler, 'choose_machine_and_model', return_value=(self.machine, self.probe, 'fake')) as choose:
            scheduler.schedule_ready_group(job, [self.machine])
        self.assertEqual(choose.call_args.kwargs['previously_assigned'], 'previous')

    def test_local_git_state_and_remote_sync_paths(self):
        with patch.object(scheduler.subprocess, 'check_output', side_effect=[' main\n', ' abc\n', ' M file\n']):
            self.assertEqual(scheduler.get_local_git_state(), {'branch': 'main', 'hash': 'abc', 'dirty': True})
        with patch.object(scheduler.subprocess, 'check_output', side_effect=OSError()):
            self.assertEqual(scheduler.get_local_git_state()['branch'], 'unknown')
        machine = {'name': 'remote', 'repo_path': '/repo', 'ssh_target': ['host', 'fallback']}
        state = {'branch': 'main', 'hash': 'abc', 'dirty': False}
        with patch.object(scheduler.subprocess, 'run') as run:
            self.assertTrue(scheduler.sync_code_to_remote(machine, state, {'git_branch': 'main', 'git_head_hash': 'abc'}))
            run.assert_not_called()
            self.assertTrue(scheduler.sync_code_to_remote(machine, {**state, 'dirty': True}, {}))
            self.assertEqual(run.call_args.args[0][0], 'rsync')
        with patch.object(scheduler.subprocess, 'run', side_effect=[OSError('rsync failed'), None, None]) as run:
            self.assertTrue(scheduler.sync_code_to_remote(machine, {**state, 'dirty': True}, {}))
            self.assertEqual(run.call_count, 3)
        with patch.object(scheduler.subprocess, 'run', side_effect=OSError('git failed')):
            self.assertFalse(scheduler.sync_code_to_remote({**machine, 'ssh_target': 'host'}, state, {}))

    def test_remote_dispatch_and_failure_cleanup(self):
        machine = {'name': 'remote', 'repo_path': '/repo', 'ssh_target': ['host'], 'execution_mode': 'ssh'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'job.json'
            path.write_text(json.dumps({'type': 'bug-fix'}))
            scheduler.dispatch_job(path, {}, {}, True)
            with patch('worker_run.execute_job') as execute:
                scheduler.dispatch_job(path, {'execution_mode': 'local'}, {}, False, True)
                execute.assert_called_once_with(path, resume=True)
            for valid, synced in ((False, True), (True, False)):
                with patch.object(scheduler, 'verify_remote_worker_package', return_value=valid), \
                     patch.object(scheduler, 'sync_code_to_remote', return_value=synced), \
                     patch.object(scheduler, 'get_local_git_state', return_value={}), self.assertRaises(SystemExit):
                    scheduler.dispatch_job(path, machine, {}, False)
            for resume, failure in ((True, False), (False, True)):
                with patch.object(scheduler, 'verify_remote_worker_package', return_value=True), \
                     patch.object(scheduler, 'sync_code_to_remote', return_value=True), \
                     patch.object(scheduler, 'get_local_git_state', return_value={}), \
                     patch.object(scheduler, 'run', return_value=SimpleNamespace(returncode=int(failure))) as run:
                    scheduler.dispatch_job(path, machine if resume else {**machine, 'ssh_target': 'host'}, {}, False, resume=resume)
                self.assertEqual(run.call_count, 4)
                self.assertEqual(' --resume' in run.call_args_list[2].args[0][-1], resume)
                self.assertEqual(run.call_args_list[-1].args[0][0], 'scp')
            path.write_text(json.dumps({'type': 'coverage'}))
            with self.assertRaisesRegex(RuntimeError, 'local worker'):
                scheduler.dispatch_job(path, machine, {}, False)
        self.assertEqual(scheduler.remote_runtime_dir({'orchestrator_runtime_dir': 'custom'}), 'custom')
        for code in (0, 1):
            with patch.object(scheduler, 'run', return_value=SimpleNamespace(returncode=code)):
                self.assertEqual(scheduler.verify_remote_worker_package(machine, 'host'), code == 0)

    def test_main_persists_only_real_dispatches(self):
        job = scheduler.normalize_job({'job_id': 'job-1'})
        assignment = {'machine': 'local', 'model': 'fake'}
        for dry in (False, True):
            with patch.object(sys, 'argv', ['schedule', 'job.json'] + (['--dry-run', '--resume'] if dry else [])), \
                 patch.object(scheduler, 'load_job', return_value=job), \
                 patch.object(scheduler, 'load_machines', return_value=[self.machine]), \
                 patch.object(scheduler, 'schedule_ready_group', return_value=(job['task_groups'][0], self.machine, {}, 'fake', assignment)), \
                 patch.object(scheduler, 'write_json') as write, patch.object(scheduler, 'dispatch_job') as dispatch:
                scheduler.main()
            self.assertEqual(write.call_count, int(not dry))
            self.assertEqual(dispatch.call_args.kwargs['dry_run'], dry)

    def test_module_entrypoint_schedules_a_dry_run(self):
        import common
        def read(path):
            if path.name == 'machines.json':
                return {'machines': [self.machine]}
            return {'job_id': 'job-1', 'builder': 'fake'}
        with patch.object(sys, 'argv', ['schedule_job', 'job.json', '--dry-run']), \
             patch.object(common, 'read_json', side_effect=read), \
             patch('probe_machine.probe_machine', return_value=self.probe):
            runpy.run_module('schedule_job', run_name='__main__')
        self.assertIn('Would schedule job-1', self.output.getvalue())
