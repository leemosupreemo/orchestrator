"""Git setup behavior against isolated repositories; no external hosts are contacted."""
import json
import subprocess
from unittest.mock import patch
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.web import server as ui
from tests import test_web_ui as web_tests


class GitSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("commit", "-q", "--allow-empty", "-m", "Initial")

    def git(self, *args, root=None):
        return subprocess.run(["git", *args], cwd=root or self.root, capture_output=True,
                              text=True, check=True).stdout.strip()

    def remote(self, name="origin"):
        remote = Path(self.tmp.name) / (name + ".git")
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", name, str(remote))
        return remote

    def test_hosted_projects_are_not_classified_as_local_or_github(self):
        for remote, label in [
            ("git@gitlab.com:team/sub/app.git", "GitLab"),
            ("https://bitbucket.org/team/app.git", "Bitbucket"),
            ("https://dev.azure.com/team/project/_git/app", "Azure DevOps"),
            ("ssh://git@gitea.example.com:2222/team/app.git", "gitea.example.com"),
        ]:
            with self.subTest(remote=remote):
                if self.git("remote"):
                    self.git("remote", "set-url", "origin", remote)
                else:
                    self.git("remote", "add", "origin", remote)
                self.assertEqual(ui.detect_project_source(self.root), ("git", label, None))

    def test_local_path_remote_is_still_a_connected_repository(self):
        self.remote()
        self.assertEqual(ui.detect_project_source(self.root), ("git", "Git remote", None))

    def test_push_keeps_the_existing_remote_and_differently_named_upstream(self):
        origin = self.remote()
        upstream = self.remote("team")
        self.git("push", "-q", "team", "main:review")
        self.git("fetch", "-q", "team")
        self.git("branch", "--set-upstream-to=team/review", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "Next")
        subprocess.run(ui.build_git_push({}, self.root), cwd=self.root, check=True, capture_output=True)
        self.assertEqual(self.git("rev-parse", "refs/heads/review", root=upstream), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("branch", root=origin), "")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}"), "team/review")
        self.assertEqual(ui.build_git_pull({}, self.root), ["git", "pull", "--ff-only", "team", "refs/heads/review"])

    def test_first_push_sets_tracking_on_origin(self):
        self.remote()
        subprocess.run(ui.build_git_push({}, self.root), cwd=self.root, check=True, capture_output=True)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}"), "origin/main")

    def test_connect_updates_origin_and_workflow_without_losing_project_settings(self):
        project = ui.runtime_dir(self.root) / "project.json"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(json.dumps({"name": "App", "base_branch": "main", "custom": 42}))
        ui.git_connection(self.root, {"op": "connect", "url": "git@gitlab.com:team/app.git", "mode": "auto"})
        self.assertEqual(self.git("remote", "get-url", "origin"), "git@gitlab.com:team/app.git")
        saved = json.loads(project.read_text())
        self.assertEqual(saved["custom"], 42)
        self.assertEqual(saved["git_remote"], "git@gitlab.com:team/app.git")
        self.assertEqual(saved["code_host"], "auto")
        ui.git_connection(self.root, {"op": "connect", "url": "git@bitbucket.org:team/app.git", "mode": "git"})
        self.assertEqual(self.git("remote", "get-url", "origin"), "git@bitbucket.org:team/app.git")

    def test_local_workflow_does_not_remove_an_existing_remote(self):
        self.remote()
        before = self.git("remote", "get-url", "origin")
        ui.git_connection(self.root, {"op": "workflow", "mode": "git"})
        self.assertEqual(self.git("remote", "get-url", "origin"), before)
        saved = json.loads((ui.runtime_dir(self.root) / "project.json").read_text())
        self.assertEqual(saved["code_host"], "git")

    def test_invalid_connection_leaves_origin_unchanged(self):
        self.remote()
        before = self.git("remote", "get-url", "origin")
        for url in ["-evil", "https://user:secret@gitlab.com/team/app", "https://gitlab.com/team/app\n--evil", "not a remote"]:
            with self.subTest(url=url), self.assertRaises(ui.UIError):
                ui.git_connection(self.root, {"op": "connect", "url": url, "mode": "auto"})
        with self.assertRaises(ui.UIError):
            ui.git_connection(self.root, {"op": "connect", "url": "git@gitlab.com:team/app.git", "mode": "github"})
        self.assertEqual(self.git("remote", "get-url", "origin"), before)

    def test_connection_check_reads_a_real_remote(self):
        self.remote()
        self.git("push", "-q", "origin", "main")
        result = ui.git_connection(self.root, {"op": "check"})
        self.assertTrue(result["ok"])
        self.assertIn("read", result["detail"].lower())

    def test_sync_prerequisites_distinguish_no_repository_no_commits_and_no_remote(self):
        folder = Path(self.tmp.name) / "new"
        folder.mkdir()
        g = ui.git_state(folder)
        self.assertFalse(g["is_repo"])
        self.assertIn("Initialize", g["push_reason"])
        self.git("init", "-q", "-b", "main", root=folder)
        g = ui.git_state(folder)
        self.assertTrue(g["is_repo"])
        self.assertFalse(g["has_commits"])
        self.assertIn("first commit", g["push_reason"])
        g = ui.git_state(self.root)
        self.assertTrue(g["has_commits"])
        self.assertFalse(g["can_push"])
        self.assertIn("remote", g["push_reason"])
        with self.assertRaises(ui.UIError):
            ui.build_git_push({}, self.root)

    def test_detached_head_blocks_sync_but_still_allows_fetch(self):
        self.remote()
        self.git("checkout", "-q", "--detach")
        g = ui.git_state(self.root)
        self.assertFalse(g["can_push"])
        self.assertFalse(g["can_pull"])
        self.assertTrue(g["can_fetch"])
        self.assertIn("Switch to a branch", g["push_reason"])

    def test_tracking_selection_can_use_a_differently_named_remote_branch(self):
        self.remote("team")
        self.git("push", "-q", "team", "main:review")
        self.git("fetch", "-q", "team")
        ui.git_tracking(self.root, {"upstream": "team/review"})
        g = ui.git_state(self.root)
        self.assertTrue(g["can_push"])
        self.assertTrue(g["can_pull"])
        self.assertEqual(g["sync_remote"], "team")
        self.assertEqual(g["sync_ref"], "refs/heads/review")
        self.assertIn("team/review", g["remote_branches"])
        with self.assertRaises(ui.UIError):
            ui.git_tracking(self.root, {"upstream": "team/not-found"})

    def test_pull_blocks_local_edits_and_diverged_history(self):
        remote = self.remote()
        self.git("push", "-qu", "origin", "main")
        (self.root / "new.txt").write_text("local edits")
        self.assertIn("Commit or stash", ui.git_state(self.root)["pull_reason"])
        (self.root / "new.txt").unlink()
        self.git("commit", "-q", "--allow-empty", "-m", "Local")
        other = Path(self.tmp.name) / "other"
        self.git("clone", "-q", "--branch", "main", str(remote), str(other))
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "--allow-empty", "-m", "Remote", root=other)
        self.git("push", "-q", "origin", "main", root=other)
        self.git("fetch", "-q", "origin")
        g = ui.git_state(self.root)
        self.assertFalse(g["can_pull"])
        self.assertFalse(g["can_push"])
        self.assertIn("diverged", g["pull_reason"])
        with self.assertRaises(ui.UIError):
            ui.build_git_pull({}, self.root)

    def test_fetch_updates_counts_and_records_only_successful_fetches(self):
        remote = self.remote()
        self.git("push", "-qu", "origin", "main")
        self.assertIsNone(ui.git_state(self.root)["last_fetch"])
        other = Path(self.tmp.name) / "other"
        self.git("clone", "-q", "--branch", "main", str(remote), str(other))
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "--allow-empty", "-m", "Remote", root=other)
        self.git("push", "-q", "origin", "main", root=other)
        self.assertEqual(ui.git_state(self.root)["behind"], 0)
        before = self.git("rev-parse", "HEAD")
        ui.git_fetch(self.root)
        g = ui.git_state(self.root)
        self.assertEqual(g["behind"], 1)
        self.assertIsInstance(g["last_fetch"], (int, float))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.git("remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        with self.assertRaises(ui.UIError):
            ui.git_fetch(self.root)
        self.assertEqual(ui.git_state(self.root)["last_fetch"], g["last_fetch"])

    def test_fetch_can_discover_branches_without_origin_or_tracking(self):
        self.remote("team")
        self.git("push", "-q", "team", "main:review")
        self.assertTrue(ui.git_state(self.root)["can_fetch"])
        ui.git_fetch(self.root)
        self.assertIn("team/review", ui.git_state(self.root)["remote_branches"])

    def test_changes_separate_staged_unstaged_and_untracked_with_rename_paths(self):
        (self.root / "old name.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "File")
        self.git("mv", "old name.txt", "new name.txt")
        (self.root / "new name.txt").write_text("edited\n")
        (self.root / "folder").mkdir()
        (self.root / "folder/new.txt").write_text("untracked\n")
        groups = {g["id"]: g["changes"] for g in ui.git_state(self.root)["change_groups"]}
        self.assertEqual(groups["staged"][0]["label"], "Renamed")
        self.assertEqual(groups["staged"][0]["old_path"], "old name.txt")
        self.assertEqual(groups["unstaged"][0]["label"], "Modified")
        self.assertEqual(groups["untracked"][0]["path"], "folder/new.txt")
        self.assertEqual(ui.git_state(self.root)["changes_total"], 2)

    def test_diff_keeps_staged_and_unstaged_edits_separate_and_literal(self):
        path = "[literal].txt"
        (self.root / path).write_text("original\n")
        self.git("add", path)
        self.git("commit", "-qm", "File")
        (self.root / path).write_text("staged\n")
        self.git("add", path)
        (self.root / path).write_text("unstaged\n")
        staged = ui.git_file_diff(self.root, path, "staged")["diff"]
        unstaged = ui.git_file_diff(self.root, path, "unstaged")["diff"]
        self.assertIn("+staged", staged)
        self.assertNotIn("+unstaged", staged)
        self.assertIn("-staged", unstaged)
        self.assertIn("+unstaged", unstaged)

    def test_diff_previews_untracked_text_and_reports_binary_and_truncation(self):
        (self.root / "new.txt").write_text("new content\n")
        result = ui.git_file_diff(self.root, "new.txt", "untracked")
        self.assertIn("+new content", result["diff"])
        (self.root / "image.bin").write_bytes(b"image\0content")
        self.assertTrue(ui.git_file_diff(self.root, "image.bin", "untracked")["binary"])
        (self.root / "large.txt").write_text("x" * (ui.MAX_DIFF_CHARS + 100))
        result = ui.git_file_diff(self.root, "large.txt", "untracked")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"]), ui.MAX_DIFF_CHARS)

    def test_diff_rejects_traversal_unchanged_files_and_external_symlinks(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("private")
        (self.root / "link.txt").symlink_to(outside)
        (self.root / "committed.txt").write_text("unchanged")
        self.git("add", "committed.txt")
        self.git("commit", "-qm", "File")
        for path, kind in [("../secret.txt", "untracked"), (str(outside), "untracked"),
                           ("link.txt", "untracked"), ("committed.txt", "unstaged"),
                           (".git/config", "untracked"), ("committed.txt", "invalid")]:
            with self.subTest(path=path, kind=kind), self.assertRaises(ui.UIError):
                ui.git_file_diff(self.root, path, kind)

    def test_untracked_symlinks_cannot_preview_git_internal_files(self):
        (self.root / ".git/private").write_text("secret Git data")
        (self.root / "preview").symlink_to(".git/private")
        with self.assertRaises(ui.UIError):
            ui.git_file_diff(self.root, "preview", "untracked")

    def test_source_labels_follow_actual_origin_over_stale_project_metadata(self):
        stale = {"git_remote": "https://github.com/old/app.git", "github_repo": "old/app"}
        self.git("remote", "add", "origin", "git@gitlab.com:team/app.git")
        self.assertEqual(ui.detect_project_source(self.root, stale), ("git", "GitLab", None))
        self.git("remote", "set-url", "origin", "git@github.com:new/app.git")
        self.assertEqual(ui.detect_project_source(self.root, stale), ("github", "GitHub Tracked", "new/app"))
        self.git("remote", "remove", "origin")
        self.assertEqual(ui.detect_project_source(self.root, stale), ("local", "Local", None))

    def test_tracked_non_utf8_text_can_be_previewed(self):
        (self.root / "names.csv").write_bytes(b"Andre\n")
        self.git("add", "names.csv")
        self.git("commit", "-qm", "Names")
        (self.root / "names.csv").write_bytes(b"Andr\xe9\n")
        result = ui.git_file_diff(self.root, "names.csv", "unstaged")
        self.assertFalse(result["binary"])
        self.assertIn("-Andre", result["diff"])
        self.assertIn("+Andr", result["diff"])

    def test_repository_link_follows_actual_origin_and_supports_subgroups(self):
        project = ui.runtime_dir(self.root) / "project.json"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(json.dumps({"git_remote": "https://github.com/old/app.git"}))
        self.git("remote", "add", "origin", "ssh://git@gitlab.example.com:2222/team/sub/app.git")
        self.assertEqual(ui.repo_web_url(self.root), "https://gitlab.example.com/team/sub/app")
        self.git("remote", "remove", "origin")
        self.assertIsNone(ui.repo_web_url(self.root))


@unittest.skipUnless(shutil.which("node"), "node is required for Git setup UI tests")
class GitSetupUITests(unittest.TestCase):
    def render(self, **overrides):
        data = {"branch": "main", "upstream": None, "ahead": None, "behind": None,
                "branches": ["main"], "elsewhere": {}, "changes": [], "changes_total": 0,
                "sync_remote": "origin", "sync_ref": "refs/heads/main", "origin": "",
                "mode": "git", "code_host": "auto", "provider": "On this computer", "base_branch": "main",
                "is_repo": True, "has_commits": True, "can_pull": False, "can_push": False,
                "can_fetch": False, "push_reason": "Connect a remote to publish commits.", "pull_reason": "Connect a remote to download changes."}
        from orchestrator import source_control
        data["source_control"] = {"current": source_control.current_key("none", data["origin"], data["code_host"]), "blurb": source_control.BLURB,
                                  "options": source_control.OPTIONS}
        data.update(overrides)
        static = Path(__file__).resolve().parents[1] / "orchestrator/web/static"
        script = r'''
const fs = require('fs');
require(process.argv[2]);
const input = JSON.parse(process.argv[3]);
const pages = {}, state = {you: {role: input.role || 'owner'}, project: {name: 'Example'}};
const esc = Html.escape;
const calls = [], failures = [], handlers = {};
const api = async (path, options) => {calls.push({path, body: options?.body}); return input;};
const $ = selector => ({isConnected: true, addEventListener: (event, handler) => {handlers[selector] = handler;}});
const view = {querySelectorAll: () => []};
const toast = (message, error) => {if (error) failures.push(message);};
let refreshed = 0, routed = 0;
const refreshState = async () => {refreshed++;};
const loadSetup = async () => {};
const route = async () => {routed++;};
const act = action => `data-action="${action}"`;
const branchOption = (b, current) => `<option value="${esc(b)}"${b === current ? ' selected' : ''}>${esc(b)}</option>`;
const moreMenu = () => '';
const ago = () => 'a moment ago';
const source = fs.readFileSync(process.argv[1], 'utf8');
const clampStart = source.indexOf('function clamped(');
eval(source.slice(clampStart, source.indexOf('document.addEventListener', clampStart)));
const start = source.indexOf('pages.git = async');
eval(source.slice(start, source.indexOf('\n};', start) + 3));
pages.git().then(async result => {
  if (input.interaction) {
    result.after();
    const button = () => ({isConnected: true, innerHTML: 'Label', setAttribute() {}, removeAttribute() {}}); // what the page reads from a real button
    await handlers[input.interaction]({currentTarget: {...button(), elements: {branch: {value: input.interactionBranch || 'main'}}}, submitter: button(), preventDefault() {}});
    result.effects = {calls, failures, refreshed, routed};
  }
  console.log(JSON.stringify(result));
});
'''
        result = subprocess.run(["node", "-e", script, str(static / "app.js"), str(static / "html.js"),
                                 json.dumps(data)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_base_branch_is_editable_on_git_setup_without_a_separate_page(self):
        html = self.render(base_branch="main", branches=["main", "develop"])["html"]
        self.assertIn('id="git-base-branch-form"', html)
        self.assertIn('<option value="develop"', html)
        self.assertNotIn('href="#/config/base-branch"', html)

    def test_base_branch_form_saves_to_the_existing_project_setting(self):
        result = self.render(interaction="#git-base-branch-form", interactionBranch="develop", branches=["main", "develop"])
        self.assertIn({"path": "config/base-branch", "body": {"branch": "develop"}}, result["effects"]["calls"])
        self.assertEqual(result["effects"]["routed"], 1)

    def test_with_no_source_control_chosen_the_page_asks_and_says_why_it_matters(self):
        html = self.render()["html"]
        self.assertIn('id="git-source-control"', html)
        self.assertIn("Source control keeps a history of every change", html)  # the one-line reason, always shown
        self.assertIn("Where will your code be kept?", html)
        self.assertIn('<option value="" selected>Choose one…</option>', html)
        self.assertIn("Not chosen yet", html)
        for label in ("GitHub", "GitLab", "Bitbucket", "Azure DevOps", "Another Git host", "On this computer only"):
            self.assertIn(f">{label}</option>", html)
        self.assertIn("Local job records", html)  # the connection card still says how jobs work

    def test_the_page_has_no_provider_subtitle_because_the_dropdown_shows_it(self):
        connected = self.render(origin="git@github.com:team/app.git", provider="GitHub", mode="github", source_control={
            "current": "github", "blurb": "Why.", "options": [{"key": "github", "label": "GitHub"}]})
        self.assertEqual(connected["sub"], "")  # not "GitHub · GitHub workflow"
        self.assertIn('<option value="github" selected>GitHub (current)</option>', connected["html"])
        self.assertEqual(self.render(has_commits=False)["sub"], "No commits yet")  # a repository that isn't ready still says so
        self.assertEqual(self.render(is_repo=False, has_commits=False)["sub"], "Not initialized")

    def test_a_connected_project_shows_its_choice_as_current_and_offers_to_change_it(self):
        html = self.render(origin="git@gitlab.com:team/app.git", provider="GitLab", source_control={
            "current": "gitlab", "blurb": "Why.", "options": [{"key": "gitlab", "label": "GitLab"}, {"key": "github", "label": "GitHub"}]})["html"]
        self.assertIn("Change where your code is kept", html)
        self.assertIn('<option value="gitlab" selected>GitLab (current)</option>', html)
        self.assertNotIn("Choose one…", html)
        self.assertIn('<span class="pill done">GitLab</span>', html)

    def test_other_hosts_show_their_provider_and_workflow(self):
        html = self.render(origin="git@gitlab.com:group/app.git", provider="GitLab")["html"]
        self.assertIn("GitLab", html)
        self.assertIn("Gathering Git and host access", html)
        self.assertNotIn("Sign in to GitHub", html)
        self.assertIn("merged on this computer", html)

    def test_members_cannot_change_connection_settings(self):
        html = self.render(role="member")["html"]
        self.assertNotIn('id="git-connect"', html)
        self.assertNotIn('id="git-create"', html)

    def test_unavailable_sync_actions_have_visible_reasons(self):
        html = self.render()["html"]
        self.assertRegex(html, r'id="git-push"[^>]*disabled')
        self.assertRegex(html, r'id="git-pull"[^>]*disabled')
        self.assertIn("Connect a remote to publish commits.", html)
        self.assertIn("Connect a remote to download changes.", html)

    def test_uninitialized_folder_offers_initialization_instead_of_detached_head(self):
        page = self.render(is_repo=False, has_commits=False, branch="", branches=[])
        self.assertIn('id="git-init"', page["html"])
        self.assertIn("Not initialized", page["sub"])
        self.assertNotIn("detached", page["sub"].lower())
        self.assertNotIn("Branch and sync", page["html"])
        self.assertNotIn("Uncommitted changes", page["html"])

    def test_tracking_branch_can_be_selected_after_fetch(self):
        html = self.render(origin="git@gitlab.com:team/app.git", remote_branches=["origin/main", "origin/develop"])["html"]
        self.assertIn('id="git-tracking"', html)
        self.assertIn("Choose tracking branch", html)

    def test_fetch_shows_freshness_and_keeps_cached_counts_explicit(self):
        page = self.render(can_fetch=True, last_fetch=1234567890, upstream="origin/main", ahead=2, behind=1)
        self.assertIn('id="git-fetch"', page["html"])
        self.assertIn("Last fetched", page["html"])
        self.assertIn("cached", page["html"])

    def test_changes_have_readable_groups_expandable_diffs_and_escaped_names(self):
        page = self.render(changes_total=1, change_groups=[{"id": "unstaged", "label": "Unstaged", "changes": [
            {"path": "<script>.txt", "old_path": "", "label": "Modified"}]}])
        html = page["html"]
        self.assertIn("Unstaged", html)
        self.assertIn("Modified", html)
        self.assertIn("data-git-diff", html)
        self.assertIn("&lt;script&gt;.txt", html)
        self.assertNotIn("<script>.txt", html)
        self.assertLess(html.index('data-action="git_new_branch"'), html.index("Uncommitted changes"))

    def test_project_selector_groups_other_hosts_separately_from_local_projects(self):
        static = Path(__file__).resolve().parents[1] / "orchestrator/web/static"
        script = r'''
const fs = require('fs');
require(process.argv[2]);
const esc = Html.escape;
const state = {you: {role: 'member'}, project: {root: '/github', recent: [
 {name: 'Hub', root: '/github', source_type: 'github'},
 {name: 'Lab', root: '/gitlab', source_type: 'git', source_label: 'GitLab'},
 {name: 'Offline', root: '/local', source_type: 'local'}]}};
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderProjectSelect(');
eval(source.slice(start, source.indexOf('\ndocument.addEventListener', start)));
const select = {dataset: {}};
renderProjectSelect(select);
console.log(JSON.stringify(select.innerHTML));
'''
        result = subprocess.run(["node", "-e", script, str(static / "app.js"), str(static / "html.js")],
                                capture_output=True, text=True, check=True)
        html = json.loads(result.stdout)
        self.assertRegex(html, r'<optgroup label="Other Git hosts">[^<]*(?:<option[^>]*>[^<]*</option>)*</optgroup>')
        self.assertIn('value="/gitlab"', html.split('<optgroup label="Other Git hosts">')[1].split('</optgroup>')[0])
        self.assertIn('value="/local"', html.split('<optgroup label="On this computer">')[1].split('</optgroup>')[0])

    def test_successful_fetch_refreshes_state_without_showing_an_error(self):
        result = self.render(can_fetch=True, interaction="#git-fetch")
        self.assertEqual(result["effects"]["failures"], [])
        self.assertEqual(result["effects"]["refreshed"], 1)
        self.assertEqual(result["effects"]["routed"], 1)
        self.assertEqual(result["effects"]["calls"][-1], {"path": "git/fetch", "body": {}})

    def test_connected_repository_prioritizes_work_and_folds_setup_settings(self):
        page = self.render(origin="git@gitlab.com:team/app.git", provider="GitLab", can_push=True)
        html = page["html"]
        self.assertLess(html.index("Connection"), html.index("Job integration"))
        self.assertLess(html.index("Job integration"), html.index("Advanced Git tools"))
        self.assertRegex(html, r'<details class="git-advanced-tools">')
        self.assertIn('<label class="field" for="git-branch-select">', html)

    def test_local_repository_keeps_connection_choices_expanded(self):
        html = self.render()["html"]
        self.assertIn('id="git-connection-card"', html)
        self.assertIn('id="sc-select"', html)  # the choices are the dropdown, not a list of rows
        self.assertNotIn("Where to keep your code", html)

    def test_a_shared_sync_blocker_is_explained_once(self):
        page = self.render(push_reason="Connect a remote before syncing.", pull_reason="Connect a remote before syncing.")
        self.assertEqual(page["html"].count("Connect a remote before syncing."), 1)
        self.assertNotIn("Connect a remote before syncing.", page["sub"])

    def test_non_origin_remotes_still_offer_tracking_selection(self):
        html = self.render(can_fetch=True, remote_branches=["team/review"])["html"]
        self.assertIn('id="git-tracking"', html)
        self.assertNotIn("No remote connected", html)

    def test_setup_does_not_add_a_second_primary_when_sync_is_available(self):
        html = self.render(can_push=True, can_pull=True, can_fetch=True, upstream="team/review", remote_branches=["team/review"])["html"]
        self.assertEqual(html.count('class="btn small primary"'), 0)  # no primary on the page itself; the chooser draws its own only once an option is picked

    def test_long_commit_subject_can_expand_instead_of_filling_the_screen(self):
        html = self.render(last_commit="abc123 " + "X" * 200)["html"]
        self.assertIn("data-clamp-toggle", html)
        self.assertIn("data-clamp-full hidden", html)

    def test_normal_view_leads_with_connection_and_job_integration(self):
        html = self.render(origin="git@gitlab.com:team/app.git", provider="GitLab")["html"]
        self.assertLess(html.index("Connection"), html.index("Job integration"))
        self.assertLess(html.index("Job integration"), html.index("Advanced Git tools"))
        self.assertRegex(html, r'<details class="git-advanced-tools">')
        self.assertGreater(html.index('id="git-push"'), html.index("Advanced Git tools"))

    def test_github_label_problems_offer_missing_only_repair_and_host_links(self):
        html = self.render(origin="git@github.com:team/app.git", web_url="https://github.com/team/app", mode="github", provider="GitHub",
                           github={"installed": True, "user": "alice"}, health={"checked": {"checked_at": 123, "checks": [
                               {"id": "read", "label": "Repository read access", "status": "ok", "detail": "Remote branches can be read."},
                               {"id": "publish", "label": "Code publishing", "status": "unknown", "detail": "Write access is checked when publishing."}],
                               "labels": [{"name": "job:bug", "description": "Bug fix", "present": False}]},
                           "configuration": {}, "publishing": {"problems": [], "recent": []}, "links": [], "labels_url": "https://github.com/login?return_to=%2Fteam%2Fapp%2Flabels"})["html"]
        self.assertIn('id="git-repair-labels"', html)
        self.assertIn("job:bug", html)
        self.assertIn("Not verified", html)
        self.assertIn('href="https://github.com/login?return_to=%2Fteam%2Fapp%2Flabels"', html)
        self.assertIn("Needs attention", html)

    def test_plain_git_does_not_offer_github_label_management(self):
        html = self.render(origin="git@gitlab.com:team/app.git", provider="GitLab")["html"]
        self.assertNotIn('id="git-repair-labels"', html)
        self.assertNotIn("Managed job labels", html)


class GitSetupApiTests(web_tests.ServerTestCase):
    sign_in = web_tests.SignInTests.sign_in
    as_user = web_tests.SignInTests.as_user

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".orchestrator/\n")
        (self.root / "app.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        self.remote = Path(self.tmp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(self.remote))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True, text=True).stdout.strip()

    def test_owner_can_connect_fetch_and_select_tracking_through_the_api(self):
        response, data = self.request("POST", "/api/git/connection", body={"op": "connect", "url": str(self.remote), "mode": "git"}, headers=web_tests.UI_HEADERS)
        self.assertEqual(response.status, 200, data)
        self.git("push", "-q", "origin", "main:review")
        response, data = self.request("POST", "/api/git/fetch", body={}, headers=web_tests.UI_HEADERS)
        self.assertEqual(response.status, 200, data)
        response, data = self.request("POST", "/api/git/tracking", body={"upstream": "origin/review"}, headers=web_tests.UI_HEADERS)
        self.assertEqual(response.status, 200, data)
        response, data = self.request("GET", "/api/git")
        self.assertEqual(response.status, 200)
        self.assertEqual(data["upstream"], "origin/review")
        self.assertTrue(data["can_pull"])
        self.assertTrue(data["last_fetch"])

    def wait_task(self, task):
        for _ in range(100):
            _, data = self.request("GET", f"/api/product/task/{task}")
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail("the task never finished")

    def test_the_page_state_says_which_source_control_is_in_use_and_lists_the_options(self):
        _, data = self.request("GET", "/api/git")
        self.assertEqual(data["source_control"]["current"], "")  # a repository with no remote and no choice made
        self.assertEqual([o["key"] for o in data["source_control"]["options"]], ["github", "gitlab", "bitbucket", "azure", "other", "local"])
        self.assertTrue(data["source_control"]["blurb"].startswith("Source control keeps a history"))
        self.request("POST", "/api/git/connection", body={"op": "workflow", "mode": "git"}, headers=web_tests.UI_HEADERS)
        self.assertEqual(self.request("GET", "/api/git")[1]["source_control"]["current"], "local")
        self.request("POST", "/api/git/connection", body={"op": "connect", "url": "git@gitlab.com:team/app.git", "mode": "git"}, headers=web_tests.UI_HEADERS)
        self.assertEqual(self.request("GET", "/api/git")[1]["source_control"]["current"], "gitlab")
        self.git("remote", "set-url", "origin", "git@git.example.com:team/app.git")
        self.assertEqual(self.request("GET", "/api/git")[1]["source_control"]["current"], "other")

    def test_optional_ai_help_writes_steps_for_the_chosen_option(self):
        prompts = []

        def fake(handler, root, prompt, model="", timeout=150):
            prompts.append(prompt)
            return "You have a repository with commits and no remote.\n1. Press **Sign in to GitHub**."

        with patch.object(web_tests.ui.UIHandler, "_model_call", fake):
            res, started = self.request("POST", "/api/git/setup-help", body={"option": "github"}, headers=web_tests.UI_HEADERS)
            self.assertEqual(res.status, 202, started)
            task = self.wait_task(started["task"])
        self.assertEqual((task["status"], task["result"]["option"]), ("done", "github"))
        self.assertIn("Sign in to GitHub", task["result"]["text"])
        self.assertIn("chose: GitHub", prompts[0])
        self.assertIn("is a Git repository, with commits", prompts[0])  # where this project actually is
        self.assertIn("Remote ('origin'): none", prompts[0])

    def test_ai_help_needs_a_known_option_and_the_page_header(self):
        self.assertEqual(self.request("POST", "/api/git/setup-help", body={"option": "svn"}, headers=web_tests.UI_HEADERS)[0].status, 400)
        self.assertEqual(self.request("POST", "/api/git/setup-help", body={})[0].status, 403)  # another site can't start a model run
        with patch.object(web_tests.ui.UIHandler, "_model_call", lambda *a, **k: "   "):
            _, started = self.request("POST", "/api/git/setup-help", body={"option": "local"}, headers=web_tests.UI_HEADERS)
            task = self.wait_task(started["task"])
        self.assertEqual(task["status"], "error")
        self.assertIn("didn't write any steps", task["error"])

    def test_repository_health_is_gathered_in_the_background_on_get(self):
        response, data = self.request("GET", "/api/git")
        self.assertEqual(response.status, 200)
        self.assertIn("health", data)
        self.assertIsNone(data["health"]["checked"])
        self.assertTrue(data["health"]["checking_task"])
        self.assertIn("configuration", data["health"])

    def test_repository_checks_run_as_a_background_task(self):
        self.git("remote", "add", "origin", str(self.remote))
        response, data = self.request("POST", "/api/git/check", body={}, headers=web_tests.UI_HEADERS)
        self.assertEqual(response.status, 202, data)
        import time
        for _ in range(50):
            _, result = self.request("GET", "/api/product/task/" + data["task"])
            if result["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(result["status"], "done", result)
        self.assertTrue(result["result"]["checked_at"])

    def test_members_cannot_repair_repository_labels(self):
        token = self.sign_in(source="settings")
        response, data = self.as_user(token, "POST", "/api/git/labels/repair", body={}, sources={"tester@example.com": "settings"})
        self.assertEqual(response.status, 403, data)

    def test_member_cannot_change_connections_or_tracking_but_can_inspect_diffs(self):
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}
        for route, body in [("connection", {"op": "connect", "url": str(self.remote), "mode": "git"}),
                            ("tracking", {"upstream": "origin/main"})]:
            response, data = self.as_user(token, "POST", "/api/git/" + route, body=body, sources=member)
            self.assertEqual(response.status, 403, data)
        self.assertEqual(self.git("remote"), "")
        (self.root / "app.txt").write_text("modified\n")
        response, data = self.as_user(token, "GET", "/api/git/diff?path=app.txt&kind=unstaged", sources=member)
        self.assertEqual(response.status, 200, data)
        self.assertIn("+modified", data["diff"])
        response, data = self.as_user(token, "GET", "/api/git/diff?path=../secret&kind=untracked", sources=member)
        self.assertEqual(response.status, 404, data)
