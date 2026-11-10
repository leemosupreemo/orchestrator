"""User-local Node bootstrap: network is faked, archive/checksums are real."""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import ai_providers


class NodePrerequisiteTests(unittest.TestCase):
    def archive(self, unsafe=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name in ['bin/node', 'lib/node_modules/npm/bin/npm-cli.js', 'lib/node_modules/npm/bin/npx-cli.js']:
                data = b'#!/bin/sh\nexit 0\n'
                member = tarfile.TarInfo('../escaped' if unsafe else 'node-v24.22.2-darwin-arm64/' + name)
                member.size, member.mode = len(data), 0o755
                archive.addfile(member, io.BytesIO(data))
            for executable in ['npm', 'npx']:
                link = tarfile.TarInfo('node-v24.22.2-darwin-arm64/bin/' + executable)
                link.type = tarfile.SYMTYPE
                link.linkname = '../lib/node_modules/npm/bin/' + executable + '-cli.js'
                archive.addfile(link)
        return output.getvalue()

    def downloads(self, archive, checksum=None):
        filename = 'node-v24.22.2-darwin-arm64.tar.gz'
        responses = {
            'https://nodejs.org/dist/index.json': json.dumps([
                {'version': 'v25.0.0', 'lts': False},
                {'version': 'v24.22.2', 'lts': 'Krypton', 'files': ['osx-arm64-tar']}]).encode(),
            'https://nodejs.org/dist/v24.22.2/SHASUMS256.txt':
                f'{checksum or hashlib.sha256(archive).hexdigest()}  {filename}\n'.encode(),
            'https://nodejs.org/dist/v24.22.2/' + filename: archive,
        }
        return responses.__getitem__

    def test_installs_verified_lts_under_isolated_home(self):
        from orchestrator.tool_prerequisites import install_node
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': directory}):
            install_node(download=self.downloads(self.archive()), system='Darwin', machine='arm64')
            local_bin = Path(directory) / '.local/bin'
            self.assertTrue(os.access(local_bin / 'node', os.X_OK))
            self.assertTrue((local_bin / 'npm').resolve().is_relative_to(Path(directory).resolve()))
            self.assertIn(str(local_bin), ai_providers.tool_path('/usr/bin').split(os.pathsep))

    def test_checksum_failure_does_not_publish_executables(self):
        from orchestrator.tool_prerequisites import install_node
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': directory}):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                install_node(download=self.downloads(self.archive(), '0' * 64), system='Darwin', machine='arm64')
            self.assertFalse((Path(directory) / '.local/bin/node').exists())

    def test_archive_cannot_escape_install_directory(self):
        from orchestrator.tool_prerequisites import install_node
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME': directory}):
            with self.assertRaisesRegex(RuntimeError, 'archive'):
                install_node(download=self.downloads(self.archive(unsafe=True)), system='Darwin', machine='arm64')
            self.assertFalse((Path(directory) / 'escaped').exists())

    def test_unsupported_platform_fails_without_download(self):
        from orchestrator.tool_prerequisites import install_node
        def forbidden(url):
            self.fail('Unsupported platforms must not download')
        with self.assertRaisesRegex(RuntimeError, 'supported'):
            install_node(download=forbidden, system='Windows', machine='arm64')

    def test_override_tool_directories_never_add_host_paths(self):
        with patch.dict(os.environ, {'HOME': '/isolated', 'ORCHESTRATOR_TOOL_DIRS': '/isolated/.local/bin'}):
            self.assertEqual(ai_providers.tool_path('/usr/bin'), '/usr/bin:/isolated/.local/bin')


class DownloadTrustTests(unittest.TestCase):
    def test_uses_system_curl_trust_when_python_ca_bundle_is_missing(self):
        import ssl
        import subprocess
        import urllib.error
        from orchestrator import tool_prerequisites as tools
        error=urllib.error.URLError(ssl.SSLCertVerificationError('missing Python CA certificate'))
        with patch.object(tools.urllib.request,'urlopen',side_effect=error), patch.object(tools.shutil,'which',return_value='/usr/bin/curl'), patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=b'[]',stderr=b'')) as run:
            self.assertEqual(tools._download('https://nodejs.org/dist/index.json'),b'[]')
            argv=run.call_args.args[0]
            self.assertEqual(argv[1], '--disable')
            self.assertNotIn('-k',argv)
            self.assertNotIn('--insecure',argv)
            self.assertIn('--proto-redir',argv)

    def test_other_download_failures_do_not_turn_off_certificate_checks(self):
        import urllib.error
        from orchestrator import tool_prerequisites as tools
        with patch.object(tools.urllib.request,'urlopen',side_effect=urllib.error.URLError('offline')), patch('subprocess.run') as run:
            with self.assertRaises(urllib.error.URLError):
                tools._download('https://nodejs.org/dist/index.json')
            run.assert_not_called()
