"""The terminal drafting flow uses and versions the shared product document."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import prd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator' / 'scripts'))
import draft_from_code


class SharedDraftTests(unittest.TestCase):
    def test_accepting_a_draft_reads_shared_content_and_records_the_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'README.md').write_text('A project')
            legacy = root / prd.PATH
            legacy.parent.mkdir(parents=True)
            legacy.write_text('## Pitch\n\nOutdated tracked product')
            doc = prd.Prd(root, root / '.orchestrator')
            doc.set_section('pitch', 'Shared current product')
            proposed = prd.replace_section(doc.read(), 'features', 'New draft feature')

            def model(model, prompt, **kwargs):
                self.assertIn('Shared current product', prompt)
                self.assertNotIn('Outdated tracked product', prompt)
                return json.dumps({'summary': 'Draft feature', 'markdown': proposed}), 'fake', ''

            with patch.object(draft_from_code, 'run_llm', side_effect=model), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(draft_from_code.main(['--root', str(root), '--model', 'fake', '--yes']), 0)
            self.assertIn('New draft feature', doc.read())
            self.assertEqual(doc.history()[0]['source'], 'draft')
            self.assertIn('Outdated tracked product', legacy.read_text())
