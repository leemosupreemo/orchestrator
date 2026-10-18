import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import feature_map
from orchestrator import features as store

PRD = "# Word Duel\n\n## Pitch\nA turn-based word game.\n\n## Core features\n- Play a round\n- Challenge a friend\n"


def reply(features):
    return "Here you go:\n" + json.dumps({"features": features})


def item(name, depends_on=(), paths=(), stories=("As a player I can play.",), serves="Play a round"):
    return {"name": name, "summary": f"{name} summary", "stories": list(stories), "paths": list(paths),
            "depends_on": list(depends_on), "serves": serves}


class FeatureMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_carries_the_prd_existing_features_and_layout(self):
        existing = [{"id": "accounts", "name": "Accounts", "summary": "Sign in", "paths": ["app/auth/"]}]
        prompt = feature_map.prompt(PRD, existing, "app/\n  auth/\n  game/")
        self.assertIn("A turn-based word game", prompt)
        self.assertIn("Accounts", prompt)
        self.assertIn("app/auth/", prompt)
        self.assertIn('"features"', prompt)  # asks for the JSON shape

    def test_proposal_is_validated_and_ordered_into_layers(self):
        proposal = feature_map.parse(reply([item("Challenge a friend", ["Play a round"], ["app/social/"]),
                                            item("Play a round", [], ["app/game/"])]), [])
        self.assertEqual([f["name"] for f in proposal["features"]], ["Play a round", "Challenge a friend"])
        self.assertEqual([f["layer"] for f in proposal["features"]], [0, 1])
        self.assertEqual(proposal["warnings"], [])

    def test_dependencies_may_name_existing_features(self):
        existing = [{"id": "accounts", "name": "Accounts", "paths": []}]
        proposal = feature_map.parse(reply([item("Play a round", ["Accounts"])]), existing)
        self.assertEqual(proposal["features"][0]["depends_on"], ["Accounts"])
        self.assertEqual(proposal["features"][0]["layer"], 1)

    def test_unknown_dependencies_are_dropped_with_a_warning(self):
        proposal = feature_map.parse(reply([item("Play a round", ["Leaderboards"])]), [])
        self.assertEqual(proposal["features"][0]["depends_on"], [])
        self.assertTrue(any("Leaderboards" in w for w in proposal["warnings"]))

    def test_cycles_and_duplicate_names_are_refused(self):
        with self.assertRaises(feature_map.FeatureMapError):
            feature_map.parse(reply([item("A", ["B"]), item("B", ["A"])]), [])
        with self.assertRaises(feature_map.FeatureMapError):
            feature_map.parse(reply([item("Play"), item("play ")]), [])
        dup_existing = feature_map.parse(reply([item("Accounts"), item("Play")]), [{"id": "accounts", "name": "Accounts", "paths": []}])
        self.assertEqual([f["name"] for f in dup_existing["features"]], ["Play"])  # already a feature: not proposed again
        self.assertTrue(any("Accounts" in w for w in dup_existing["warnings"]))

    def test_overlapping_paths_are_flagged(self):
        existing = [{"id": "game", "name": "Game", "paths": ["app/game/"]}]
        proposal = feature_map.parse(reply([item("Scoring", [], ["app/game/scoring.py"]), item("Lobby", [], ["app/lobby/"]),
                                            item("Lobby chat", [], ["app/lobby/chat/"])]), existing)
        joined = " ".join(proposal["warnings"])
        self.assertIn("Scoring", joined)
        self.assertIn("Game", joined)
        self.assertIn("Lobby chat", joined)

    def test_garbage_and_oversized_answers_are_refused_or_trimmed(self):
        for bad in ("no json here", json.dumps({"features": "nope"}), json.dumps({"features": []})):
            with self.subTest(bad=bad), self.assertRaises(feature_map.FeatureMapError):
                feature_map.parse(bad, [])
        many = feature_map.parse(reply([item(f"Feature {i}") for i in range(40)]), [])
        self.assertEqual(len(many["features"]), feature_map.MAX_FEATURES)
        long = feature_map.parse(reply([item("X" * 200, stories=["s" * 900] * 30)]), [])
        self.assertLessEqual(len(long["features"][0]["name"]), 80)
        self.assertLessEqual(len(long["features"][0]["stories"]), 8)

    def test_accepting_creates_chosen_features_in_order_with_stories(self):
        store.create(self.runtime, "Accounts")
        proposal = feature_map.parse(reply([item("Challenge a friend", ["Play a round", "Accounts"]), item("Play a round"), item("Settings")]),
                                     store.load(self.runtime))
        created = feature_map.accept(self.runtime, proposal["features"], ["Play a round", "Challenge a friend"])
        self.assertEqual([f["name"] for f in created], ["Play a round", "Challenge a friend"])
        saved = {f["name"]: f for f in store.load(self.runtime)}
        self.assertNotIn("Settings", saved)
        self.assertEqual(sorted(saved["Challenge a friend"]["depends_on"]), sorted([saved["Play a round"]["id"], "accounts"]))
        self.assertEqual(saved["Play a round"]["stories"], ["As a player I can play."])

    def test_accepting_a_feature_without_what_it_depends_on_is_refused(self):
        proposal = feature_map.parse(reply([item("Challenge a friend", ["Play a round"]), item("Play a round")]), [])
        with self.assertRaises(feature_map.FeatureMapError):
            feature_map.accept(self.runtime, proposal["features"], ["Challenge a friend"])
        self.assertEqual(store.load(self.runtime), [])



class DetailAndTechniqueTests(unittest.TestCase):
    THIN = "# X\n\n## Pitch\nA notes app.\n"
    FAIR = PRD + "\n## Who it's for\nFriends who like games.\n\n## Core features\n- Play a round\n- Challenge a friend\n- See scores\n- Rematch\n"
    RICH = FAIR + "\n## Look and feel\n" + ("Warm, paper-like, large type. " * 100)

    def test_the_level_follows_how_much_the_person_has_said(self):
        self.assertEqual(feature_map.detail_level(self.THIN), "sparse")
        self.assertEqual(feature_map.detail_level(self.FAIR), "moderate")
        self.assertEqual(feature_map.detail_level(self.RICH), "detailed")
        self.assertEqual(feature_map.detail_level(self.THIN, extra_chars=20000), "detailed")  # an attached document counts

    def test_the_prompt_asks_for_less_when_there_is_less_to_go_on(self):
        thin, rich = feature_map.prompt(self.THIN, [], "", "sparse"), feature_map.prompt(self.RICH, [], "", "detailed")
        self.assertIn("3 to 6", thin)
        self.assertIn("No user stories", thin)
        self.assertIn("up to 15 epics", rich)
        for name in ("story_map", "wbs", "list"):
            self.assertIn(f'"{name}"', thin)

    def test_the_prompt_includes_an_attached_source_document(self):
        self.assertIn("Refunds need a manager", feature_map.prompt(self.FAIR, [], "", "detailed", "Refunds need a manager"))

    def test_a_sparse_answer_is_trimmed_to_what_a_thin_brief_supports(self):
        entries = [{**item(f"Epic {n}"), "requirements": ["a", "b", "c", "d"], "stories": ["As a x", "As a y"]} for n in range(9)]
        proposal = feature_map.parse(reply(entries), [], "sparse")
        self.assertEqual(len(proposal["features"]), 6)
        self.assertTrue(all(len(f["requirements"]) == 2 and f["stories"] == [] for f in proposal["features"]))
        self.assertEqual(proposal["detail"], "sparse")

    def test_the_technique_open_questions_and_slices_come_back_checked(self):
        body = json.dumps({"technique": "story_map", "rationale": "People use it through a flow.", "open_questions": ["Who pays?", "Who pays?", ""],
                           "features": [{**item("Play a round"), "slice": "mvp", "requirements": ["A round has 6 turns"]},
                                        {**item("Chat", ["Play a round"]), "slice": "someday"}]})
        proposal = feature_map.parse(body, [], "detailed")
        self.assertEqual((proposal["technique"], proposal["technique_name"]), ("story_map", "Story map"))
        self.assertEqual(proposal["open_questions"], ["Who pays?"])
        self.assertEqual([f["slice"] for f in proposal["features"]], ["MVP", ""])
        self.assertEqual(proposal["features"][0]["requirements"], ["A round has 6 turns"])

    def test_slices_are_dropped_unless_it_is_a_story_map_and_an_unknown_technique_is_a_list(self):
        body = json.dumps({"technique": "something odd", "features": [{**item("Core"), "slice": "MVP"}]})
        proposal = feature_map.parse(body, [], "moderate")
        self.assertEqual((proposal["technique"], proposal["features"][0]["slice"]), ("list", ""))

    def test_accepting_keeps_the_requirements_and_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            proposal = feature_map.validate([{**item("Play a round"), "requirements": ["A round has 6 turns"], "slice": "MVP"}], [])
            feature_map.accept(runtime, proposal["features"], ["Play a round"])
            saved = store.load(runtime)[0]
            self.assertEqual((saved["requirements"], saved["slice"]), (["A round has 6 turns"], "MVP"))


if __name__ == "__main__":
    unittest.main()
