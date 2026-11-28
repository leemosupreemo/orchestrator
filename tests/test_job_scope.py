from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import job_scope


class JobScopeTests(unittest.TestCase):
    def test_mixed_test_and_production_proposals_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'app.py'
            source.write_text('original')
            with self.assertRaises(ValueError):
                job_scope.write_files(root, 'test-audit', 'job-1', [
                    {'path': 'tests/test_app.py', 'content': 'test'},
                    {'path': 'app.py', 'content': 'changed'},
                ])
            self.assertEqual(source.read_text(), 'original')
            self.assertFalse((root / 'tests/test_app.py').exists())

    def test_tests_accept_test_files_across_supported_stacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ['tests/test_app.py', 'AppTests/AuthTests.swift', 'src/app.test.ts', 'src/app_test.go', 'src/test/java/AppTest.java']
            written = job_scope.write_files(root, 'test-audit', 'job-1', [
                {'path': path, 'content': 'test content'} for path in paths
            ])
            self.assertEqual(written, paths)
            self.assertTrue(all((root / path).read_text() == 'test content' for path in paths))

    def test_tests_reject_production_and_traversal_paths(self):
        for path in ['src/app.py', 'src/tests.py', '../tests/test_app.py', '/tests/test_app.py', '.git/hooks/test_hook', 'tests/../app.py', 'tests/package.json']:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    job_scope.write_files(Path(tmp), 'test-audit', 'job-1', [{'path': path, 'content': 'changed'}])

    def test_symlinks_cannot_turn_test_paths_into_production_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'src').mkdir()
            (root / 'tests').symlink_to(root / 'src', target_is_directory=True)
            with self.assertRaises(ValueError):
                job_scope.write_files(root, 'test-audit', 'job-1', [{'path': 'tests/test_app.py', 'content': 'changed'}])
            self.assertFalse((root / 'src/test_app.py').exists())

    def test_hardlinks_cannot_turn_test_paths_into_production_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'app.py'
            source.write_text('original')
            (root / 'tests').mkdir()
            (root / 'tests/test_alias.py').hardlink_to(source)
            with self.assertRaises(ValueError):
                job_scope.write_files(root, 'test-audit', 'job-1', [{'path': 'tests/test_alias.py', 'content': 'changed'}])
            self.assertEqual(source.read_text(), 'original')

    def test_designs_are_saved_as_visual_mockups_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = job_scope.write_files(root, 'feature-design', 'job-1', [
                {'path': 'screen.html', 'content': '<html>Mock screen</html>'},
                {'path': 'screen.svg', 'content': '<svg xmlns="http://www.w3.org/2000/svg"/>'},
            ])
            self.assertEqual(paths, ['docs/product/designs/job-1/screen.html', 'docs/product/designs/job-1/screen.svg'])
            with self.assertRaises(ValueError):
                job_scope.write_files(root, 'feature-design', 'job-1', [{'path': 'app.py', 'content': 'production'}])

    def test_invalid_batches_preserve_existing_files(self):
        cases = [
            ("feature-plan", "job-1", [{"path": "tests/test_x.py", "content": "x"}]),
            ("test-audit", "../bad", [{"path": "tests/test_x.py", "content": "x"}]),
            ("test-audit", "job-1", None), ("test-audit", "job-1", []),
            ("test-audit", "job-1", [None]),
            ("test-audit", "job-1", [{"path": 1, "content": "x"}]),
            ("test-audit", "job-1", [{"path": "tests/test_x.py", "content": None}]),
            ("feature-design", "job-1", [{"path": "nested/screen.html", "content": "x"}]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind, job_id, proposals in cases:
                with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                    job_scope.write_files(root, kind, job_id, proposals)
            self.assertEqual(list(root.iterdir()), [])
            proposal = {"path": "tests/test_x.py", "content": "x"}
            with self.assertRaisesRegex(ValueError, "duplicate"):
                job_scope.write_files(root, "test-audit", "job-1", [proposal, proposal])
            target = root / "tests/test_x.py"
            target.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "not a file"):
                job_scope.write_files(root, "test-audit", "job-1", [proposal])

    def test_linux_sandbox_quotes_command_and_deduplicates_roots(self):
        import shlex
        with patch.object(job_scope.platform, "system", return_value="Linux"), \
             patch.object(job_scope.shutil, "which", return_value="/bin/bwrap"):
            args = shlex.split(job_scope.protect_command("echo 'hello world'", [Path("/project"), Path("/project")]))
        self.assertEqual(args, ["/bin/bwrap", "--die-with-parent", "--bind", "/", "/",
                                "--ro-bind", "/project", "/project", "--", "/bin/sh", "-c", "echo 'hello world'"])

    def test_missing_sandbox_refuses_to_run(self):
        with patch.object(job_scope.shutil, 'which', return_value=None):
            with self.assertRaises(RuntimeError):
                job_scope.protect_command('true', [Path('/project')])

    @unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('sandbox-exec'), 'macOS sandbox integration')
    def test_real_process_cannot_change_production_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'app.py'
            source.write_text('original')
            code = 'from pathlib import Path; Path(' + repr(str(source)) + ').write_text("changed")'
            import shlex
            command = job_scope.protect_command(shlex.join([sys.executable, '-c', code]), [root])
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
