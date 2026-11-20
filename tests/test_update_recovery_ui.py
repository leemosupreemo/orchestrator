import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class PreviewUpdateUiTests(unittest.TestCase):
    def test_recovery_lifecycle_in_node(self):
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('update-recovery-ui.test.cjs'))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
