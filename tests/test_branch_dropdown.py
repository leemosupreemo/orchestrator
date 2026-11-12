"""Branch grouping and downloads, using disposable local Git remotes."""
import json
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from orchestrator.web import server as ui


class BranchDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'repo'
        self.remote = self.base / 'origin.git'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.test')
        (self.root / 'file.txt').write_text('original\n')
        self.git('add', 'file.txt')
        self.git('commit', '-qm', 'initial')
        self.git('init', '--bare', '-q', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-qu', 'origin', 'main')
        self.git('remote', 'set-head', 'origin', 'main')
        self.add_remote_branch('feature/new')
        runtime = self.root / '.orchestrator'
        runtime.mkdir()
        (runtime / 'project.json').write_text(json.dumps({'project_name': 'Demo', 'code_host': 'git'}))
        (self.root / '.git/info/exclude').write_text('.orchestrator/\n')

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def add_remote_branch(self, name):
        self.git('branch', name)
        self.git('push', '-q', 'origin', name)
        self.git('branch', '-D', name)

    def execute(self, argv):
        return subprocess.run(argv, cwd=self.root, check=True, capture_output=True, text=True)

    def test_both_state_payloads_offer_only_remote_branches_missing_locally(self):
        with patch.object(ui, 'auto_discover_local_projects'), \
             patch.object(ui, 'load_recent_projects', return_value={'projects': []}), \
             patch.object(ui, 'get_project_languages', return_value=[]), \
             patch.object(ui, 'github_repos', return_value={'repos': []}):
            states = [ui.git_state(self.root), ui.project_state(self.root)]
        for state in states:
            self.assertIn('download_branches', state)
            self.assertEqual(state['branches'], ['main'])
            self.assertEqual(state['download_branches'], [
                {'name': 'feature/new', 'ref': 'origin/feature/new', 'remote': 'origin'},
            ])

    def test_download_fetches_the_latest_commit_and_sets_tracking(self):
        writer = self.base / 'writer'
        self.git('clone', '-q', str(self.remote), str(writer))
        self.git('checkout', '-q', 'feature/new', cwd=writer)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit',
                 '--allow-empty', '-qm', 'new remote commit', cwd=writer)
        latest = self.git('rev-parse', 'HEAD', cwd=writer)
        self.git('push', '-q', 'origin', 'feature/new', cwd=writer)
        self.assertNotEqual(self.git('rev-parse', 'origin/feature/new'), latest)
        self.execute(ui.build_git_checkout({'branch': 'remote:origin/feature/new'}, self.root))
        self.assertEqual(self.git('branch', '--show-current'), 'feature/new')
        self.assertEqual(self.git('rev-parse', 'HEAD'), latest)
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/feature/new')

    def test_download_can_stash_changes_before_switching(self):
        (self.root / 'file.txt').write_text('local work\n')
        self.execute(ui.build_stash_checkout({'branch': 'remote:origin/feature/new'}, self.root))
        self.assertEqual(self.git('branch', '--show-current'), 'feature/new')
        self.assertEqual((self.root / 'file.txt').read_text(), 'original\n')
        self.assertIn('local work', self.git('stash', 'show', '-p'))

    def test_download_rejects_unknown_refs_and_existing_local_branches(self):
        for branch in ('remote:origin/missing', 'remote:origin/main', 'remote:origin/feature/new;echo bad'):
            with self.subTest(branch=branch), self.assertRaises(ui.UIError):
                ui.build_git_checkout({'branch': branch}, self.root)

    def test_download_keeps_branch_names_out_of_shell_code(self):
        name = 'feature/$(touch${IFS}owned)'
        self.add_remote_branch(name)
        self.execute(ui.build_git_checkout({'branch': 'remote:origin/' + name}, self.root))
        self.assertEqual(self.git('branch', '--show-current'), name)
        self.assertFalse((self.root / 'owned').exists())

    def test_choices_keep_remote_identity_and_exclude_local_names(self):
        self.git('remote', 'add', 'team/fork', str(self.remote))
        self.git('fetch', '-q', 'team/fork')
        state = ui.git_state(self.root)
        self.assertIn('download_branches', state)
        self.assertEqual({b['ref'] for b in state['download_branches']},
                         {'origin/feature/new', 'team/fork/feature/new'})
        self.assertEqual({b['name'] for b in state['download_branches']}, {'feature/new'})
        self.git('branch', 'feature/new')
        self.assertEqual(ui.git_state(self.root)['download_branches'], [])

    def test_download_honors_custom_fetch_mapping_instead_of_fetching_a_different_branch(self):
        self.git('symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.git('update-ref', '-d', 'refs/remotes/origin/feature/new')
        self.git('config', '--replace-all', 'remote.origin.fetch', '+refs/heads/release/*:refs/remotes/origin/*')
        self.add_remote_branch('release/v1')
        expected = self.git('rev-parse', 'HEAD')
        different = self.git('commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'different branch')
        self.git('update-ref', 'refs/heads/v1', different)
        self.git('push', '-q', 'origin', 'v1')
        self.git('branch', '-D', 'v1')
        self.assertEqual(ui.git_state(self.root)['download_branches'], [
            {'name': 'release/v1', 'ref': 'origin/v1', 'remote': 'origin'},
        ])
        self.execute(ui.build_git_checkout({'branch': 'remote:origin/v1'}, self.root))
        self.assertEqual(self.git('branch', '--show-current'), 'release/v1')
        self.assertEqual(self.git('rev-parse', 'HEAD'), expected)
        self.assertEqual(self.git('config', 'branch.release/v1.merge'), 'refs/heads/release/v1')

    def test_download_does_not_offer_branches_excluded_by_fetch_configuration(self):
        self.git('config', '--add', 'remote.origin.fetch', '^refs/heads/feature/*')
        self.assertEqual(ui.git_state(self.root)['download_branches'], [])


class GroupedBranchMarkupTests(unittest.TestCase):
    def render(self, branches, current, downloads, elsewhere=None, last=None):
        static = Path(__file__).resolve().parents[1] / 'orchestrator/web/static'
        script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[3]);
const context = {state: {project: {last_branch: input.last}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
context.esc = context.Html.escape;
vm.runInContext(source.slice(source.indexOf('function branchOptions('), source.indexOf('// The branch you last chose')), context);
process.stdout.write(context.branchOptions(input.branches, input.current, input.elsewhere, input.downloads));
"""
        values = {'branches': branches, 'current': current, 'downloads': downloads, 'elsewhere': elsewhere or {}, 'last': last}
        return subprocess.run(['node', '-e', script, str(static / 'app.js'), str(static / 'html.js'), json.dumps(values)],
                              check=True, capture_output=True, text=True).stdout

    def test_remote_only_repository_does_not_appear_to_have_already_selected_a_download(self):
        rendered = self.render([], 'main', [{'name': 'feature', 'ref': 'origin/feature', 'remote': 'origin'}])
        self.assertIn('<option value="" selected disabled>main</option>', rendered)
        self.assertNotIn('Installed locally', rendered)
        self.assertIn('<optgroup label="Download">', rendered)

    def test_local_group_is_first_and_remote_options_preserve_identity(self):
        rendered = self.render(['main', 'local&branch'], 'main', [
            {'name': 'feature<new>', 'ref': 'origin/feature<new>', 'remote': 'origin'},
            {'name': 'feature<new>', 'ref': 'fork/feature<new>', 'remote': 'fork'},
        ], {'local&branch': '/other'}, 'local&branch')

        class Options(HTMLParser):
            def __init__(self):
                super().__init__()
                self.groups, self.options, self.text = [], [], []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'optgroup': self.groups.append(attrs['label'])
                if tag == 'option': self.options.append((self.groups[-1], attrs))
            def handle_data(self, data): self.text.append(data)

        parsed = Options()
        parsed.feed(rendered)
        self.assertEqual(parsed.groups, ['Installed locally', 'Download'])
        self.assertEqual([attrs['value'] for _, attrs in parsed.options],
                         ['main', 'local&branch', 'remote:origin/feature<new>', 'remote:fork/feature<new>'])
        self.assertIn('selected', parsed.options[0][1])
        self.assertIn('local&branch (last used) (in another folder)', parsed.text)
        self.assertNotIn('<new>', rendered)
