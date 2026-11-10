from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import package_updates as updates


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.calls = []

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def npm(self, path='package.json', **extra):
        self.write(path, json.dumps({'dependencies': {'react': '^18', '@acme/ui': '^2'}, **extra}))

    def runner(self, argv, *, cwd, env):
        self.calls.append((argv, cwd, env))
        return 0


    def local_venv(self, directory=''):
        base = f'{directory}/' if directory else ''
        self.write(base + '.venv/pyvenv.cfg', 'include-system-site-packages = false\n')
        self.write(base + '.venv/bin/python', '')

    def test_bun_updates_selected_workspace_with_ancestor_lock(self):
        self.npm(packageManager='bun@1.3.0', workspaces=['apps/*'])
        self.npm('apps/ui/package.json')
        self.write('bun.lock', '{}')
        plan = updates.plan_update(self.root, 'npm', 'react', 'apps/ui/package.json')
        self.assertEqual(plan.argv, ('bun', 'update', '--depth', '0', 'react'))
        self.assertEqual(plan.cwd, self.root / 'apps/ui')

    def test_poetry_requires_and_uses_isolated_project_environment(self):
        from unittest.mock import patch
        self.write('pyproject.toml', '[tool.poetry.dependencies]\nrequests="^2"\n')
        self.write('poetry.lock', '')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'local .venv'):
            updates.plan_update(self.root, 'pip', 'requests', 'pyproject.toml')
        self.local_venv()
        with patch.dict('os.environ', {'VIRTUAL_ENV': '/external/env', 'CONDA_PREFIX': '/external/conda', 'POETRY_VIRTUALENVS_CREATE': 'false'}):
            result = updates.execute_update(self.root, 'pip', 'requests', 'pyproject.toml', 'true', run=self.runner)
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0][0], ['poetry', 'update', 'requests'])
        env = self.calls[0][2]
        self.assertEqual(env['VIRTUAL_ENV'], str(self.root / '.venv'))
        self.assertEqual(env['POETRY_VIRTUALENVS_CREATE'], 'true')
        self.assertEqual(env['POETRY_VIRTUALENVS_IN_PROJECT'], 'true')
        self.assertNotIn('CONDA_PREFIX', env)

    def test_pip_uses_local_interpreter_and_manifest_as_constraints(self):
        self.write('requirements.txt', 'requests>=2,<3\nurllib3<3\n')
        self.local_venv()
        result = updates.execute_update(self.root, 'pip', 'requests', 'requirements.txt', 'true', run=self.runner)
        self.assertTrue(result['ok'])
        argv = self.calls[0][0]
        self.assertEqual(argv, [str(self.root / '.venv/bin/python'), '-I', '-m', 'pip', '--isolated', '--require-virtualenv', 'install', '--upgrade', '--upgrade-strategy', 'only-if-needed', '--no-user', '--prefix', str(self.root / '.venv'), '--constraint', str(self.root / 'requirements.txt'), 'requests'])
        self.assertEqual(self.calls[0][2]['PIP_CONFIG_FILE'], '/dev/null')

    def test_pip_rejects_complex_constraints_and_shared_environments(self):
        self.local_venv()
        for extra in ['-r other.txt', 'urllib3[secure]<3', 'urllib3<3; python_version>"3"', 'urllib3 @ https://example.com/a.whl']:
            self.write('requirements.txt', 'requests>=2\n' + extra + '\n')
            with self.subTest(extra=extra), self.assertRaisesRegex(updates.PackageUpdateError, 'simple'):
                updates.plan_update(self.root, 'pip', 'requests', 'requirements.txt')
        self.write('requirements.txt', 'requests>=2\n')
        self.write('.venv/pyvenv.cfg', 'include-system-site-packages = true\n')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'system'):
            updates.plan_update(self.root, 'pip', 'requests', 'requirements.txt')

    def test_python_rejects_symlinked_project_environment(self):
        self.write('requirements.txt', 'requests>=2\n')
        with tempfile.TemporaryDirectory() as outside:
            (self.root / '.venv').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(updates.PackageUpdateError, 'symlink'):
                updates.plan_update(self.root, 'pip', 'requests', 'requirements.txt')

    def test_python_conflicting_lock_managers_are_rejected(self):
        self.write('pyproject.toml', '[project]\nname="app"\nversion="1"\ndependencies=["requests>=2"]\n')
        self.write('poetry.lock', '')
        self.write('uv.lock', '')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'multiple'):
            updates.plan_update(self.root, 'pip', 'requests', 'pyproject.toml')

    def test_python_rejects_environment_site_packages_escape(self):
        self.write('requirements.txt', 'requests>=2\n')
        self.local_venv()
        with tempfile.TemporaryDirectory() as outside:
            (self.root / '.venv/lib').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(updates.PackageUpdateError, 'symlink'):
                updates.plan_update(self.root, 'pip', 'requests', 'requirements.txt')

    def test_python_rejects_environment_parent_directory_escape(self):
        self.write('requirements.txt', 'requests>=2\n')
        self.local_venv()
        (self.root / '.venv/lib').mkdir()
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / 'site-packages').mkdir()
            (self.root / '.venv/lib/python3.13').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(updates.PackageUpdateError, 'symlink'):
                updates.plan_update(self.root, 'pip', 'requests', 'requirements.txt')

    def test_capability_names_unsupported_native_manager(self):
        self.npm(packageManager='yarn@4.5.0', workspaces=['apps/*'])
        self.write('yarn.lock', '__metadata: {}')
        capability = updates.update_capability(self.root, 'npm', 'react', 'package.json')
        self.assertFalse(capability['supported'])
        self.assertEqual(capability['manager'], 'yarn')
        self.write('pyproject.toml', '[tool.poetry.dependencies]\nrequests="^2"\n')
        self.write('poetry.lock', '')
        capability = updates.update_capability(self.root, 'pip', 'requests', 'pyproject.toml')
        self.assertFalse(capability['supported'])
        self.assertEqual(capability['manager'], 'poetry')

    def test_capability_reuses_previously_detected_entries_without_rescanning(self):
        from unittest.mock import patch
        self.npm()
        detected = [{'ecosystem': 'npm', 'name': 'react', 'manifest': 'package.json'}]
        with patch('orchestrator.package_updates.packages.detect', side_effect=AssertionError('unexpected rescan')):
            capability = updates.update_capability(self.root, 'npm', 'react', 'package.json', detected=detected)
            missing = updates.update_capability(self.root, 'npm', 'absent', 'package.json', detected=detected)
        self.assertTrue(capability['supported'])
        self.assertFalse(missing['supported'])
        self.assertIn('not declared', missing['reason'])

    def test_capability_reports_validated_support_and_boundaries(self):
        self.npm(packageManager='bun@1.3.0')
        capability = updates.update_capability(self.root, 'npm', 'react', 'package.json')
        self.assertTrue(capability['supported'])
        self.assertEqual(capability['manager'], 'bun')
        self.write('build.gradle', 'implementation "org.example:lib:1.0"\n')
        capability = updates.update_capability(self.root, 'gradle', 'org.example:lib', 'build.gradle')
        self.assertFalse(capability['supported'])
        self.assertEqual(capability['manager'], 'gradle')
        self.assertIn('project-specific', capability['reason'])
        missing = updates.update_capability(self.root, 'npm', 'missing', 'package.json')
        self.assertFalse(missing['supported'])
        self.assertIn('not declared', missing['reason'])

    def test_modern_yarn_workspace_update_cannot_change_other_manifests(self):
        self.npm(packageManager='yarn@4.5.0', workspaces=['apps/*'])
        self.npm('apps/a/package.json')
        self.write('apps/b/package.json', json.dumps({'dependencies': {'react': '^19'}}))
        self.write('yarn.lock', '__metadata: {}')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'workspace'):
            updates.plan_update(self.root, 'npm', 'react', 'apps/a/package.json')

    def test_validated_npm_update_runs_before_configured_tests(self):
        self.npm('web/package.json')
        self.write('web/package-lock.json', '{}')
        result = updates.execute_update(self.root, 'npm', '@acme/ui', 'web/package.json', 'python3 -m unittest', run=self.runner)
        self.assertTrue(result['ok'])
        self.assertEqual([c[0] for c in self.calls], [['npm', 'update', '--global=false', '--save', '@acme/ui'], ['bash', '-lc', 'python3 -m unittest']])
        self.assertEqual([c[1] for c in self.calls], [self.root / 'web', self.root])

    def test_lockfile_and_package_manager_choose_matching_command(self):
        for lock, metadata, expected in [
            ('pnpm-lock.yaml', '', ['pnpm', 'update', 'react']),
            ('yarn.lock', 'yarn@1.22.22', ['yarn', 'upgrade', 'react']),
            ('yarn.lock', 'yarn@4.5.0', ['yarn', 'up', '-R', 'react']),
            ('yarn.lock', '', ['yarn', 'upgrade', 'react']),
        ]:
            with self.subTest(lock=lock, metadata=metadata):
                self.npm(packageManager=metadata)
                self.write(lock, '# yarn lockfile v1\n' if not metadata else '')
                self.assertEqual(list(updates.plan_update(self.root, 'npm', 'react', 'package.json').argv), expected)
                (self.root / lock).unlink()

    def test_workspace_uses_ancestor_lockfile_without_changing_cwd(self):
        self.npm('apps/ui/package.json')
        self.write('pnpm-lock.yaml', '')
        plan = updates.plan_update(self.root, 'npm', 'react', 'apps/ui/package.json')
        self.assertEqual(list(plan.argv), ['pnpm', 'update', 'react'])
        self.assertEqual(plan.cwd, self.root / 'apps/ui')

    def test_ambiguous_and_conflicting_managers_are_rejected(self):
        self.npm(packageManager='yarn@4.0.0')
        self.write('package-lock.json', '{}')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'conflict'):
            updates.plan_update(self.root, 'npm', 'react', 'package.json')
        self.npm()
        self.write('pnpm-lock.yaml', '')
        with self.assertRaisesRegex(updates.PackageUpdateError, 'multiple'):
            updates.plan_update(self.root, 'npm', 'react', 'package.json')

    def test_missing_dependency_options_wildcards_and_injection_never_run(self):
        self.npm()
        for name in ['missing', '--global', 'react;touch /tmp/bad', 'react@latest', '*', '@acme/*', 'react\n']:
            with self.subTest(name=name), self.assertRaises(updates.PackageUpdateError):
                updates.execute_update(self.root, 'npm', name, 'package.json', 'true', run=self.runner)
        self.assertEqual(self.calls, [])

    def test_untrusted_manifest_paths_and_symlink_escape_are_rejected(self):
        self.npm()
        for path in ['../package.json', str(self.root / 'package.json'), '.orchestrator/project.json']:
            with self.subTest(path=path), self.assertRaises(updates.PackageUpdateError):
                updates.plan_update(self.root, 'npm', 'react', path)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'package.json'
            target.write_text('{"dependencies":{"react":"18"}}')
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(updates.PackageUpdateError):
                updates.plan_update(self.root, 'npm', 'react', 'linked/package.json')

    def test_update_failure_stops_tests_and_preserves_exact_exit(self):
        self.npm()
        def fail(argv, **kwargs):
            self.calls.append(argv)
            return 17
        result = updates.execute_update(self.root, 'npm', 'react', 'package.json', 'true', run=fail)
        self.assertFalse(result['ok'])
        self.assertEqual((result['stage'], result['returncode']), ('update', 17))
        self.assertIn('17', result['message'])
        self.assertEqual(len(self.calls), 1)

    def test_test_failure_identifies_configured_command_and_exit(self):
        self.npm()
        def fail_tests(argv, **kwargs):
            return 9 if argv[0] == 'bash' else 0
        result = updates.execute_update(self.root, 'npm', 'react', 'package.json', 'pytest -x', run=fail_tests)
        self.assertFalse(result['ok'])
        self.assertEqual((result['stage'], result['returncode']), ('tests', 9))
        self.assertIn('pytest -x', result['message'])

    def test_missing_tests_prevents_any_mutation(self):
        self.npm()
        with self.assertRaisesRegex(updates.PackageUpdateError, 'test command'):
            updates.execute_update(self.root, 'npm', 'react', 'package.json', '', run=self.runner)
        self.assertEqual(self.calls, [])

    def test_missing_executable_is_plain_failure(self):
        self.npm()
        def missing(*args, **kwargs):
            raise FileNotFoundError('npm is missing')
        result = updates.execute_update(self.root, 'npm', 'react', 'package.json', 'true', run=missing)
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'update')
        self.assertIn('npm is missing', result['message'])

    def test_swift_cargo_alias_and_go_use_single_dependency_native_commands(self):
        fixtures = [
            ('Package.swift', 'let p = Package(dependencies: [.package(url: "https://github.com/Quick/Nimble.git", from: "13.0.0")])', 'swiftpm', 'Nimble', ['swift', 'package', 'update', 'nimble']),
            ('Cargo.toml', '[dependencies]\nserde_alias = { package = "serde", version = "1" }\n', 'cargo', 'serde_alias', ['cargo', 'update', '--package', 'serde']),
            ('go.mod', 'module example.com/app\nrequire example.com/lib v1.0.0\n', 'go', 'example.com/lib', ['go', 'get', 'example.com/lib@upgrade']),
        ]
        for manifest, content, ecosystem, name, expected in fixtures:
            with self.subTest(ecosystem=ecosystem):
                self.write(manifest, content)
                self.assertEqual(list(updates.plan_update(self.root, ecosystem, name, manifest).argv), expected)

    def test_uv_updates_project_lock_and_local_environment_only(self):
        self.write('pyproject.toml', '[project]\nname="app"\nversion="1"\ndependencies=["requests>=2"]\n')
        self.write('uv.lock', '')
        result = updates.execute_update(self.root, 'pip', 'requests', 'pyproject.toml', 'true', run=self.runner)
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0][0], ['uv', 'sync', '--upgrade-package', 'requests'])
        self.assertEqual(self.calls[0][2]['UV_PROJECT_ENVIRONMENT'], str(self.root / '.venv'))

    def test_plain_pip_and_unsupported_ecosystems_explain_boundary(self):
        self.write('requirements.txt', 'requests>=2\n')
        self.write('build.gradle', 'implementation "org.example:lib:1.0"\n')
        for ecosystem, name, manifest, message in [('pip', 'requests', 'requirements.txt', 'local .venv'), ('gradle', 'org.example:lib', 'build.gradle', 'Gradle')]:
            with self.subTest(ecosystem=ecosystem), self.assertRaisesRegex(updates.PackageUpdateError, message):
                updates.plan_update(self.root, ecosystem, name, manifest)

    def test_cocoapods_uses_project_bundle_when_present(self):
        self.write('ios/Podfile', "pod 'Alamofire', '~> 5.0'\n")
        plain = updates.plan_update(self.root, 'cocoapods', 'Alamofire', 'ios/Podfile')
        self.assertEqual(list(plain.argv), ['pod', 'update', 'Alamofire'])
        self.write('Gemfile.lock', '')
        bundled = updates.plan_update(self.root, 'cocoapods', 'Alamofire', 'ios/Podfile')
        self.assertEqual(list(bundled.argv), ['bundle', 'exec', 'pod', 'update', 'Alamofire'])
        self.assertEqual(bundled.cwd, self.root / 'ios')

    def test_bundler_targets_only_chosen_gem_conservatively(self):
        self.write('Gemfile', "gem 'rails', '~> 7'\n")
        plan = updates.plan_update(self.root, 'ruby', 'rails', 'Gemfile')
        self.assertEqual(list(plan.argv), ['bundle', 'update', '--conservative', 'rails'])

    def test_dart_and_flutter_choose_sdk_from_manifest(self):
        self.write('pubspec.yaml', 'name: app\ndependencies:\n  http: ^1.0.0\n')
        plan = updates.plan_update(self.root, 'dart', 'http', 'pubspec.yaml')
        self.assertEqual(list(plan.argv), ['dart', 'pub', 'upgrade', 'http'])
        self.write('pubspec.yaml', 'name: app\ndependencies:\n  flutter:\n    sdk: flutter\n  http: ^1.0.0\n')
        plan = updates.plan_update(self.root, 'dart', 'http', 'pubspec.yaml')
        self.assertEqual(list(plan.argv), ['flutter', 'pub', 'upgrade', 'http'])

    def test_installed_names_still_cannot_be_selectors_or_options(self):
        self.npm(dependencies={'--global': '*', '@acme/*': '*', 'react@latest': '*'})
        for name in ('--global', '@acme/*', 'react@latest'):
            with self.subTest(name=name), self.assertRaisesRegex(updates.PackageUpdateError, 'characters'):
                updates.plan_update(self.root, 'npm', name, 'package.json')

    def test_npm_forces_local_mode_despite_global_user_configuration(self):
        self.npm()
        plan = updates.plan_update(self.root, 'npm', 'react', 'package.json')
        self.assertIn('--global=false', plan.argv)

    def test_missing_tests_message_explicitly_says_tests_were_not_run(self):
        self.npm()
        with self.assertRaisesRegex(updates.PackageUpdateError, 'not run'):
            updates.execute_update(self.root, 'npm', 'react', 'package.json', None, run=self.runner)
        self.assertEqual(self.calls, [])

    def test_cli_runs_configured_tests_and_reports_test_failure(self):
        import contextlib
        import io
        from unittest.mock import patch
        from orchestrator.scripts.update_package import main
        self.npm()
        self.write('.orchestrator/project.json', '{"project_name":"app","test_command":"python3 -m unittest"}')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('orchestrator.package_updates.subprocess.call', side_effect=[0, 7]) as runner:
            status = main(['--root', str(self.root), '--ecosystem', 'npm', '--name', 'react', '--manifest', 'package.json'])
        self.assertEqual(status, 1)
        result = json.loads(next(line.split('=', 1)[1] for line in output.getvalue().splitlines() if line.startswith('ORCHESTRATOR_PACKAGE_UPDATE_RESULT=')))
        self.assertEqual((result['stage'], result['returncode']), ('tests', 7))
        self.assertEqual(runner.call_args_list[1].args[0], ['bash', '-lc', 'python3 -m unittest'])

    def test_canonical_test_document_is_used_when_config_has_no_override(self):
        from orchestrator.scripts.update_package import _test_command
        self.write('.orchestrator/project.json', '{"project_name":"app"}')
        self.write('docs/build-test-commands.md', '## Package validation\n\n```bash\npython3 -m compileall .\n```\n\n## Test suite\n\n```bash\npython3 -m unittest discover -s tests\n```\n')
        self.assertEqual(_test_command(self.root), 'python3 -m unittest discover -s tests')

    def test_cli_validation_emits_result_marker_without_external_process(self):
        from orchestrator.scripts.update_package import main
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(['--root', str(self.root), '--ecosystem', 'npm', '--name=--global', '--manifest', 'package.json'])
        self.assertEqual(status, 1)
        marker = next(line for line in output.getvalue().splitlines() if line.startswith('ORCHESTRATOR_PACKAGE_UPDATE_RESULT='))
        result = json.loads(marker.split('=', 1)[1])
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'validation')
