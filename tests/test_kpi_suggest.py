from __future__ import annotations

import unittest

from orchestrator import kpi_suggest as ks

FEATURE = {"id": "lobby", "name": "Lobby", "summary": "Friends meet and pick seats.", "paths": ["app/lobby/"],
           "kpis": [{"name": "Seat claims", "event": "seat_claimed"}]}


class PromptTests(unittest.TestCase):
    def test_prompt_asks_for_one_metric_and_carries_the_context(self):
        text = ks.prompt(FEATURE, "A word game to play with friends.", ["Add seat picker", "Fix lobby crash"])
        for expected in ("exactly ONE metric", "Feature: Lobby", "Friends meet and pick seats.", "app/lobby/", "- Add seat picker",
                         "do not repeat these", "- Seat claims (event seat_claimed)", "A word game to play with friends.", "ONLY one JSON object"):
            self.assertIn(expected, text)

    def test_prompt_leaves_out_sections_that_have_nothing_in_them(self):
        text = ks.prompt({"id": "x", "name": "X"})
        for absent in ("Jobs that built it", "already has", "Product requirements", "Code it owns"):
            self.assertNotIn(absent, text)


class ParseTests(unittest.TestCase):
    ANSWER = '{"name": "Lobby join rate", "event": "lobby_joined", "direction": "up", "target": "70", "unit": "%", "why": "It shows people reach the game."}'

    def test_reads_a_plain_or_fenced_answer_into_the_form_fields(self):
        expected = {"name": "Lobby join rate", "event": "lobby_joined", "direction": "up", "target": 70.0, "unit": "%",
                    "why": "It shows people reach the game."}
        self.assertEqual(ks.parse(self.ANSWER, FEATURE), expected)
        self.assertEqual(ks.parse(f"Here you go:\n```json\n{self.ANSWER}\n```\nHope that helps.", FEATURE), expected)

    def test_a_missing_target_is_fine(self):
        out = ks.parse('{"name": "Crash rate", "event": "app_crashed", "direction": "down", "target": null, "unit": ""}', FEATURE)
        self.assertIsNone(out["target"])
        self.assertEqual(out["direction"], "down")

    def test_answers_that_cannot_be_used_say_why(self):
        for reply, message in (("no idea", "didn't suggest"), ("{not json", "wasn't readable"), ("[1, 2]", "didn't suggest"),
                               ('{"name": "X", "event": "Bad Event", "direction": "up"}', "Event names"),
                               ('{"name": "X", "event": "ok_event", "direction": "sideways"}', "Direction"),
                               ('{"name": "", "event": "ok_event", "direction": "up"}', "needs a name"),
                               ('{"name": "X", "event": "ok_event", "direction": "up", "target": "lots"}', "target"),
                               ('{"name": "Again", "event": "seat_claimed", "direction": "up"}', "already used")):
            with self.assertRaises(ks.KpiSuggestError, msg=reply) as ctx:
                ks.parse(reply, FEATURE)
            self.assertIn(message, str(ctx.exception), reply)


if __name__ == "__main__":
    unittest.main()
