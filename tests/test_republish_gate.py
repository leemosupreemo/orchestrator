"""Republishing the hosted web app is checked first: committed static files, and no failing test that isn't already known.

Before this the deploy was a bare `firebase deploy` and shipped with ten tests failing, and it uploads whatever is on disk, so it
could carry another session's unfinished edits. The CLI tests run the real script against a throwaway repo with a fake `firebase`."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from orchestrator import republish_gate as gate  # noqa: E402
import republish  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
class BaselineTests(unittest.TestCase):
    def test_the_file_is_read_with_reasons_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known.txt"
            path.write_text("# a note\n\ntest_a.A.test_one  # because\n  tests.test_b.B.test_two\n   # indented note\n")
            self.assertEqual(gate.read_baseline(path), {"test_a.A.test_one": "because", "test_b.B.test_two": ""})
            self.assertEqual(gate.read_baseline(Path(tmp) / "missing.txt"), {})

    def test_the_checked_in_baseline_lists_real_tests_in_the_canonical_form(self):
        known = gate.read_baseline(ROOT / gate.BASELINE_FILE)
        for test_id in known:  # empty is fine: nothing is allowed to fail
            self.assertEqual(len(test_id.split(".")), 3, test_id)        # module.Class.test
            self.assertFalse(test_id.startswith("tests."), test_id)
            self.assertTrue((ROOT / "tests" / f"{test_id.split('.')[0]}.py").is_file(), test_id)


class ReadingTheSuiteTests(unittest.TestCase):
    def test_a_new_failure_blocks_and_known_ones_do_not(self):
        known = {"test_worker_run.WorkerRunTests.test_single_builder": "", "test_web_ui.AccessibilityStaticTests.test_design_tokens_hold": "",
                 "test_gone.G.test_now_passes": ""}
        failing = ["test_worker_run.WorkerRunTests.test_single_builder", "test_web_ui.AccessibilityStaticTests.test_design_tokens_hold",
                   "test_x.NewTests.test_brand_new"]
        verdict = gate.judge(failing, known, returncode=1, ran=3972)
        self.assertEqual(verdict.new, ["test_x.NewTests.test_brand_new"])
        self.assertEqual(len(verdict.known), 2)
        self.assertEqual(verdict.fixed, ["test_gone.G.test_now_passes"])
        self.assertFalse(verdict.ok)
        self.assertTrue(gate.judge(["test_a.A.t"], {"test_a.A.t": ""}, returncode=1, ran=10).ok)
        self.assertTrue(gate.judge([], {}, returncode=0, ran=10).ok)

    def run_in(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("")
            for name, body in files.items():
                (root / "tests" / name).write_text(body)
            return gate.run_suite(root)

    def test_what_ran_and_what_failed_is_read_from_the_runner_not_from_text(self):
        # A test that itself prints a unittest-looking report must not change the count or add failures.
        noisy = ("import unittest\nclass A(unittest.TestCase):\n    def test_prints(self):\n"
                 "        print('FAIL: test_fake (nested.A.test_fake)')\n        print('Ran 1 test in 0.001s')\n"
                 "    def test_boom(self):\n        raise RuntimeError('x')\n"
                 "    def test_subtests(self):\n        for i in range(2):\n            with self.subTest(i=i):\n                self.fail('no')\n"
                 "    def test_fine(self):\n        pass\n")
        code, result = self.run_in({"test_a.py": noisy})
        self.assertEqual(code, 1)
        self.assertEqual(result["ran"], 4)
        self.assertEqual(sorted(result["failing"]), ["test_a.A.test_boom", "test_a.A.test_subtests"])

    def test_a_passing_suite_and_one_that_cannot_load_are_told_apart(self):
        code, result = self.run_in({"test_ok.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"})
        self.assertEqual((code, result), (0, {"ran": 1, "failing": []}))
        code, result = self.run_in({"test_bad.py": "import not_a_real_module_anywhere\n"})
        self.assertEqual(code, 1)
        self.assertEqual(result["failing"], ["unittest.loader._FailedTest.test_bad"])

    def test_no_tests_and_a_runner_that_dies_both_leave_nothing_to_trust(self):
        with tempfile.TemporaryDirectory() as tmp:  # no tests folder at all
            code, result = gate.run_suite(Path(tmp))
        self.assertEqual(result, {"ran": 0, "failing": []})
        self.assertIn("No tests ran", gate.judge(result["failing"], {}, returncode=code, ran=result["ran"]).problem)
        died = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback: the runner crashed")  # never wrote its result
        code, result = gate.run_suite(Path("."), run=lambda *a, **k: died)
        self.assertEqual((code, result), (1, {"ran": 0, "failing": []}))

    def test_a_run_that_did_not_really_run_blocks(self):
        self.assertIn("No tests ran", gate.judge([], {}, returncode=0, ran=0).problem)
        crashed = gate.judge([], {}, returncode=1, ran=5)
        self.assertIn("without naming a failing test", crashed.problem)
        self.assertFalse(crashed.ok)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout


class Throwaway:
    """A repo shaped like this one (static folder, tests, .firebaserc) plus a fake `firebase` that records how it was called."""

    def __init__(self, passing=True, baseline=""):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "repo"
        (self.root / gate.STATIC_DIR).mkdir(parents=True)
        (self.root / gate.STATIC_DIR / "app.js").write_text("const isBackgroundRun = 1;\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")  # as in this repo: a regular package, so another `tests` on the machine can't shadow it
        body = "pass" if passing else "self.fail('broken')"
        (self.root / "tests" / "test_one.py").write_text(f"import unittest\nclass Ok(unittest.TestCase):\n    def test_a(self):\n        {body}\n")
        (self.root / gate.BASELINE_FILE).write_text(baseline)
        (self.root / ".firebaserc").write_text('{"projects": {"default": "demo-project"}}')
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "start")
        self.bin = Path(self.dir.name) / "bin"
        self.bin.mkdir()
        self.calls = Path(self.dir.name) / "firebase.calls"
        firebase = self.bin / "firebase"
        firebase.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\nexit ${{FAKE_FIREBASE_EXIT:-0}}\n')
        firebase.chmod(0o755)

    def run(self, *argv, fetch=None, env=None):
        out = []
        with patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", **(env or {})}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = republish.main(list(argv), root=self.root, fetch=fetch, out=out.append)
        return code, "\n".join(out)

    @property
    def deployed(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


class RepublishScriptTests(unittest.TestCase):
    def test_a_clean_tree_and_a_passing_suite_deploys_hosting_only(self):
        with Throwaway() as repo:
            code, text = repo.run()
            self.assertEqual(code, 0, text)
            self.assertEqual(repo.deployed, ["deploy --only hosting --non-interactive"])
            self.assertIn("Deployed to https://demo-project.web.app", text)
            self.assertTrue(list((repo.root / ".orchestrator/output/republish").glob("*.log")))  # the full output is kept

    def test_a_new_failing_test_blocks_the_deploy_and_names_it(self):
        with Throwaway(passing=False) as repo:
            code, text = repo.run()
            self.assertEqual((code, repo.deployed), (1, []))
            self.assertIn("new failure: test_one.Ok.test_a", text)
            self.assertIn("not republished", text)

    def test_a_known_failure_does_not_block_and_a_fixed_one_is_reported(self):
        with Throwaway(passing=False, baseline="test_one.Ok.test_a  # known\ntest_old.Gone.test_x  # was broken\n") as repo:
            code, text = repo.run()
            self.assertEqual((code, len(repo.deployed)), (0, 1), text)
            self.assertIn("1 already known, 0 new", text)
            self.assertIn("test_old.Gone.test_x", text)  # passes now: its line can go
            self.assertIn("delete their lines", text)

    def test_uncommitted_static_files_block_unless_allowed(self):
        with Throwaway() as repo:
            (repo.root / gate.STATIC_DIR / "app.js").write_text("const another = 'sessions edit';\n")
            (repo.root / gate.STATIC_DIR / "new.js").write_text("x\n")
            code, text = repo.run()
            self.assertEqual((code, repo.deployed), (1, []))
            self.assertIn("2 uncommitted change(s)", text)
            self.assertIn("app.js", text)
            code, text = repo.run("--allow-dirty")
            self.assertEqual((code, len(repo.deployed)), (0, 1))
            self.assertIn("deploying 2 uncommitted", text)

    def test_changes_outside_the_shipped_folder_do_not_matter(self):
        with Throwaway() as repo:
            (repo.root / "README.md").write_text("notes\n")
            self.assertEqual(repo.run()[0], 0)

    def test_dry_run_checks_but_never_deploys(self):
        with Throwaway() as repo:
            code, text = repo.run("--dry-run")
            self.assertEqual((code, repo.deployed), (0, []))
            self.assertIn("nothing was deployed", text)
        with Throwaway(passing=False) as repo:
            self.assertEqual(repo.run("--dry-run")[0], 1)

    def test_skipping_the_tests_says_so_loudly_and_still_checks_the_tree(self):
        with Throwaway(passing=False) as repo:
            code, text = repo.run("--skip-tests")
            self.assertEqual((code, len(repo.deployed)), (0, 1))
            self.assertIn("SKIPPING THE TEST GATE", text)
            (repo.root / gate.STATIC_DIR / "app.js").write_text("changed\n")
            self.assertEqual(repo.run("--skip-tests")[0], 1)

    def test_only_runs_the_named_modules(self):
        with Throwaway(passing=False) as repo:
            (repo.root / "tests" / "test_two.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_b(self):\n        pass\n")
            code, text = repo.run("--only", "tests.test_two")
            self.assertEqual((code, len(repo.deployed)), (0, 1), text)
            self.assertIn("1 tests ran", text)
            self.assertIn("selected test suite", text)
            code, text = repo.run("--only", "tests.test_missing")  # a module that can't be loaded is a failure, not a pass
            self.assertEqual((code, len(repo.deployed)), (1, 1), text)

    def test_a_failing_deploy_is_reported_not_hidden(self):
        with Throwaway() as repo:
            code, text = repo.run(env={"FAKE_FIREBASE_EXIT": "3"})
            self.assertEqual(code, 2)
            self.assertIn("deploy failed (exit 3)", text)

    def test_verify_checks_the_live_app_for_the_new_symbol(self):
        with Throwaway() as repo:
            seen = []
            code, text = repo.run("--verify", "isBackgroundRun", fetch=lambda url: (seen.append(url), "a isBackgroundRun b isBackgroundRun")[1])
            self.assertEqual(code, 0)
            self.assertEqual(seen, ["https://demo-project.web.app/app.js"])
            self.assertIn("contains 'isBackgroundRun' 2 time(s)", text)
        with Throwaway() as repo:
            code, text = repo.run("--verify", "missingSymbol", fetch=lambda url: "old app")
            self.assertEqual(code, 2)
            self.assertIn("isn't there", text)

    def test_without_firebase_installed_it_says_so(self):
        with Throwaway() as repo:
            (repo.bin / "firebase").unlink()
            with patch.object(republish.shutil, "which", return_value=None):
                code, text = repo.run()
            self.assertEqual(code, 2)
            self.assertIn("isn't installed", text)


if __name__ == "__main__":
    unittest.main()
