from __future__ import annotations

import unittest

from orchestrator import bug_split as bs

NOTES = """1. Rejoining a lobby after backgrounding shows an empty seat. Steps: join, background 30s, come back.
2. The score banner overlaps the timer on small phones
- crash when tapping Rematch twice
Also: can we add dark mode?"""


class PromptTests(unittest.TestCase):
    def test_prompt_carries_the_notes_and_the_rules(self):
        text = bs.prompt(NOTES, ["Fix login crash"])
        for expected in ("Rejoining a lobby", "One entry per separate problem", "Do not invent steps", "feature requests", "At most 12",
                         "Bugs already open (do not repeat these):", "- Fix login crash", "ONLY one JSON object"):
            self.assertIn(expected, text)

    def test_prompt_leaves_out_the_open_bugs_section_when_there_are_none(self):
        self.assertNotIn("already open", bs.prompt("one bug"))


class ParseTests(unittest.TestCase):
    ANSWER = ('{"bugs": [{"summary": "Empty seat after rejoining a lobby", "repro": "Join\\nBackground 30s\\n\\nCome back", "expected": "My seat is kept"},'
              ' {"summary": "Score banner overlaps the timer on small phones", "repro": "", "expected": ""},'
              ' {"summary": "Crash when tapping Rematch twice"}]}')

    def test_reads_plain_fenced_and_bare_list_answers(self):
        out = bs.parse(self.ANSWER)
        self.assertEqual([b["summary"] for b in out["bugs"]], ["Empty seat after rejoining a lobby", "Score banner overlaps the timer on small phones", "Crash when tapping Rematch twice"])
        self.assertEqual(out["bugs"][0]["repro"], "Join\nBackground 30s\nCome back")  # steps stay one per line
        self.assertEqual(out["bugs"][2], {"summary": "Crash when tapping Rematch twice", "repro": "", "expected": ""})
        self.assertEqual(len(bs.parse(f"Here you go:\n```json\n{self.ANSWER}\n```")["bugs"]), 3)
        self.assertEqual(bs.parse('["First bug", "Second bug"]')["bugs"][1]["summary"], "Second bug")

    def test_duplicates_blanks_and_junk_are_dropped_and_the_list_is_capped(self):
        out = bs.parse('{"bugs": [{"summary": "Crash on launch"}, {"summary": "crash  on launch!"}, {"summary": "  "}, 7, null, {"title": "Slow list"}]}')
        self.assertEqual([b["summary"] for b in out["bugs"]], ["Crash on launch", "Slow list"])
        many = ",".join(f'{{"summary": "Bug number {n}"}}' for n in range(20))
        self.assertEqual(len(bs.parse(f'{{"bugs": [{many}]}}')["bugs"]), bs.MAX_BUGS)

    def test_a_long_summary_is_cut_to_a_title(self):
        out = bs.parse('{"bugs": [{"summary": "' + "x" * 500 + '"}]}')
        self.assertEqual(len(out["bugs"][0]["summary"]), bs.SUMMARY_CHARS)

    def test_unusable_answers_raise_with_a_reason(self):
        for reply, message in (("no bugs here", "didn't separate"), ("{not json", "wasn't readable"), ('{"bugs": "x"}', "wasn't a list"),
                               ('{"bugs": []}', "didn't find a bug"), ('{"bugs": [{"summary": ""}]}', "didn't find a bug")):
            with self.assertRaises(bs.BugSplitError, msg=reply) as ctx:
                bs.parse(reply)
            self.assertIn(message, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
