from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestrator import cli


class WizardUXTests(unittest.TestCase):
    def test_text_accepts_s_q_and_spaces_in_paths(self):
        with patch('orchestrator.scripts.common.get_key', side_effect=[*'src/sql', 'space', *'tests', 'enter']), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.prompt_text('Path'), 'src/sql tests')

    def test_invalid_yes_no_input_reprompts_instead_of_declining(self):
        with patch('orchestrator.scripts.common.get_key', side_effect=['x', 'y']), redirect_stdout(io.StringIO()):
            self.assertTrue(cli.prompt_yes_no('Apply?'))

    def run_wizard(self, root, keys, result=0, extra=()):
        output = io.StringIO()
        with patch.dict(os.environ, {'ORCHESTRATOR_USER_STATE_DIR': str(root / 'user-state')}), patch('orchestrator.scripts.common.get_key', side_effect=keys), patch('orchestrator.cli.run_script', return_value=result), redirect_stdout(output):
            status = cli.main(['wizard', '--root', str(root), '--models', 'codex', *extra])
        return status, output.getvalue()

    def test_cancel_at_review_leaves_new_project_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, output = self.run_wizard(root, ['enter', 'enter', 'c', 'enter'])
            self.assertEqual(status, 0)
            self.assertFalse((root / '.orchestrator').exists())
            self.assertFalse((root / 'AGENTS.md').exists())
            self.assertIn('cancel', output.lower())

    def test_quick_setup_skips_optional_tools_and_finishes_after_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, output = self.run_wizard(root, ['enter', 'enter', 'enter'])
            self.assertEqual(status, 0)
            config = json.loads((root / '.orchestrator/project.json').read_text())
            self.assertEqual(config['base_branch'], 'main')
            self.assertFalse(config['firebase_distribution'])
            self.assertFalse((root / '.orchestrator/prompts').exists())
            self.assertLess(output.index('Configuration OK'), output.index('Wizard Complete'))
            self.assertIn('orchestrator console', output)

    def test_failed_indexing_never_reports_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, output = self.run_wizard(root, ['enter', 'enter', 'enter'], result=1)
            self.assertEqual(status, 1)
            self.assertNotIn('Wizard Complete', output)
            self.assertIn('saved', output.lower())

    def test_failed_noninteractive_verification_never_reports_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, output = self.run_wizard(root, [], result=1, extra=['--non-interactive', '--verify'])
            self.assertEqual(status, 1)
            self.assertNotIn('Wizard Complete', output)

    def test_review_edits_are_saved_only_after_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, _ = self.run_wizard(root, [
                'enter', 'enter', 'p', 'enter', '2', 'enter',
                *'release', 'enter', 'enter', 'enter',
            ])
            self.assertEqual(status, 0)
            config = json.loads((root / '.orchestrator/project.json').read_text())
            self.assertEqual(config['base_branch'], 'release')
            self.assertEqual(config['pr_base_branch'], 'release')

    def test_cancel_preserves_existing_files_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            self.run_wizard(root, ['enter', 'enter', 'enter'])
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            status, _ = self.run_wizard(root, ['enter', 'enter', 'c', 'enter'], extra=['--force', '--copy-prompt-overrides'])
            self.assertEqual(status, 0)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(after, before)

    def test_provider_key_is_not_written_before_review(self):
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
                output = io.StringIO()
                keys = ['enter', 'k', 'enter', 'enter', 'enter'] if apply else ['enter', 'k', 'enter', 'enter', 'c', 'enter']
                with patch.dict(os.environ, {'ORCHESTRATOR_USER_STATE_DIR': str(root / 'user-state')}), patch('orchestrator.cli.check_cli_auth', return_value=(False, 'Not authenticated')), patch('orchestrator.cli.prompt_radio', return_value='OpenAI Codex (openai_api_key)'), patch('orchestrator.cli.prompt_password', return_value='test-key-not-a-secret'), patch('orchestrator.scripts.common.get_key', side_effect=keys), patch('orchestrator.cli.run_script', return_value=0), redirect_stdout(output):
                    self.assertEqual(cli.main(['wizard', '--root', str(root)]), 0)
                settings_file = root / '.orchestrator/config/settings.json'
                self.assertNotIn('test-key-not-a-secret', output.getvalue())
                if apply:
                    self.assertEqual(json.loads(settings_file.read_text())['openai_api_key'], 'test-key-not-a-secret')
                else:
                    self.assertFalse(settings_file.exists())

    def test_numbered_selection_rejects_invalid_numbers(self):
        with patch('orchestrator.scripts.common.get_key', side_effect=['9', 'enter', '2', ',', '1', 'enter']), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wizard_choose_items('Models', [('codex', 'Codex'), ('claude', 'Claude')], [], None), ['claude', 'codex'])

    def test_back_from_radio_returns_to_section_without_crashing(self):
        from orchestrator.scripts.common import BackException
        with patch('orchestrator.scripts.common.prompt_radio', side_effect=BackException()):
            with self.assertRaises(cli.SkipSectionException):
                cli.prompt_radio('Provider', ['Codex'])

    def test_skip_worker_install_prompt_still_reaches_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            status, output = self.run_wizard(root, ['enter', 'enter', 's', 'c', 'enter'], extra=['--ssh-machine', 'mac2=host:/tmp/repo'])
            self.assertEqual(status, 0)
            self.assertIn('cancel', output.lower())
            self.assertFalse((root / '.orchestrator').exists())

    def test_xcode_check_failure_prevents_success_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Example.xcodeproj').mkdir()
            with patch('orchestrator.cli.infer_xcode', return_value=('Example.xcodeproj', None, 'Example', ['Example'], ['ExampleTests'])), patch('orchestrator.cli.subprocess.run', side_effect=OSError('Xcode unavailable')):
                status, output = self.run_wizard(root, ['enter', 'enter', 'enter', 'enter'])
            self.assertEqual(status, 1)
            self.assertNotIn('Wizard Complete', output)
            self.assertIn('Xcode validation failed', output)

    def test_force_can_repair_malformed_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pyproject.toml').write_text('[project]\nname="example"\n')
            runtime = root / '.orchestrator'
            runtime.mkdir()
            (runtime / 'project.json').write_text('{broken')
            status, _ = self.run_wizard(root, [], extra=['--force', '--non-interactive'])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads((runtime / 'project.json').read_text())['base_branch'], 'main')

    def test_noninteractive_unknown_project_rejects_missing_build_and_test_commands_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, output = self.run_wizard(root, [], extra=['--non-interactive'])

            self.assertEqual(status, 1)
            self.assertFalse((root / '.orchestrator').exists())
            self.assertIn('Build Command is required', output)
            self.assertIn('Test Command is required', output)


if __name__ == '__main__':
    unittest.main()
