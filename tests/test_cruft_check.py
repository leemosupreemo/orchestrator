from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cruft_check as C  # noqa: E402

DIFF = """diff --git a/src/score.py b/src/score.py
--- a/src/score.py
+++ b/src/score.py
@@ -10,0 +11,3 @@ def old():
+def rank_players(scores):
+    return sorted(scores)
+
@@ -40 +44 @@ def other():
-    x = 1
+    x = 2
diff --git a/src/gone.py b/src/gone.py
--- a/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def removed_helper():
-    pass
"""


def added(path, *texts, start=1):
    return {path: [(start + i, t) for i, t in enumerate(texts)]}


def run(add, elsewhere=None, mentions=lambda n: 5, accepted=None):
    return C.evaluate(add, lambda n: (elsewhere or {}).get(n, []), mentions, accepted)


def kinds(result):
    return [f["kind"] for f in result["findings"]]


class ParseTests(unittest.TestCase):
    def test_added_lines_get_their_new_line_numbers_and_deleted_files_add_nothing(self):
        got = C.parse_added(DIFF)
        self.assertEqual(got, {"src/score.py": [(11, "def rank_players(scores):"), (12, "    return sorted(scores)"), (13, ""), (44, "    x = 2")]})

    def test_empty_diff(self):
        self.assertEqual(C.parse_added(""), {})


class ExistingSourceTests(unittest.TestCase):
    def test_only_files_in_test_folders_are_tests_not_modules_named_like_one(self):
        for path in ("orchestrator/test_probe.py", "src/test_results.ts", "lib/helpers.py", "app/Models.swift"):
            self.assertTrue(C.existing_source(path), path)
            self.assertEqual(C.reviewable(path), not path.split("/")[-1].startswith("test_"), path)  # judged as a change, it is a test
        for path in ("tests/test_a.py", "web/__tests__/a.ts", "Sources/AppTests/A.swift", "spec/a.rb", "notes.md", "package-lock.json"):
            self.assertFalse(C.existing_source(path), path)


class DuplicateAndUnusedTests(unittest.TestCase):
    def test_a_helper_the_project_already_has_is_a_duplicate(self):
        r = run(added("src/a.py", "def format_date(d):", "    return d"), {"format_date": ["src/util.py"]})
        self.assertEqual(kinds(r), ["duplicate"])
        self.assertIn("src/util.py", r["findings"][0]["detail"])
        self.assertEqual((r["findings"][0]["files"], r["findings"][0]["line"]), (["src/a.py"], 1))

    def test_defining_the_name_in_its_own_file_is_not_a_duplicate(self):
        self.assertTrue(run(added("src/a.py", "def format_date(d):"), {"format_date": ["src/a.py"]})["clear"])

    def test_a_definition_nothing_mentions_is_unused_but_a_used_one_is_not(self):
        text = "def format_date(d):"
        self.assertEqual(kinds(run(added("src/a.py", text), mentions=lambda n: 1)), ["unused"])
        self.assertTrue(run(added("src/a.py", text), mentions=lambda n: 2)["clear"])

    def test_a_decorated_function_is_registered_by_its_decorator_so_it_is_not_unused(self):
        r = run(added("src/a.py", "@app.route('/x')", "def serve_leaderboard():"), mentions=lambda n: 1)
        self.assertTrue(r["clear"])

    def test_names_that_every_file_has_are_never_looked_up(self):
        asked = []
        C.evaluate(added("src/a.py", "def render(x):", "class Model:", "def test_something_long(self):", "def __init__(self):"),
                   lambda n: asked.append(n) or [], lambda n: 1)
        self.assertEqual(asked, [])

    def test_other_languages_and_arrow_functions(self):
        r = run({**added("web/a.ts", "export const formatMoney = (n: number) => n;"), **added("App/B.swift", "func scoreBoard() {}"),
                 **added("main.go", "func RankPlayers() {}")}, mentions=lambda n: 1)
        self.assertEqual(sorted(f["files"][0] for f in r["findings"]), ["App/B.swift", "main.go", "web/a.ts"])
        self.assertEqual(set(kinds(r)), {"unused"})
        # a method (indented) and a one-word name are not judged
        self.assertTrue(run(added("App/B.swift", "    func scoreBoard() {}", "func score() {}"), mentions=lambda n: 1)["clear"])

    def test_tests_locks_and_non_source_are_not_judged(self):
        add = {**added("tests/test_a.py", "def format_date(d):"), **added("package-lock.json", "def format_date(d):"),
               **added("notes.md", "def format_date(d):")}
        self.assertTrue(run(add, {"format_date": ["src/u.py"]}, mentions=lambda n: 1)["clear"])

    def test_lookups_are_capped_so_a_huge_change_cannot_run_hundreds_of_git_calls(self):
        calls = []
        many = added("src/a.py", *[f"def helper_function_{i}():" for i in range(C.MAX_NAMES + 25)])
        C.evaluate(many, lambda n: calls.append(n) or [], lambda n: 2)
        self.assertEqual(len(calls), C.MAX_NAMES)


class LeftoverTests(unittest.TestCase):
    def test_debug_statements_and_markers_are_one_finding_per_file(self):
        r = run(added("web/a.js", "const x = 1;", "console.log(x);", "debugger;"))
        self.assertEqual(kinds(r), ["leftover"])
        self.assertEqual(r["findings"][0]["line"], 2)
        self.assertIn("1 more", r["findings"][0]["detail"])

    def test_normal_prints_and_todos_are_left_alone(self):
        self.assertTrue(run(added("src/cli.py", "print('hello')", "# TODO: later"))["clear"])

    def test_two_or_more_commented_out_lines_of_code(self):
        r = run(added("src/a.py", "# old = compute(x)", "# return old", "# a normal explanation of the step"))
        self.assertEqual([f["title"] for f in r["findings"]], ["Commented-out code"])
        self.assertTrue(run(added("src/a.py", "# old = compute(x)", "# explains why"))["clear"])


class PastedBlockTests(unittest.TestCase):
    BLOCK = ["total = compute_total(items)", "discount = lookup_discount(user)", "if discount is not None:",
             "    total = total - discount.apply(total)", "return round(total, 2)"]

    def test_the_same_block_in_two_places_is_one_finding(self):
        add = {**added("src/a.py", *self.BLOCK), **added("src/b.py", "x = 1", *self.BLOCK, start=20)}
        r = run(add)
        self.assertEqual(kinds(r), ["pasted_block"])
        self.assertEqual((r["findings"][0]["files"], r["findings"][0]["line"]), (["src/b.py"], 21))

    def test_a_long_pasted_block_is_still_one_finding_that_grows(self):
        long = self.BLOCK + [f"result_{i} = transform_value_{i}(total)" for i in range(6)]
        r = run({**added("src/a.py", *long), **added("src/b.py", *long)})
        self.assertEqual(kinds(r), ["pasted_block"])
        self.assertIn("-" + str(len(long)), r["findings"][0]["detail"])

    def test_boilerplate_lines_and_a_block_repeated_in_one_spot_are_not_pasted_code(self):
        brackets = ["}", "});", "]", "end", "else {"]
        self.assertTrue(run({**added("src/a.py", *brackets), **added("src/b.py", *brackets)})["clear"])
        self.assertTrue(run(added("src/a.py", *self.BLOCK))["clear"])


class AcceptAndRequestTests(unittest.TestCase):
    def test_accepted_findings_are_hidden_and_counted(self):
        add = added("src/a.py", "def format_date(d):")
        first = run(add, mentions=lambda n: 1)
        r = run(add, mentions=lambda n: 1, accepted=[first["findings"][0]["id"]])
        self.assertTrue(r["clear"])
        self.assertEqual(r["accepted"], 1)

    def test_high_severity_sorts_first_then_file_and_line(self):
        add = {**added("src/b.py", "console.log(1)"), **added("src/a.py", "def format_date(d):", "def other_helper(d):")}
        r = run(add, {"format_date": ["src/u.py"]}, mentions=lambda n: 1)
        self.assertEqual([(f["kind"], f["files"][0]) for f in r["findings"]],
                         [("duplicate", "src/a.py"), ("unused", "src/a.py"), ("leftover", "src/b.py")])

    def test_fix_request_names_each_finding_and_forbids_new_work(self):
        r = run(added("src/a.py", "def format_date(d):"), {"format_date": ["src/u.py"]})
        text = C.fix_request(r["findings"], "Leaderboard")
        self.assertIn("Leaderboard", text)
        self.assertIn("src/a.py:1", text)
        self.assertIn("Do not add", text)


class JobCruftTests(unittest.TestCase):
    """The server's half: real git in a throwaway repository, never the live checkout."""

    def sh(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=self.root, check=True,
                       capture_output=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sh("init", "-q", "-b", "main")
        (self.root / "src").mkdir()
        (self.root / "src" / "util.py").write_text("def format_date(d):\n    return str(d)\n\n\ndef used_helper_fn(x):\n    return x\n")
        (self.root / "src" / "app.py").write_text("from util import used_helper_fn\n\nprint(used_helper_fn(1))\n")
        # Two look-alikes that are not duplicates: JavaScript text inside a Python test, and a method of the same name.
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_js.py").write_text("SOURCE = '''\nasync function rank_everyone() {}\n'''\nfunction rank_everyone() {}\n")
        (self.root / "src" / "k.py").write_text("class Klass:\n    def rank_everyone(self):\n        pass\n")
        self.sh("add", "-A")
        self.sh("commit", "-q", "-m", "base")
        self.sh("checkout", "-q", "-b", "ai/leaderboard")
        (self.root / "src" / "board.py").write_text(
            "def format_date(d):\n    return d.isoformat()\n\n\ndef never_called_fn(x):\n    return x\n\n\n"
            "def rank_everyone(scores):\n    console_log = 1\n    return sorted(scores)\n")
        (self.root / "src" / "app.py").write_text("from util import used_helper_fn\nfrom board import rank_everyone\n\nprint(used_helper_fn(1), rank_everyone([]))\n")
        self.sh("add", "-A")
        self.sh("commit", "-q", "-m", "feature")
        self.job = {"title": "Leaderboard", "branch": "ai/leaderboard", "base_branch": "main"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_the_duplicate_and_the_unused_helper_and_nothing_else(self):
        from orchestrator.web import server
        r = server.job_cruft(self.root, self.job)
        self.assertEqual(sorted((f["kind"], f["line"]) for f in r["findings"]), [("duplicate", 1), ("unused", 5)])
        dup = next(f for f in r["findings"] if f["kind"] == "duplicate")
        self.assertIn("src/util.py", dup["detail"])
        self.assertIn("Leaderboard", r["fix"]["spec"])

    def test_accepting_a_finding_hides_it_and_the_answer_is_kept_until_the_branch_moves(self):
        from unittest import mock
        from orchestrator.web import server
        first = server.job_cruft(self.root, self.job)
        with mock.patch.object(server, "git", side_effect=server.git) as spy:
            self.assertIs(server.job_cruft(self.root, self.job), first)
            self.assertEqual(spy.call_count, 2)  # branch tip and base tip only: no diff, no lookups
        accepted = {**self.job, "cruft_accepted": [first["findings"][0]["id"]]}
        self.assertEqual(len(server.job_cruft(self.root, accepted)["findings"]), 1)

    def test_it_reports_how_many_of_the_suggested_definitions_the_change_used(self):
        from orchestrator.web import server
        suggested = [{"name": "used_helper_fn", "path": "src/util.py", "line": 5}, {"name": "format_date", "path": "src/util.py", "line": 1}]
        r = server.job_cruft(self.root, {**self.job, "reuse_suggested": suggested})
        # app.py now calls used_helper_fn; board.py defines its own format_date, which is not using the existing one
        self.assertEqual(r["reuse"], {"suggested": 2, "used": ["used_helper_fn"]})
        self.assertNotIn("reuse", server.job_cruft(self.root, self.job))

    def test_a_project_module_named_like_a_test_still_counts_as_existing_code(self):
        from orchestrator.web import server
        self.sh("checkout", "-q", "main")
        (self.root / "src" / "test_probe.py").write_text("def probe_cache_reader(path):\n    return path\n")
        self.sh("add", "-A")
        self.sh("commit", "-q", "-m", "a module that probes tests")
        self.sh("checkout", "-q", "-b", "ai/second")
        (self.root / "src" / "fresh.py").write_text("def probe_cache_reader(path):\n    return path.strip()\n")
        self.sh("add", "-A")
        self.sh("commit", "-q", "-m", "second")
        r = server.job_cruft(self.root, {"title": "x", "branch": "ai/second", "base_branch": "main"})
        dup = [f for f in r["findings"] if f["kind"] == "duplicate"]
        self.assertEqual(len(dup), 1)
        self.assertIn("src/test_probe.py", dup[0]["detail"])

    def test_a_job_without_a_branch_has_nothing_to_check(self):
        from orchestrator.web import server
        self.assertIsNone(server.job_cruft(self.root, {"title": "x"}))
        self.assertIsNone(server.job_cruft(self.root, {"title": "x", "branch": "no-such-branch"}))


if __name__ == "__main__":
    unittest.main()
