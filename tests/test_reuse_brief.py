from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import reuse_brief as R  # noqa: E402

DEFS = [
    ("src/scoring.py", 3, "score_player"),
    ("src/scoring.py", 10, "rank_players"),
    ("src/scoring.py", 20, "ScoreService"),
    ("src/scoring.py", 30, "round_score"),
    ("src/scoring.py", 40, "score_history"),
    ("src/dates.py", 1, "format_date"),
    ("web/board.ts", 5, "renderBoard"),
    ("src/payments.py", 7, "charge_card"),
]


class TokenTests(unittest.TestCase):
    def test_names_split_at_underscores_and_capitals_and_endings_are_trimmed(self):
        self.assertEqual(R.tokens("rankPlayers"), R.tokens("rank_players"))
        self.assertEqual(R.tokens("rankPlayers"), {"rank", "player"})
        self.assertEqual(R.tokens("Ranking scores"), R.tokens("rank score"))

    def test_filler_and_short_words_are_dropped(self):
        self.assertEqual(R.tokens("Add a new feature for the user to make it work"), set())


class RankTests(unittest.TestCase):
    def test_definitions_sharing_words_with_the_task_come_back_best_first(self):
        got = R.rank(DEFS, "Global leaderboard ranking players by score")
        self.assertEqual(got[0]["name"], "rank_players")  # matches both ranking and player
        self.assertNotIn("charge_card", [g["name"] for g in got])
        self.assertNotIn("format_date", [g["name"] for g in got])

    def test_nothing_related_means_nothing_suggested(self):
        self.assertEqual(R.rank(DEFS, "Rotate the log files nightly"), [])

    def test_a_definition_in_a_file_the_plan_changes_ranks_higher(self):
        defs = [("src/a.py", 1, "score_value"), ("src/b.py", 1, "score_value")]
        self.assertEqual(R.rank(defs, "score", ["src/b.py"])[0]["path"], "src/b.py")
        self.assertEqual(R.rank(defs, "score", ["src/"])[0]["path"], "src/a.py")  # a directory plan covers both: stable order

    def test_one_file_cannot_fill_the_list(self):
        got = R.rank(DEFS, "score player ranking history round")
        self.assertLessEqual(sum(1 for g in got if g["path"] == "src/scoring.py"), R.PER_FILE)

    def test_the_list_is_capped(self):
        many = [(f"src/f{i}.py", 1, f"score_helper_{i}") for i in range(30)]
        self.assertEqual(len(R.rank(many, "score helper")), R.LIMIT)


class MergeTests(unittest.TestCase):
    def test_each_tasks_suggestions_join_the_jobs_once(self):
        a = {"name": "rank_players", "path": "src/scoring.py", "line": 10}
        b = {"name": "format_date", "path": "src/dates.py", "line": 1}
        self.assertEqual(R.merge(None, [a]), [a])
        self.assertEqual(R.merge([a], [{**a, "line": 99}, b]), [a, b])  # the same definition again is not added twice
        self.assertEqual(R.merge([a], None), [a])
        self.assertEqual(R.merge(None, None), [])

    def test_it_does_not_change_what_it_was_given(self):
        known = [{"name": "x_helper", "path": "a.py", "line": 1}]
        R.merge(known, [{"name": "y_helper", "path": "b.py", "line": 1}])
        self.assertEqual(len(known), 1)


class TaskTextAndBlockTests(unittest.TestCase):
    def test_the_task_text_gathers_title_summary_and_criteria(self):
        job = {"title": "Leaderboard", "plan": {"summary": "Rank everyone", "acceptance_criteria": ["Shows top ten", {"text": "Ties share a rank"}]}}
        text = R.task_text(job)
        for part in ("Leaderboard", "Rank everyone", "Shows top ten", "Ties share a rank"):
            self.assertIn(part, text)

    def test_the_block_names_each_suggestion_and_says_it_is_a_guess(self):
        block = R.prompt_block([{"name": "rank_players", "path": "src/scoring.py", "line": 10}])
        self.assertIn("### EXISTING CODE TO REUSE", block)
        self.assertIn("`rank_players` (src/scoring.py:10)", block)
        self.assertIn("It is a guess", block)
        self.assertTrue(block.endswith("\n\n"))

    def test_there_is_no_block_when_there_is_nothing_to_say(self):
        self.assertEqual(R.prompt_block([]), "")

    def test_planned_files_that_already_exist_are_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src").mkdir()
            (Path(tmp) / "src" / "scoring.py").write_text("x = 1\n")
            block = R.prompt_block([], Path(tmp), ["src/scoring.py", "src/new_file.py"])
            self.assertIn("src/scoring.py", block)
            self.assertNotIn("new_file", block)


class RepositoryTests(unittest.TestCase):
    """The git half, in a throwaway repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "scoring.py").write_text("def rank_players(scores):\n    return sorted(scores)\n\n\nclass ScoreService:\n    pass\n\n\n    def method_inside(self):\n        pass\n")
        (self.root / "tests" / "test_scoring.py").write_text("def rank_players_test_helper():\n    pass\n")
        (self.root / "notes.md").write_text("def rank_players_in_docs():\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_index_has_top_level_source_definitions_only_including_uncommitted_files(self):
        (self.root / "src" / "fresh.py").write_text("def brand_new_helper():\n    pass\n")  # untracked: an earlier task in the same job
        names = {(p, n) for p, _, n in R.index(self.root)}
        self.assertEqual(names, {("src/scoring.py", "rank_players"), ("src/scoring.py", "ScoreService"), ("src/fresh.py", "brand_new_helper")})

    def test_candidates_for_a_job_find_the_existing_helper(self):
        job = {"title": "Leaderboard", "plan": {"summary": "Rank the players", "likely_files": ["src/scoring.py"]}}
        got = R.candidates(self.root, job)
        self.assertEqual([g["name"] for g in got][:1], ["rank_players"])
        self.assertEqual(got[0]["line"], 1)

    def test_outside_a_repository_there_is_simply_no_index(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(R.index(Path(plain)), [])


if __name__ == "__main__":
    unittest.main()
