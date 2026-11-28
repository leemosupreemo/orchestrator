""""Add tests" on an untested file opens a test job prefilled from that file: area, spec, test cases, feature."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import gap_brief as gb

ROOT = Path(__file__).resolve().parents[1]

FILES = {
    "App/Features/Lobby/Components/PlayerSlotsView.swift": "import SwiftUI\nstruct PlayerSlotLayout {}\nstruct PlayerSlotsView: View {\n  func slotView() {}\n  private func hidden() {}\n  var body: some View { Text(\"x\") }\n}\n",
    "App/Features/Lobby/LobbyViewModel.swift": "import Foundation\nfinal class LobbyViewModel {\n  func join() {}\n}\n",
    "App/Domain/Cards/Card.swift": "import Foundation\nstruct Card: Hashable {\n  let rank: Rank\n  func hash(into h: inout Hasher) {}\n}\nenum Rank { case ace }\n",
    "App/Domain/Cards/Deck.swift": "struct Deck { func draw() {} }\n",
    "App/Services/Log.swift": "import OSLog\nenum Log {\n  static func info(_ m: String) {}\n  static func error(_ m: String) {}\n}\n",
    "App/DesignSystem/Transitions/PeriodicSheenModifier.swift": "import SwiftUI\nstruct PeriodicSheenModifier: ViewModifier {\n  func body(content: Content) -> some View { content }\n}\n",
    "AppTests/Unit/Features/Lobby/LobbyViewModelTests.swift": "import XCTest\nfinal class LobbyViewModelTests: XCTestCase { func testJoin() { _ = LobbyViewModel() } }\n",
    "AppTests/Unit/Domain/Cards/DeckTests.swift": "import Testing\n@Test func draws() { _ = Deck() }\n",
    "AppTests/Unit/Services/Audio/AudioTests.swift": "import XCTest\nfinal class AudioTests: XCTestCase {}\n",
}


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel, text in FILES.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8")
        self.platform = {"name": "Client", "label": "Client", "paths": ["App", "AppTests"]}

    def tearDown(self):
        self.tmp.cleanup()

    def brief(self, path, **kw):
        return gb.brief(self.root, path, platform=self.platform, **kw)

    def test_a_view_gets_its_area_subject_approach_examples_and_folder(self):
        b = self.brief("App/Features/Lobby/Components/PlayerSlotsView.swift", test_command="xcodebuild test -scheme App")
        self.assertEqual((b["area"], b["summary"], b["file"]["role"], b["framework"]), ("Lobby", "Add tests for PlayerSlotsView (Lobby)", "view", "XCTest"))
        self.assertEqual(b["file"]["functions"], ["slotView"])  # private helpers and the view body aren't the behaviour to test
        self.assertEqual(b["examples"][0]["path"], "AppTests/Unit/Features/Lobby/LobbyViewModelTests.swift")  # tests a neighbour
        for needle in ("FILE: App/Features/Lobby/Components/PlayerSlotsView.swift", "PLATFORM: Client", "A screen or UI component",
                       "Declares PlayerSlotLayout, PlayerSlotsView", "HOW TO TEST IT:", "move it into a small view model",
                       "FOLLOW THESE EXISTING TESTS", "PUT THE NEW TESTS: in AppTests/Unit/Features/Lobby/, using XCTest.",
                       "RUN WITH: xcodebuild test -scheme App", "A test mentions PlayerSlotsView", "DON'T:"):
            self.assertIn(needle, b["spec"])

    def test_each_kind_of_code_gets_its_own_advice(self):
        self.assertEqual(self.brief("App/Domain/Cards/Card.swift")["file"]["role"], "model")
        self.assertEqual(self.brief("App/Services/Log.swift")["file"]["role"], "service")
        self.assertEqual(self.brief("App/DesignSystem/Transitions/PeriodicSheenModifier.swift")["file"]["role"], "visual effect")
        self.assertIn("Never write to the real system log", self.brief("App/Services/Log.swift")["spec"])
        self.assertIn("say so in the plan instead of writing empty tests", self.brief("App/DesignSystem/Transitions/PeriodicSheenModifier.swift")["spec"])

    def test_tests_go_in_the_folder_that_mirrors_the_source(self):
        card = self.brief("App/Domain/Cards/Card.swift")
        self.assertIn("PUT THE NEW TESTS: in AppTests/Unit/Domain/Cards/, using Swift Testing", card["spec"])  # follows its neighbour's framework
        self.assertIn("in AppTests/Unit/Services/", self.brief("App/Services/Log.swift")["spec"])  # not the Audio folder below it

    def test_matching_test_cases_and_the_feature_are_prefilled(self):
        cases = [{"id": "TC-1", "area": "lobby", "title": "Seat a player"}, {"id": "TC-2", "area": "Shop", "title": "Buy coins"},
                 {"id": "TC-3", "area": "Game", "title": "Shows PlayerSlotsView when full"}, {"id": "TC-4", "area": "Lobby", "title": "Leave"}]
        features = [{"id": "f-app", "name": "App", "paths": ["App"]}, {"id": "f-lobby", "name": "Lobby", "summary": "Find a game", "paths": ["App/Features/Lobby/"]}]
        b = self.brief("App/Features/Lobby/Components/PlayerSlotsView.swift", cases=cases, features=features, uncovered=200)
        self.assertEqual(b["area"].lower(), "lobby")  # an area the library already uses
        self.assertEqual(b["test_cases"].splitlines(), ["TC-1: Seat a player", "TC-3: Shows PlayerSlotsView when full", "TC-4: Leave"])
        self.assertEqual(b["feature"], "f-lobby")  # the most specific feature wins
        self.assertIn("FEATURE: Lobby: Find a game", b["spec"])
        self.assertIn("200 of them never run by the tests", b["spec"])

    def test_paths_outside_the_project_or_missing_are_refused(self):
        for bad in ("../etc/passwd", "App/Missing.swift", "/etc/hosts"):
            with self.assertRaises(ValueError):
                gb.brief(self.root, bad)

    def test_areas_come_from_feature_folders_or_the_nearest_meaningful_folder(self):
        self.assertEqual(gb.area_of("App/Features/Gameplay/GameScene/Win.swift"), "Gameplay")
        self.assertEqual(gb.area_of("App/Domain/Cards/Card.swift"), "Cards")
        self.assertEqual(gb.area_of("src/components/Button.tsx"), "components")  # only generic folders: the nearest one
        self.assertEqual(gb.area_of("App/Services/Log.swift", ["services"]), "services")

    def test_the_server_finds_the_platform_and_uses_the_projects_command_cases_and_features(self):
        from orchestrator.web import server as ui
        rt = ui.runtime_dir(self.root)
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "project.json").write_text(json.dumps({"test_command": "swift test", "platforms": [{"name": "Client", "kind": "client", "paths": ["App", "AppTests"]}]}), encoding="utf-8")
        b = ui.test_gap_brief(self.root, "App/Domain/Cards/Card.swift", "Client", "12")
        self.assertIn("PLATFORM: Client (Client)", b["spec"])
        self.assertIn("RUN WITH: swift test", b["spec"])
        self.assertIn("12 of them never run", b["spec"])
        with self.assertRaises(ui.UIError):
            ui.test_gap_brief(self.root, "../outside.swift")


class PageTests(unittest.TestCase):
    def test_add_tests_opens_a_prefilled_test_job(self):
        app = (ROOT / "orchestrator/web/static/app.js").read_text(encoding="utf-8")
        self.assertIn("#/new?type=coverage&gap=${encodeURIComponent(g.path)}&platform=${encodeURIComponent(p.name)}", app)
        self.assertIn("api(`tests/gap-brief?path=", app)
        self.assertIn("Object.assign(st.v, { summary: st.gap.area, details: st.gap.spec, test_cases: st.gap.test_cases })", app)
        self.assertIn('query.set("feature", st.gap.feature)', app)
        self.assertIn("Prefilled for <span class=\"mono\">", app)
        server = (ROOT / "orchestrator/web/server.py").read_text(encoding="utf-8")
        self.assertIn('parts == ["tests", "gap-brief"]', server)


if __name__ == "__main__":
    unittest.main()
