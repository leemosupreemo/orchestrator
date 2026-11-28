"""Opt-in real manager checks: ORCHESTRATOR_E2E_NATIVE_PACKAGES=1 python3 -m
unittest tests.e2e.test_native_package_updates -v.

Downloads Bun's official binary and installs Poetry into temporary tool directories.
Only throwaway project environments are changed. Network access is required.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shlex
import subprocess
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

from orchestrator.package_updates import execute_update


@unittest.skipUnless(os.environ.get('ORCHESTRATOR_E2E_NATIVE_PACKAGES') == '1', 'opt-in native manager downloads')
class NativePackageUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='orchestrator-native-update-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, POETRY_CACHE_DIR=str(self.base / 'poetry-cache'), BUN_INSTALL_CACHE_DIR=str(self.base / 'bun-cache'))
        for key in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
            self.env.pop(key, None)
        self.host = self.host_packages()
        self.external = self.base / 'external'
        venv.create(self.external)
        self.external_before = self.external_snapshot()

    def host_packages(self):
        return sorted((d.metadata['Name'], d.version) for d in importlib.metadata.distributions())

    def external_snapshot(self):
        return sorted((str(p.relative_to(self.external)), p.read_bytes())
                      for p in self.external.rglob('*') if p.is_file() and not p.is_symlink())

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def run_native(self, argv, *, cwd=None, env=None):
        result = subprocess.run(argv, cwd=cwd or self.root, env=env or self.env,
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def update(self, ecosystem, name, manifest, check):
        # Deliberately supply an unrelated active environment. Python updates must
        # override it; Bun must not modify it either.
        with patch.dict(os.environ, {**self.env, 'VIRTUAL_ENV': str(self.external)}, clear=True):
            def run(argv, *, cwd, env):
                completed = subprocess.run(argv, cwd=cwd, env=env, text=True,
                                           capture_output=True, timeout=240)
                print(completed.stdout)
                print(completed.stderr)
                return completed.returncode
            result = execute_update(self.root, ecosystem, name, manifest, check, run=run)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.external_snapshot(), self.external_before)
        self.assertEqual(self.host_packages(), self.host)

    def test_bun_updates_only_chosen_workspace_dependency(self):
        system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
        arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x64'}.get(platform.machine())
        if not system or not arch:
            self.skipTest('Bun binary provisioning supports macOS/Linux arm64/x64')
        archive = self.base / 'bun.zip'
        self.run_native(['curl', '--disable', '--fail', '--proto', '=https', '--proto-redir', '=https', '--location', '--max-time', '60', '--output', str(archive),
                         f'https://github.com/oven-sh/bun/releases/latest/download/bun-{system}-{arch}.zip'])
        tool = self.base / 'tools'
        tool.mkdir()
        bun = tool / 'bun'
        with zipfile.ZipFile(archive) as package:
            member = next(name for name in package.namelist() if name.endswith('/bun'))
            bun.write_bytes(package.read(member))
        bun.chmod(0o755)
        self.env['PATH'] = str(tool) + os.pathsep + self.env.get('PATH', '')
        version = self.run_native([str(bun), '--version'])
        print('Bun version:', version)
        self.write('package.json', json.dumps({'private': True, 'workspaces': ['apps/*'], 'packageManager': 'bun@' + version}))
        for workspace, selected in [('a', '3.0.0'), ('b', '2.0.0')]:
            self.write(f'apps/{workspace}/package.json', json.dumps({'name': workspace, 'version': '1.0.0',
                       'dependencies': {'is-odd': selected, 'lodash': '4.17.20'}}))
        self.run_native([str(bun), 'install'])
        for workspace, selected in [('a', '^3.0.0'), ('b', '^2.0.0')]:
            self.write(f'apps/{workspace}/package.json', json.dumps({'name': workspace, 'version': '1.0.0',
                       'dependencies': {'is-odd': selected, 'lodash': '^4.17.20'}}))
        sibling = (self.root / 'apps/b/package.json').read_bytes()
        root_manifest = (self.root / 'package.json').read_bytes()
        self.write('check.cjs', """const {createRequire} = require('module');
const assert = require('assert');
const a = createRequire(process.cwd() + '/apps/a/package.json');
const b = createRequire(process.cwd() + '/apps/b/package.json');
assert.equal(a('is-odd/package.json').version, '3.0.1');
assert.equal(b('is-odd/package.json').version, '2.0.0');
assert.equal(a('lodash/package.json').version, '4.17.20');
assert.equal(b('lodash/package.json').version, '4.17.20');
console.log('Selected workspace updated within range; sibling and unselected versions unchanged');
""")
        self.update('npm', 'is-odd', 'apps/a/package.json', shlex.join([str(bun), 'check.cjs']))
        self.assertEqual((self.root / 'apps/b/package.json').read_bytes(), sibling)
        self.assertEqual((self.root / 'package.json').read_bytes(), root_manifest)
        selected = json.loads((self.root / 'apps/a/package.json').read_text())
        self.assertEqual(selected['dependencies']['lodash'], '^4.17.20')

    def test_poetry_updates_selected_package_inside_local_environment(self):
        tools = self.base / 'poetry-tool'
        venv.create(tools, with_pip=True)
        self.run_native([str(tools / 'bin/python'), '-I', '-m', 'pip', '--isolated', '--cache-dir', str(self.base / 'pip-cache'), '--timeout', '15', '--retries', '1', 'install', 'poetry'])
        poetry = tools / 'bin/poetry'
        print('Poetry version:', self.run_native([str(poetry), '--version']))
        venv.create(self.root / '.venv', with_pip=True)
        self.env.update(VIRTUAL_ENV=str(self.root / '.venv'), POETRY_VIRTUALENVS_CREATE='true', POETRY_VIRTUALENVS_IN_PROJECT='true')
        def manifest(idna, certifi):
            return ('[project]\nname="native-update-smoke"\nversion="0.1.0"\nrequires-python=">=3.10"\n'
                    f'dependencies=["idna{idna}","certifi{certifi}"]\n'
                    '[tool.poetry]\npackage-mode=false\n')
        self.write('pyproject.toml', manifest('==3.10', '==2024.8.30'))
        self.run_native([str(poetry), 'install', '--no-root'])
        self.write('pyproject.toml', manifest('>=3.10,<4', '>=2024.8.30,<2025'))
        before = (self.root / 'pyproject.toml').read_bytes()
        self.env['PATH'] = str(tools / 'bin') + os.pathsep + self.env.get('PATH', '')
        python = str(self.root / '.venv/bin/python')
        check = ('import idna,certifi,importlib.metadata,sys; '
                 'assert idna.__version__.startswith("3.") and idna.__version__ != "3.10"; '
                 'assert importlib.metadata.version("certifi")=="2024.8.30"; '
                 'assert idna.__file__.startswith(sys.prefix+"/"); '
                 'print("Selected local package updated within range:",idna.__version__)')
        self.update('pip', 'idna', 'pyproject.toml', shlex.join([python, '-I', '-c', check]))
        self.assertEqual((self.root / 'pyproject.toml').read_bytes(), before)
