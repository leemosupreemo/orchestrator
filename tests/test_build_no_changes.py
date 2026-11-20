"""The build step only counts as done when files changed.

A quick change once showed Build ✓ "No tasks needed" although the builder's file edits had all been refused (Claude Code in
print mode, with no permission mode), then went on to "verify" untouched code and loop on fix attempts. Now the Claude builder
and debugger may edit files, a build that changed nothing stops the job with the reason, and the job page says so."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator/scripts"))
import worker_run as worker  # noqa: E402
from common import dirty_file_hashes, files_changed_between, project_config_hash  # noqa: E402
from llm import get_llm_command  # noqa: E402
from model_router import ModelRole  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402

from tests import test_worker_run as harness  # noqa: E402  (its pipeline harness, not its tests)

NOTHING = {"modified": [], "untracked": []}
GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}


class ClaudeEditPermissionTests(unittest.TestCase):
    def test_only_the_builder_and_debugger_may_edit_files(self):
        for model in ("claude-sonnet-4-6", "claude"):
            for role in (ModelRole.BUILDER, ModelRole.DEBUGGER):
                self.assertIn("--permission-mode acceptEdits", get_llm_command(model, "/tmp/p", role=role), (model, role))
            for role in (ModelRole.PLANNER, ModelRole.VERIFIER, None):
                self.assertNotIn("--permission-mode", get_llm_command(model, "/tmp/p", role=role), (model, role))

    def test_it_still_combines_with_web_search_and_sessions(self):
        cmd = get_llm_command("claude-sonnet-4-6", "/tmp/p", role=ModelRole.BUILDER, session_id="abc", web=True)
        self.assertIn("--permission-mode acceptEdits", cmd)
        self.assertIn("--session-id abc", cmd)
        self.assertIn('--allowedTools "WebSearch,WebFetch"', cmd)


class NoChangeRuleTests(unittest.TestCase):
    def test_a_build_that_changed_files_carries_on(self):
        self.assertEqual(worker.no_change_stop({"type": "quick-fix"}, ["a.py"]), "")

    def test_a_job_that_never_changed_anything_stops(self):
        said = worker.no_change_stop({"type": "quick-fix", "builder_summary": "Implemented it."}, [])
        self.assertIn("The build finished without changing any files, so nothing was built.", said)
        self.assertIn("If the change was already in place", said)

    def test_a_blocked_edit_names_the_tool_permission(self):
        said = worker.no_change_stop({"type": "quick-fix", "builder_summary": "All five writes were blocked by the session's permission mode"}, [])
        self.assertIn("check that its tool may edit files", said)

    def test_earlier_work_counts_unless_this_was_a_fix_attempt(self):
        earlier = {"type": "bug-fix", "ai_modified_files": ["a.py"]}
        self.assertEqual(worker.no_change_stop(earlier, []), "")
        self.assertIn("The fix attempt finished without changing any files", worker.no_change_stop(earlier, [], debug_attempt=True))
        self.assertEqual(worker.no_change_stop({"type": "feature-plan", "completed_task_indices": [0]}, []), "")

    def test_an_investigation_may_change_nothing(self):
        self.assertEqual(worker.no_change_stop({"type": "bug-investigate"}, []), "")


class SnapshotTests(unittest.TestCase):
    def test_edits_to_files_that_were_already_uncommitted_are_seen(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GIT_ENV):
            root = Path(tmp)
            git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            (root / "a.txt").write_text("one\n")
            (root / "b.txt").write_text("one\n")
            git("add", ".")
            git("commit", "-q", "-m", "start")
            (root / "a.txt").write_text("dirty before the builder\n")  # git status says "modified" both before and after
            before = dirty_file_hashes(root)
            self.assertEqual(files_changed_between(before, dirty_file_hashes(root)), [])
            (root / "a.txt").write_text("changed by the builder\n")
            (root / "dir").mkdir()
            (root / "dir" / "new.txt").write_text("new\n")
            (root / "b.txt").unlink()
            self.assertEqual(files_changed_between(before, dirty_file_hashes(root)), ["a.txt", "b.txt", "dir/new.txt"])
            self.assertEqual(dirty_file_hashes(root / "missing"), {})


    def test_moving_a_submodule_pointer_is_seen_even_when_it_was_already_dirty(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GIT_ENV):
            base = Path(tmp)
            sub, root = base / "sub", base / "root"
            for d in (sub, root):
                d.mkdir()
                subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True)
            git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
            shas = []
            for n in ("one", "two"):
                (sub / "f.txt").write_text(n)
                git(sub, "add", ".")
                git(sub, "commit", "-q", "-m", n)
                shas.append(git(sub, "rev-parse", "HEAD"))
            (root / "a.txt").write_text("a")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "start")
            git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "core")
            git(root, "commit", "-q", "-m", "add core")
            git(root / "core", "checkout", "-q", shas[0])  # the pointer is already dirty before the builder runs
            before = dirty_file_hashes(root)
            self.assertEqual(files_changed_between(before, dirty_file_hashes(root)), [])
            git(root, "update-index", "--cacheinfo", f"160000,{shas[0]},core")  # a staged pointer move, as the builder did
            self.assertEqual(files_changed_between(before, dirty_file_hashes(root)), ["core"])

    def test_an_untracked_folder_is_still_a_plain_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GIT_ENV):
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            (root / "empty").mkdir()
            (root / "empty" / ".keep").write_text("")
            self.assertEqual(set(dirty_file_hashes(root).values()), {hashlib_sha("")})

    def test_the_project_config_hash_follows_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(project_config_hash(root), "")
            (root / ".orchestrator").mkdir()
            (root / ".orchestrator" / "project.json").write_text('{"test_command": "a"}')
            first = project_config_hash(root)
            (root / ".orchestrator" / "project.json").write_text('{"test_command": "b"}')
            self.assertNotEqual(first, project_config_hash(root))


def hashlib_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


class WorkerStopTests(unittest.TestCase):
    """The real worker, with the builder and git faked by the shared harness."""
    setUp = harness.WorkerRunTests.setUp
    pipeline = harness.WorkerRunTests.pipeline

    def test_a_build_with_no_changes_stops_instead_of_verifying_or_fixing(self):
        with self.pipeline({"type": "quick-fix"}, states=[NOTHING] * 4,
                           results=[(True, False, Path("summary"), {"summary": "All writes were blocked by the permission mode"})]) as (path, calls):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job["status"], "human-needed")
            self.assertTrue(job["build_no_changes"])
            self.assertIn("nothing was built", job["last_error"])
            self.assertIn("may edit files", job["last_error"])
            calls["debug"].assert_not_called()
            self.assertFalse(any("git commit" in c.args[0] for c in calls["run_shell"].call_args_list))
            self.assertEqual(calls["send_notifications"].call_args.args[1], "Job Paused: nothing was built")

    def test_editing_an_already_uncommitted_file_counts_as_building(self):
        hashes = iter([{"a.py": "old"}, {"a.py": "new"}])
        with self.pipeline({"type": "quick-fix"}, states=[{"modified": ["a.py"], "untracked": []}] * 4) as (path, calls), \
             patch.object(worker, "dirty_file_hashes", side_effect=lambda root=None: next(hashes)):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job["status"], "review-needed")
            self.assertNotIn("build_no_changes", job)

    def test_editing_only_the_project_config_counts_as_building(self):
        configs = iter(["old", "new"])
        with self.pipeline({"type": "quick-fix"}, states=[NOTHING] * 4) as (path, calls), \
             patch.object(worker, "project_config_hash", side_effect=lambda root=None: next(configs)):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job["status"], "review-needed")
            self.assertNotIn("build_no_changes", job)

    def test_a_later_build_that_changes_files_clears_the_flag(self):
        with self.pipeline({"type": "quick-fix", "build_no_changes": True}) as (path, calls):
            worker.execute_job(path)
            job = worker.read_json(path)
            self.assertEqual(job["status"], "review-needed")
            self.assertNotIn("build_no_changes", job)

    def test_a_fix_attempt_that_changes_nothing_stops_the_loop(self):
        job = {"type": "bug-fix", "ai_modified_files": ["a.py"], "debug_history": [{"iteration": 1, "result": "pending"}]}
        with self.pipeline(job, states=[NOTHING] * 4, results=[(True, False, Path("summary"), {})]) as (path, calls), \
             patch.dict(os.environ, {"AI_DEBUG_CHILD": "1"}):
            worker.execute_job(path)
            saved = worker.read_json(path)
            self.assertEqual(saved["status"], "human-needed")
            self.assertIn("The fix attempt finished without changing any files", saved["last_error"])
            self.assertEqual(saved["debug_history"][-1]["result"]["notes"], "The fix attempt changed no files")


class JobPageTests(unittest.TestCase):
    def test_the_server_says_nothing_was_built_and_offers_to_run_the_build_again(self):
        state = ui.job_state({"status": "human-needed", "build_no_changes": True, "last_error": "The build finished without changing any files."})
        self.assertEqual((state["label"], state["tone"], state["next"]["action"], state["next"]["label"]),
                         ("Nothing was built", "failed", "resume", "Run the build again"))

    def test_the_timeline_marks_build_failed_when_no_files_changed(self):
        app = (ROOT / "orchestrator/web/static/app.js").read_text(encoding="utf-8")
        self.assertIn('const nothingBuilt = codeJob && status !== "completed"', app)
        self.assertIn('if (nothingBuilt && idx === 1) return { state: "active failed", icon: "!" };', app)
        self.assertIn('nothingBuilt ? "No files changed"', app)
        self.assertIn('`${plural(filesChanged, "file")} changed`', app)  # a quick change that built something says how much
        self.assertIn('nothingBuilt ? "Waits for the build"', app)


if __name__ == "__main__":
    unittest.main()
