"""Regressions for a failed Xcode invocation masquerading as 95% coverage."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator' / 'scripts'))
import dev_console
import job_actions


class CoverageExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = io.StringIO()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(dev_console, 'ROOT', self.root))
        self.stack.enter_context(patch.object(dev_console, 'clear_screen'))
        self.stack.enter_context(patch.object(dev_console, 'get_online_machines', return_value=[]))
        self.pause = self.stack.enter_context(patch.object(dev_console, 'input'))
        self.stack.enter_context(redirect_stdout(self.output))

    def configure(self, xcode):
        config = SimpleNamespace(uses_xcode=xcode, scheme='Example', project_name='Example',
                                 test_target='ExampleTests', test_command='python3 -m unittest discover -s tests',
                                 xcode_workspace=None, xcode_project=None)
        self.stack.enter_context(patch.object(dev_console, 'PROJECT_CONFIG', config))

    def failed_xcode(self):
        self.configure(True)
        self.stack.enter_context(patch('shutil.which', return_value=None))
        self.stack.enter_context(patch.object(dev_console, 'get_best_simulator_destination', return_value='platform=iOS Simulator'))
        self.stack.enter_context(patch.object(dev_console, 'get_simulator_diagnostic', return_value={
            'has_simctl': True, 'has_runtimes': True, 'available_devices_count': 1, 'best_destination': 'simulator'}))
        self.stack.enter_context(patch.object(dev_console, 'discover_test_suites', return_value=[{'name': 'ExampleTests', 'test_count': 454}]))
        proc = MagicMock()
        proc.stdout = iter(['xcodebuild: error: directory does not contain a project\n'])
        proc.wait.return_value = proc.returncode = 1
        self.stack.enter_context(patch.object(dev_console.subprocess, 'Popen', return_value=proc))

    def test_old_estimate_is_not_exposed_as_measured_coverage(self):
        dev_console.save_coverage_data({'overall_coverage_pct': 95.0, 'estimated': True,
                                       'targets': [{'name': 'App', 'coverage_pct': 95.0}]})
        record = dev_console.get_coverage_data()
        self.assertNotIn('overall_coverage_pct', record)
        self.assertNotIn('targets', record)
        self.assertIn('last_error', record)

    def test_failed_xcode_does_not_invent_or_save_95_percent(self):
        self.failed_xcode()
        dev_console.save_coverage_data({'overall_coverage_pct': 77.9, 'tool': 'Xcode (xccov)'})
        result = dev_console.run_calculate_coverage([], [])
        self.assertIsNone(result)
        self.assertNotIn('CODE COVERAGE & TEST HEALTH REPORT', self.output.getvalue())
        record = dev_console.get_coverage_data()
        self.assertEqual(record['overall_coverage_pct'], 77.9)
        self.assertIn('last_error', record)

    def test_failed_measurement_clears_a_previous_estimate(self):
        self.failed_xcode()
        dev_console.save_coverage_data({'overall_coverage_pct': 95.0, 'estimated': True, 'total_tests': 454})
        dev_console.run_calculate_coverage([], [])
        self.assertNotIn('overall_coverage_pct', dev_console.get_coverage_data())

    def test_failed_xcode_cannot_accept_a_partial_result_bundle(self):
        self.failed_xcode()
        child = MagicMock(stdout=iter([]), returncode=1)
        child.wait.return_value = 1
        def launch(argv, *args, **kwargs):
            bundle = Path(argv[argv.index('-resultBundlePath') + 1])
            bundle.mkdir()
            return child
        self.stack.enter_context(patch.object(dev_console.subprocess, 'Popen', side_effect=launch))
        self.stack.enter_context(patch.object(dev_console.subprocess, 'check_output', return_value=b'{"lineCoverage": 0.95}'))
        self.assertIsNone(dev_console.run_calculate_coverage([], []))
        self.assertNotIn('overall_coverage_pct', dev_console.get_coverage_data())

    def test_missing_tool_records_failure_and_removes_estimated_percentage(self):
        self.configure(False)
        dev_console.save_coverage_data({'overall_coverage_pct': 95.0, 'estimated': True})
        (self.root / 'go.mod').write_text('module example')
        self.stack.enter_context(patch.object(dev_console.subprocess, 'Popen', side_effect=FileNotFoundError('go missing')))
        self.assertEqual(job_actions.coverage(), 1)
        record = dev_console.get_coverage_data()
        self.assertNotIn('overall_coverage_pct', record)
        self.assertIn('last_error', record)

    def test_web_python_coverage_uses_real_runner_and_exits_without_menu_pause(self):
        self.configure(False)
        self.stack.enter_context(patch.object(dev_console.sys.stdin, 'isatty', return_value=True))
        self.stack.enter_context(patch.object(dev_console, 'get_best_simulator_destination', return_value='unused'))
        (self.root / 'pyproject.toml').write_text('')
        (self.root / 'example.py').write_text('def value():\n    return 42\n')
        (self.root / 'untested.py').write_text('def untested():\n    return 0\n')
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / 'test_example.py').write_text(
            'import unittest\nfrom example import value\nclass TestExample(unittest.TestCase):\n'
            '    def test_value(self):\n        self.assertEqual(value(), 42)\n')
        # Prevent an actual Xcode launch on the broken implementation; the repaired
        # path runs real coverage.py against the tiny Python fixture.
        real_popen = dev_console.subprocess.Popen
        def launch(argv, *args, **kwargs):
            if argv[0] in ('xcodebuild', 'xcbeautify'):
                raise OSError('Xcode must not run for a Python project')
            return real_popen(argv, *args, **kwargs)
        self.stack.enter_context(patch.object(dev_console.subprocess, 'Popen', side_effect=launch))
        self.assertEqual(job_actions.coverage(), 0)
        record = dev_console.get_coverage_data()
        self.assertEqual(record.get('tool'), 'coverage.py')
        self.assertEqual(record['overall_coverage_pct'], 50.0)
        self.assertNotIn('estimated', record)
        self.pause.assert_not_called()
        # An explicit project scope takes precedence over our defaults.
        (self.root / '.coveragerc').write_text('[run]\nsource = example\nomit = tests/*\n')
        self.assertEqual(job_actions.coverage(), 0)
        self.assertEqual(dev_console.get_coverage_data()['overall_coverage_pct'], 100.0)
