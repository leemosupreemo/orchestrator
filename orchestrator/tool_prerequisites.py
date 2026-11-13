"""Install the npm prerequisite from verified official Node binaries without administrator access."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from typing import Callable

DIST = 'https://nodejs.org/dist'


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except urllib.error.URLError as exc:
        # Framework Python installs on macOS can lack their own CA bundle. The OS curl still verifies TLS using system trust.
        curl = shutil.which("curl")
        if not isinstance(exc.reason, ssl.SSLCertVerificationError) or not curl:
            raise
        result = subprocess.run([curl, "--disable", "--fail", "--silent", "--show-error", "--location", "--max-time", "120",
                                 "--proto", "=https", "--proto-redir", "=https", url], capture_output=True, check=False, timeout=125)
        if result.returncode:
            raise RuntimeError("Could not download Node.js: " + result.stderr.decode("utf-8", "replace")[-300:]) from exc
        return result.stdout


def install_node(*, download: Callable[[str], bytes] = _download,
                 system: str | None = None, machine: str | None = None) -> None:
    """Publish Node/npm/npx in this HOME's ~/.local/bin only after checksum and archive validation."""
    system, machine = system or platform.system(), machine or platform.machine()
    operating_system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(system)
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(machine)
    if not operating_system or not arch:
        raise RuntimeError(f'Automatic Node.js installation is not supported on {system} {machine}.')
    try:
        releases = json.loads(download(f'{DIST}/index.json'))
        artifact = f'osx-{arch}-tar' if system == 'Darwin' else f'linux-{arch}'
        version = next(item['version'] for item in releases if item.get('lts') and artifact in item.get('files', []))
        if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
            raise ValueError('Invalid Node release version')
        name = f'node-{version}-{operating_system}-{arch}'
        filename = name + '.tar.gz'
        checksums = download(f'{DIST}/{version}/SHASUMS256.txt').decode()
        checksum = next(line.split()[0] for line in checksums.splitlines()
                        if len(line.split()) == 2 and line.split()[1] == filename)
        archive_bytes = download(f'{DIST}/{version}/{filename}')
        if hashlib.sha256(archive_bytes).hexdigest() != checksum:
            raise RuntimeError('Node.js download checksum did not match; nothing was installed.')
        home = Path.home()
        destination = home / '.local/share/orchestrator/node' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            staging = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
                members = archive.getmembers()
                for member in members:
                    member_path = posixpath.normpath(member.name)
                    if member_path != name and not member_path.startswith(name + '/'):
                        raise RuntimeError('Unsafe Node.js archive path.')
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise RuntimeError('Unsupported Node.js archive entry.')
                    if member.issym() or member.islnk():
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname)
                                                    if member.issym() else member.linkname)
                        if not target.startswith(name + '/'):
                            raise RuntimeError('Unsafe Node.js archive link.')
                archive.extractall(staging, members=members, filter='data')
            unpacked = staging / name
            if not all((unpacked / 'bin' / executable).is_file() for executable in ('node', 'npm', 'npx')):
                raise RuntimeError('Node.js archive is missing required executables.')
            if not destination.exists():
                shutil.move(str(unpacked), destination)
        local_bin = home / '.local/bin'
        local_bin.mkdir(parents=True, exist_ok=True)
        for executable in ('node', 'npm', 'npx'):
            link = local_bin / executable
            if link.exists() and not link.is_symlink():
                raise RuntimeError(f'Cannot replace existing {link}.')
        for executable in ('node', 'npm', 'npx'):
            link = local_bin / executable
            link.unlink(missing_ok=True)
            link.symlink_to(destination / 'bin' / executable)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, tarfile.TarError) as exc:
        raise RuntimeError(f'Could not install Node.js: {exc}') from exc
