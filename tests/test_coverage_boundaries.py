"""Coverage runner contracts at tool, report, and configuration boundaries."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import coverage as cov
from tests.test_coverage import FakeRun


class CoverageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('COVERAGE_RCFILE', None)

    def python_project(self):
        (self.root / 'pyproject.toml').write_text('')
        return self.root

    def test_failed_tests_never_become_a_measurement_for_any_supported_runner(self):
        cases = [
            ('pyproject.toml', 'python3 -m unittest', f'{sys.executable} -m coverage run'),
            ('go.mod', 'go test ./...', 'go test'),
            ('Cargo.toml', 'cargo test', 'cargo llvm-cov --json'),
            ('Package.swift', 'swift test', 'swift test --enable-code-coverage'),
            ('package.json', 'npm test', 'npx jest'),
        ]
        for filename, command, prefix in cases:
            with self.subTest(runner=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / filename).write_text(json.dumps({'devDependencies': {'jest': '29'}}) if filename == 'package.json' else '')
                with self.assertRaisesRegex(cov.CoverageUnavailable, 'tests failed'):
                    cov.measure(root, command, FakeRun({prefix: (1, 'test failed', None)}))

    def test_unknown_project_and_unsupported_python_commands_explain_failure(self):
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'language'):
            cov.measure(self.root, None, FakeRun({}))
        self.python_project()
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'test command'):
            cov.measure(self.root, './custom-tests.sh', FakeRun({}))

    def test_missing_python_and_rust_tools_fail_with_installation_guidance(self):
        def missing(argv, cwd):
            raise FileNotFoundError(argv[0])
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'Coverage tool or report failed'):
            cov.measure(self.python_project(), 'pytest', missing)
        (self.root / 'Cargo.toml').write_text('')
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'cargo install cargo-llvm-cov'):
            cov.measure(self.root, 'cargo test', missing)

    def test_go_without_a_total_is_not_measured_as_zero(self):
        (self.root / 'go.mod').write_text('')
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'coverage total'):
            cov.measure(self.root, None, FakeRun({'go tool cover': (0, 'no total', None)}))

    def test_vitest_success_reads_coverage_summary_and_line_counts(self):
        (self.root / 'package.json').write_text(json.dumps({'devDependencies': {
            'vitest': '1', '@vitest/coverage-v8': '1'}}))
        def report(argv):
            destination = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--coverage.reportsDirectory='))
            (Path(destination) / 'coverage-summary.json').write_text(json.dumps({
                'total': {'lines': {'pct': 75, 'total': 100, 'covered': 75}}}))
        result = cov.measure(self.root, 'npm test', FakeRun({'npx vitest': (0, '', report)}))
        self.assertEqual((result.tool, result.pct, result.total_lines, result.covered_lines), ('Vitest', 75.0, 100, 75))

    def test_malformed_and_non_object_node_manifests_have_no_invented_measurement(self):
        for contents in ('{broken', '[]'):
            with self.subTest(contents=contents):
                (self.root / 'package.json').write_text(contents)
                with self.assertRaisesRegex(cov.CoverageUnavailable, 'Jest and Vitest'):
                    cov.measure(self.root, None, FakeRun({}))
        (self.root / 'package.json').unlink()
        with self.assertRaisesRegex(cov.CoverageUnavailable, 'Jest and Vitest'):
            cov.javascript(self.root, FakeRun({}), self.root)

    def test_python_defaults_and_statement_count_variant(self):
        self.python_project()
        def report(argv):
            Path(argv[-1]).write_text(json.dumps({'totals': {
                'percent_covered': 80, 'num_statements': 10, 'covered_statements': 8}}))
        runner = FakeRun({f'{sys.executable} -m coverage json': (0, '', report)})
        result = cov.measure(self.root, None, runner)
        self.assertEqual((result.pct, result.total_lines, result.covered_lines), (80.0, 10, 8))
        self.assertIn('-m unittest discover', ' '.join(runner.calls[1]))

    def test_explicit_toml_and_ini_scope_are_respected(self):
        cases = [
            ('pyproject.toml', '[tool.coverage.run]\nsource=["app"]\nomit=["app/generated/*"]\n'),
            ('setup.cfg', '[unrelated]\nkey=value\n'),
            ('tox.ini', '[coverage:run]\ninclude=app/*\nomit=app/generated/*\n'),
        ]
        # Skip irrelevant configuration and discover the later coverage section.
        for name, contents in cases:
            (self.root / name).write_text(contents)
        self.assertEqual(cov.python_scope_options(self.root), [])
        (self.root / 'tox.ini').unlink()
        self.assertEqual(cov.python_scope_options(self.root), [])
        external = self.root / 'custom.rc'
        external.write_text('[run]\nsource_pkgs=app\nomit=app/generated/*\n')
        with patch.dict(os.environ, {'COVERAGE_RCFILE': str(external)}):
            self.assertEqual(cov.python_scope_options(self.root), [])

    def test_source_count_ignores_non_source_hidden_and_unreadable_files(self):
        (self.root / 'notes.txt').write_text('not source\n')
        (self.root / '.hidden').mkdir()
        (self.root / '.hidden' / 'hidden.py').write_text('hidden\n')
        (self.root / 'visible.py').write_text('one\ntwo\n')
        (self.root / 'denied.py').write_text('denied\n')
        original_open = open
        def guarded(path, *args, **kwargs):
            if Path(path).name == 'denied.py':
                raise PermissionError('denied')
            return original_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=guarded):
            self.assertEqual(cov.count_source_lines(self.root), 2)
        self.assertEqual(cov.count_source_lines(self.root / 'missing'), 0)
        with patch('os.walk', side_effect=OSError('unavailable directory')):
            self.assertEqual(cov.count_source_lines(self.root), 0)

    def test_runner_preserves_failure_exit_even_without_output(self):
        child = SimpleNamespace(stdout=None, returncode=7, wait=lambda: 7)
        with patch.object(cov.subprocess, 'Popen', return_value=child):
            result = cov.stream(['missing-output'], self.root)
        self.assertEqual((result.returncode, result.stdout), (7, ''))

    def test_missing_manifest_diagnostics_and_sub_manifest_discovery(self):
        # 1. Sub-manifest discovery and ignore filters
        (self.root / "sub").mkdir()
        (self.root / "sub" / "Package.swift").write_text("")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "Package.swift").write_text("")
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "Package.swift").write_text("")
        self.assertEqual(cov._find_sub_manifest(self.root, "Package.swift"), self.root / "sub" / "Package.swift")
        self.assertIsNone(cov._find_sub_manifest(self.root, "nonexistent.file"))
        with patch("os.walk", side_effect=OSError("denied")):
            self.assertIsNone(cov._find_sub_manifest(self.root, "Package.swift"))

        # 2. Swift diagnostics: submanifest, no source, has source
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Found Swift language manifest at 'sub/Package.swift'"):
            cov.measure(self.root, "swift test", FakeRun({}))
        (self.root / "sub" / "Package.swift").unlink()
        (self.root / "sub").rmdir()

        with self.assertRaisesRegex(cov.CoverageUnavailable, r"This project language is configured for Swift \('swift test'\), but has no source files"):
            cov.measure(self.root, "swift test", FakeRun({}))

        (self.root / "app.swift").write_text("let a = 1\n")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "This project language is configured for Swift, but no Package.swift"):
            cov.measure(self.root, "", FakeRun({}))
        (self.root / "app.swift").unlink()

        # 3. Rust diagnostics: submanifest, no source, has source
        (self.root / "sub").mkdir()
        (self.root / "sub" / "Cargo.toml").write_text("")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Found Rust language manifest at 'sub/Cargo.toml'"):
            cov.measure(self.root, "cargo test", FakeRun({}))
        (self.root / "sub" / "Cargo.toml").unlink()
        (self.root / "sub").rmdir()

        with self.assertRaisesRegex(cov.CoverageUnavailable, r"This project language is configured for Rust \('cargo test'\), but has no source files"):
            cov.measure(self.root, "cargo test", FakeRun({}))

        (self.root / "app.rs").write_text("fn main() {}\n")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "This project language is configured for Rust, but no Cargo.toml"):
            cov.measure(self.root, "", FakeRun({}))
        (self.root / "app.rs").unlink()

        # 4. Go diagnostics: submanifest, no source, has source
        (self.root / "sub").mkdir()
        (self.root / "sub" / "go.mod").write_text("")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Found Go language manifest at 'sub/go.mod'"):
            cov.measure(self.root, "go test", FakeRun({}))
        (self.root / "sub" / "go.mod").unlink()
        (self.root / "sub").rmdir()

        with self.assertRaisesRegex(cov.CoverageUnavailable, r"This project language is configured for Go \('go test'\), but has no source files"):
            cov.measure(self.root, "go test", FakeRun({}))

        (self.root / "app.go").write_text("package main\n")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "This project language is configured for Go, but no go.mod"):
            cov.measure(self.root, "", FakeRun({}))
        (self.root / "app.go").unlink()

        # 5. JS/TS diagnostics: submanifest, no source, has source
        (self.root / "sub").mkdir()
        (self.root / "sub" / "package.json").write_text("{}")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Found JavaScript/TypeScript language manifest at 'sub/package.json'"):
            cov.measure(self.root, "npm test", FakeRun({}))
        (self.root / "sub" / "package.json").unlink()
        (self.root / "sub").rmdir()

        with self.assertRaisesRegex(cov.CoverageUnavailable, r"This project language is configured for JavaScript/TypeScript \('npm test'\), but has no source files"):
            cov.measure(self.root, "npm test", FakeRun({}))

        (self.root / "app.js").write_text("console.log(1);\n")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "This project language is configured for JavaScript/TypeScript, but no package.json"):
            cov.measure(self.root, "jest", FakeRun({}))
        (self.root / "app.js").unlink()

        # 6. Fallback with source code from an unconfigured language
        (self.root / "script.sh").write_text("echo hi\n")
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Couldn't tell which language this project uses"):
            cov.measure(self.root, None, FakeRun({}))
        (self.root / "script.sh").unlink()

