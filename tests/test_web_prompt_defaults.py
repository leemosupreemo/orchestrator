"""Browser actions must not wait for keystrokes in their output pseudo-terminal."""
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import unittest


class WebPromptDefaultsTests(unittest.TestCase):
    def test_model_machine_and_branch_prompts_continue_without_terminal_input(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import common
print(json.dumps({
    "models": common.prompt_checkbox("select models", ["configured", "other"], ["configured"]),
    "machines": common.prompt_checkbox("Select allowed machines", ["local"], ["local"]),
    "branch": common.prompt_radio("Branch", ["new", "current"], "current"),
    "confirm": common.prompt_confirm("Continue?", default=False),
    "password": common.prompt_password("Password"),
    "input": common.prompt_input("Name", default="saved"),
    "pause": input("Tap Enter to return to menu...")
}))
'''
        primary, secondary = pty.openpty()
        try:
            result = subprocess.run([sys.executable, '-c', code, str(root / 'orchestrator/scripts')],
                cwd=root, env={**os.environ, 'ORCHESTRATOR_NONINTERACTIVE': '1'}, stdin=secondary,
                capture_output=True, text=True, timeout=3)
        finally:
            os.close(primary)
            os.close(secondary)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), {
            'models': ['configured'], 'machines': ['local'], 'branch': 'current',
            'confirm': False, 'password': '', 'input': 'saved', 'pause': ''})
