"""Validated, single-dependency native updates. No model or global installers.

Updates stay within the manager's declared version constraints. Dependency
resolvers may also adjust transitive lock entries needed by the chosen package.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator import packages


class PackageUpdateError(ValueError):
    """The requested dependency cannot safely be updated directly."""

    def __init__(self, message: str, *, manager: str | None = None):
        super().__init__(message)
        self.manager = manager


@dataclass(frozen=True)
class UpdatePlan:
    argv: tuple[str, ...]
    cwd: Path
    ecosystem: str
    name: str
    manifest: str


# No options, version selectors, manager glob patterns or shell metacharacters.
PLAIN_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
NPM_NAME = re.compile(r'(?:@[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
GO_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~/-]{0,299}\Z')
UNSUPPORTED = {
    'pip': 'Python updates require a uv or Poetry pyproject.toml with its lockfile, or simple pip requirements and an existing isolated local .venv. No global Python environment was changed.',
    'gradle': 'Gradle direct updates need a project-specific version catalog or dependency configuration and are not supported yet.',
    'unity': 'Unity direct updates require the Unity Package Manager and are not supported yet.',
}


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PackageUpdateError(f'Cannot read package manager metadata: {path.name}: {exc}') from exc
    if not isinstance(value, dict):
        raise PackageUpdateError(f'Invalid package manager metadata: {path.name}.')
    return value


def _ancestors(directory: Path, root: Path):
    while True:
        yield directory
        if directory == root:
            return
        directory = directory.parent


def _node_command(directory: Path, root: Path, name: str) -> tuple[str, ...]:
    lock_manager = None
    manager_field = ''
    manager_dir = directory
    for ancestor in _ancestors(directory, root):
        metadata = _object(ancestor / 'package.json') if (ancestor / 'package.json').is_file() else {}
        manager_field = manager_field or str(metadata.get('packageManager') or '')
        locks = {manager for filename, manager in (
            ('package-lock.json', 'npm'), ('npm-shrinkwrap.json', 'npm'),
            ('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'),
            ('bun.lock', 'bun'), ('bun.lockb', 'bun'),
        ) if (ancestor / filename).is_file()}
        if len(locks) > 1:
            raise PackageUpdateError('Found multiple package manager lockfiles. Keep the lockfile for the project’s chosen manager before updating.')
        if locks:
            lock_manager = locks.pop()
            manager_dir = ancestor
            break
    declared = manager_field.split('@', 1)[0] if manager_field else ''
    if lock_manager and declared and lock_manager != declared:
        raise PackageUpdateError('The packageManager field and lockfile conflict. Resolve the manager choice before updating.')
    manager = lock_manager or declared or 'npm'
    if manager == 'npm':
        return ('npm', 'update', '--global=false', '--save', name)
    if manager == 'bun':
        return ('bun', 'update', '--depth', '0', name)
    if manager == 'pnpm':
        return ('pnpm', 'update', name)
    if manager == 'yarn':
        version = re.match(r'yarn@(\d+)(?:\.|$)', manager_field)
        modern = (version and int(version.group(1)) >= 2) or (manager_dir / '.yarnrc.yml').is_file()
        if not version and (manager_dir / 'yarn.lock').is_file():
            modern = modern or '__metadata:' in (manager_dir / 'yarn.lock').read_text(errors='replace')
        if modern and (directory != manager_dir or _object(manager_dir / 'package.json').get('workspaces')):
            raise PackageUpdateError('Modern Yarn workspace refreshes affect matching ranges across every workspace; a selected-workspace native update is not supported yet.', manager='yarn')
        return ('yarn', 'up', '-R', name) if modern else ('yarn', 'upgrade', name)
    raise PackageUpdateError(f'Direct updates with {manager} are not supported yet. No fallback to a different package manager was attempted.', manager=manager)


# Accept only plain constraints here: includes, markers, extras and URLs require
# different pip semantics and must never be silently ignored.
SIMPLE_REQUIREMENT = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9*+!_.-]+'
    r'(?:\s*,\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9*+!_.-]+)*)?\s*\Z'
)


def _local_venv(directory: Path) -> Path:
    environment = directory / '.venv'
    if environment.is_symlink():
        raise PackageUpdateError('The project environment must be a local .venv directory, not a symlink.')
    config = environment / 'pyvenv.cfg'
    python = environment / 'bin' / 'python'
    if not config.is_file() or not python.is_file():
        raise PackageUpdateError('Create an isolated project-local .venv before updating with pip or Poetry; no global environment was changed.')
    if config.is_symlink() or not python.parent.resolve().is_relative_to(environment.resolve()):
        raise PackageUpdateError('The local .venv configuration and executable directory must not escape through a symlink.')
    storage = [environment / part for part in ('lib', 'lib64', 'Lib', 'include', 'Include')]
    for base in list(storage):
        if base.is_dir():
            storage.extend(base.glob('python*/site-packages'))
            storage.append(base / 'site-packages')
    if any(not target.resolve().is_relative_to(environment.resolve()) for target in storage):
        raise PackageUpdateError('The local .venv installation directories must not escape through a symlink.')
    if not re.search(r'^include-system-site-packages\s*=\s*false\s*$', config.read_text(), re.I | re.M):
        raise PackageUpdateError('The local .venv must disable system site packages before updating.')
    return environment


def _python_command(path: Path, name: str) -> tuple[str, ...]:
    directory = path.parent
    if path.name == 'pyproject.toml':
        uv = (directory / 'uv.lock').is_file()
        poetry = (directory / 'poetry.lock').is_file()
        if uv and poetry:
            raise PackageUpdateError('Found multiple Python manager lockfiles. Keep the chosen manager lockfile before updating.')
        if uv:
            if (directory / '.venv').is_symlink():
                raise PackageUpdateError('The uv project environment must be a local .venv directory, not a symlink.')
            return ('uv', 'sync', '--upgrade-package', name)
        if poetry:
            try:
                _local_venv(directory)
            except PackageUpdateError as exc:
                raise PackageUpdateError(str(exc), manager='poetry') from exc
            return ('poetry', 'update', name)
        raise PackageUpdateError(UNSUPPORTED['pip'])
    for line in path.read_text().splitlines():
        requirement = line.split('#', 1)[0].strip()
        if requirement and not SIMPLE_REQUIREMENT.fullmatch(requirement):
            raise PackageUpdateError('Direct pip updates support only simple requirements with version constraints; includes, options, extras, markers and URLs need the project package manager.')
    environment = _local_venv(directory)
    return (str(environment / 'bin' / 'python'), '-I', '-m', 'pip', '--isolated', '--require-virtualenv',
            'install', '--upgrade', '--upgrade-strategy', 'only-if-needed', '--no-user',
            '--prefix', str(environment), '--constraint', str(path), name)


def update_capability(root: Path, ecosystem: str, name: str, manifest: str, *, detected: list[dict] | None = None) -> dict:
    """Describe support; UI callers may reuse entries from packages.detect(root)."""
    try:
        plan = plan_update(root, ecosystem, name, manifest, detected=detected)
    except (PackageUpdateError, OSError, ValueError) as exc:
        return {'supported': False, 'manager': getattr(exc, 'manager', None) or ecosystem, 'reason': str(exc)}
    manager = 'pip' if plan.argv[0].endswith('/.venv/bin/python') else plan.argv[0]
    if manager == 'bundle':
        manager = 'cocoapods' if ecosystem == 'cocoapods' else 'bundler'
    return {'supported': True, 'manager': manager,
            'reason': 'Updates this dependency within its declared constraints; required transitive lock entries may also change.'}


def plan_update(root: Path, ecosystem: str, name: str, manifest: str, *, detected: list[dict] | None = None) -> UpdatePlan:
    """Validate the chosen manifest entry before returning a fixed native argv."""
    root = Path(root).resolve()
    if not all(isinstance(value, str) and value for value in (ecosystem, name, manifest)):
        raise PackageUpdateError('Choose an installed dependency and its manifest.')
    relative = Path(manifest)
    if relative.is_absolute() or '..' in relative.parts:
        raise PackageUpdateError('The dependency manifest must be inside this project.')
    path = root / relative
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise PackageUpdateError('The dependency manifest must be a file inside this project.')
    entries = packages.detect(root) if detected is None else detected
    item = next((item for item in entries
                 if item['ecosystem'] == ecosystem and item['name'] == name and item['manifest'] == manifest), None)
    if item is None:
        raise PackageUpdateError('That dependency is not declared in the selected project manifest. Refresh Packages and try again.')
    if ecosystem in UNSUPPORTED and ecosystem != 'pip':
        raise PackageUpdateError(UNSUPPORTED[ecosystem])
    pattern = NPM_NAME if ecosystem == 'npm' else GO_NAME if ecosystem in ('go', 'cocoapods') else PLAIN_NAME
    if not pattern.fullmatch(name):
        raise PackageUpdateError('The dependency name contains unsupported options or characters.')
    cwd = path.parent.resolve()
    if ecosystem == 'npm':
        argv = _node_command(cwd, root, name)
    elif ecosystem == 'swiftpm':
        argv = ('swift', 'package', 'update', name.lower())
    elif ecosystem == 'cargo':
        # Manifest aliases are local names; Cargo's package selector uses the registry name.
        data = tomllib.loads(path.read_text())
        selected = name
        for table in ('dependencies', 'dev-dependencies', 'build-dependencies'):
            dependency = (data.get(table) or {}).get(name)
            if isinstance(dependency, dict):
                selected = dependency.get('package', name)
                break
        if not isinstance(selected, str) or not PLAIN_NAME.fullmatch(selected):
            raise PackageUpdateError('The Cargo package name is not a valid single package selector.')
        argv = ('cargo', 'update', '--package', selected)
    elif ecosystem == 'cocoapods':
        bundled = any((parent / 'Gemfile.lock').is_file() for parent in _ancestors(cwd, root))
        argv = ('bundle', 'exec', 'pod', 'update', name) if bundled else ('pod', 'update', name)
    elif ecosystem == 'ruby':
        argv = ('bundle', 'update', '--conservative', name)
    elif ecosystem == 'dart':
        # An SDK dependency requires invoking pub through Flutter. Do not confuse
        # the Dart SDK version constraint in environment.sdk with sdk: flutter.
        flutter = re.search(r"\bsdk\s*:\s*['\"]?flutter(?:['\"]|[\s,}#]|$)", path.read_text())
        argv = ('flutter' if flutter else 'dart', 'pub', 'upgrade', name)
    elif ecosystem == 'go':
        if name in ('go', 'toolchain'):
            raise PackageUpdateError('Go toolchain updates are not dependency updates.')
        argv = ('go', 'get', name + '@upgrade')
    elif ecosystem == 'pip':
        argv = _python_command(path, name)
    else:
        raise PackageUpdateError(f'Direct updates for {ecosystem} are not supported yet.')
    return UpdatePlan(argv, cwd, ecosystem, name, manifest)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    # Output remains attached to the task terminal, including exact manager/test errors.
    return subprocess.call(argv, cwd=cwd, env=env)


def execute_update(root: Path, ecosystem: str, name: str, manifest: str, test_command: str | None, *,
                   run: Callable = _run, say: Callable[[str], None] = print) -> dict:
    """Update once, then run trusted project-configured tests. Never invokes AI."""
    plan = plan_update(root, ecosystem, name, manifest)
    if not isinstance(test_command, str) or not test_command.strip():
        raise PackageUpdateError('Configure the project test command before updating dependencies. The update and tests were not run.')
    env = dict(os.environ)
    if ecosystem == 'pip':
        env['UV_PROJECT_ENVIRONMENT'] = str(plan.cwd / '.venv')
        env['PIP_CONFIG_FILE'] = os.devnull
        for key in ('PYTHONHOME', 'PYTHONPATH', 'CONDA_PREFIX'):
            env.pop(key, None)
        if plan.argv[0] == 'poetry' or plan.argv[0].endswith('/.venv/bin/python'):
            env['VIRTUAL_ENV'] = str(plan.cwd / '.venv')
            env['PATH'] = str(plan.cwd / '.venv' / 'bin') + os.pathsep + env.get('PATH', '')
            env['POETRY_VIRTUALENVS_CREATE'] = 'true'
            env['POETRY_VIRTUALENVS_IN_PROJECT'] = 'true'
    if plan.argv[0] == 'bundle':
        bundle_root = next((parent for parent in _ancestors(plan.cwd, Path(root).resolve())
                            if (parent / 'Gemfile.lock').is_file()), plan.cwd)
        env['BUNDLE_GEMFILE'] = str(bundle_root / 'Gemfile')
    commands = []
    for stage, argv, cwd in (
        ('update', list(plan.argv), plan.cwd),
        ('tests', ['bash', '-lc', test_command], Path(root).resolve()),
    ):
        display = shlex.join(argv) if stage == 'update' else test_command
        commands.append({'stage': stage, 'argv': argv, 'cwd': str(cwd)})
        say(f'Running {stage} in {cwd}: {display}')
        try:
            code = run(argv, cwd=cwd, env=env)
        except OSError as exc:
            message = f'{stage.capitalize()} could not start: {display}: {exc}'
            say(message)
            return {'ok': False, 'stage': stage, 'message': message, 'returncode': 127, 'commands': commands}
        if code:
            message = f'{stage.capitalize()} failed (exit {code}): {display}. See the command output above.'
            say(message)
            return {'ok': False, 'stage': stage, 'message': message, 'returncode': code, 'commands': commands}
    message = f'{name}: native update and project tests passed. The package manager respected the declared version constraints.'
    say(message)
    return {'ok': True, 'stage': 'complete', 'message': message, 'returncode': 0, 'commands': commands}
