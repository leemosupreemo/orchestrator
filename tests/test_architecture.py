from __future__ import annotations

import json
import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import architecture as arch
from orchestrator import new_project
from orchestrator.stack_detection import detect_project_stack

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}

REPLY = {
    "summary": "Native SwiftUI keeps the app small and fast for a single-platform game.",
    "candidates": [
        {"name": "Native SwiftUI + CloudKit", "best_for": "An iPhone-only game", "fit": 88, "pros": ["No backend to run"], "cons": ["iPhone only"],
         "profile": {"shape": {"hosted_backend": 3, "client_only": 1}, "code": {"layered": 100}, "ui": {"mvvm": 100}},
         "diagram": {"nodes": [{"id": "player", "label": "Player", "kind": "user"}, {"id": "app", "label": "iPhone app", "kind": "app"},
                               {"id": "cloud", "label": "CloudKit", "kind": "external"}],
                     "edges": [{"from": "player", "to": "app", "label": "plays"}, {"from": "app", "to": "cloud", "label": "syncs"}]}},
        {"name": "React Native + Firebase", "summary": "Cross-platform.", "fit": 140, "pros": ["Android later"], "cons": ["Heavier"],
         "profile": {"client_server": 100}},
    ],
    "chosen": "Native SwiftUI + CloudKit",
    "stack": {"language": "Swift", "framework": "SwiftUI", "build_command": "xcodebuild build -scheme WordDuel", "test_command": "xcodebuild test -scheme WordDuel"},
    "platforms": [{"platform": "iOS app", "language": "Swift", "framework": "SwiftUI", "notes": ""}],
    "layers": [{"name": "Views", "responsibility": "SwiftUI screens"}, {"name": "Game engine", "responsibility": "Rules and scoring"}],
    "structure": ["WordDuel/Views/: screens", "WordDuel/Engine/: rules"],
    "packages": [{"name": "pointfreeco/swift-snapshot-testing", "ecosystem": "swiftpm", "purpose": "screen tests"}],
    "decisions": [{"topic": "Sync", "choice": "CloudKit", "alternatives": ["Firebase", "Own server"], "reason": "No server to run.", "serves": ["Take turns"]}],
    "prd_links": [{"requirement": "Take turns", "how": "Moves sync through CloudKit"}],
    "prd_gaps": ["Android players"],
    "risks": ["CloudKit limits sharing with Android later"],
    "open_questions": [],
    "revisit_when": ["Android support becomes required", "Offline multiplayer"],
    "sources": ["https://developer.apple.com/icloud/cloudkit/", "http://insecure.example"],
}


def reply_text(**changes) -> str:
    return "Here is my proposal:\n```json\n" + json.dumps({**REPLY, **changes}) + "\n```"


class ParseTests(unittest.TestCase):
    def test_a_good_answer_is_read_and_trimmed(self):
        p = arch.parse_proposal(reply_text())
        self.assertEqual(p["chosen"], "Native SwiftUI + CloudKit")
        self.assertEqual(p["stack"]["language"], "swift")  # normalised to what stack detection knows
        self.assertEqual(len(p["candidates"]), 2)
        self.assertEqual(p["decisions"][0]["alternatives"], ["Firebase", "Own server"])
        self.assertEqual(p["sources"], ["https://developer.apple.com/icloud/cloudkit/"])  # https only

    def test_a_loosely_named_choice_is_matched_to_its_candidate(self):
        self.assertEqual(arch.parse_proposal(reply_text(chosen="native swiftui"))["chosen"], "Native SwiftUI + CloudKit")

    def test_unusable_answers_are_refused(self):
        for bad in ("no json here", reply_text(candidates=[]), reply_text(chosen="Flutter"),
                    reply_text(stack={"language": "cobol"})):
            with self.assertRaises(arch.ArchitectureError):
                arch.parse_proposal(bad)

    def test_the_prompt_carries_the_product_platforms_tools_and_feedback(self):
        text = arch.proposal_prompt("A word game for friends", ["iOS app"], "SwiftUI", ["Xcode (iOS / macOS apps)"],
                                    new_project.PLATFORM_NEEDS, web=False, previous=arch.parse_proposal(reply_text()), feedback="Add Android")
        for needle in ("A word game for friends", "Platforms chosen: iOS app", "SwiftUI", "Xcode (iOS / macOS apps)",
                       "An Xcode project with a scheme", "Add Android", "Your previous proposal", "cannot search the web"):
            self.assertIn(needle, text)

    def test_no_platforms_asks_for_a_recommendation(self):
        self.assertIn("recommend which to build for", arch.proposal_prompt("x", [], "", [], {}, web=True))
        self.assertNotIn("recommend which to build for", arch.proposal_prompt("x", [], "", [], {}, web=True, mode="describe"))

    def test_platforms_and_preferences_are_read_from_the_wizard_pitch(self):
        md = new_project.render_prd({"name": "W", "pitch": "A game", "platform": "iOS app, Web app", "stack": "SwiftUI please"})
        known = [p for p in new_project.PLATFORMS if p != new_project.RECOMMEND]
        self.assertEqual(arch.platforms_from_prd(md, known), (["iOS app", "Web app"], "SwiftUI please"))
        undecided = new_project.render_prd({"name": "W", "pitch": "A game", "platform": new_project.RECOMMEND})
        self.assertEqual(arch.platforms_from_prd(undecided, known)[0], [])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rt = self.root / ".orchestrator"
        (self.root / "docs" / "product").mkdir(parents=True)
        (self.root / "docs" / "product" / "prd.md").write_text("# W\n\n## Pitch\n\nA web app game.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_docs_only_repository_is_codeless(self):
        self.assertTrue(arch.is_codeless(self.root))
        self.assertTrue(arch.needs_decision(self.root))
        (self.root / "app.py").write_text("print(1)\n", encoding="utf-8")
        self.assertFalse(arch.is_codeless(self.root))

    def test_a_manifest_alone_means_scaffolded(self):
        (self.root / "package.json").write_text("{}", encoding="utf-8")
        self.assertFalse(arch.is_codeless(self.root))

    def test_approving_writes_the_record_and_doc_and_clears_the_proposal(self):
        arch.save_proposal(self.rt, {**arch.parse_proposal(reply_text()), "status": "proposed", "feedback_history": ["x"]})
        record = arch.approve(self.root, self.rt, arch.load_proposal(self.rt), prd_text="A web app game.")
        self.assertEqual((record["version"], record["origin"], record["history"][0]["kind"]), ("1.0", "proposed", "initial"))
        self.assertFalse(arch.prd_changed(record, "A  web app game. "))  # whitespace alone isn't a change
        self.assertTrue(arch.prd_changed(record, "A web app game with Android players."))
        self.assertNotIn("feedback_history", record)
        self.assertIsNone(arch.load_proposal(self.rt))
        self.assertEqual(arch.load_decision(self.root)["chosen"], "Native SwiftUI + CloudKit")
        md = arch.doc_path(self.root).read_text(encoding="utf-8")
        for needle in ("## Stack", "Native SwiftUI + CloudKit", "### Sync", "**Considered:** Firebase, Own server", "React Native + Firebase", "## Revisit this when",
                       "## Diagram", "```mermaid", "## Style profile", "100% Layers", "## How it serves the product requirements",
                       "**Take turns:** Moves sync", "**Not covered yet:** Android players", "**Serves:** Take turns", "Fit 88/100", "## History"):
            self.assertIn(needle, md)
        self.assertFalse(arch.needs_decision(self.root))
        tweak = {**arch.parse_proposal(reply_text()), "note": "Add snapshot tests"}
        second = arch.approve(self.root, self.rt, tweak)
        self.assertEqual((second["version"], second["history"][-1]["kind"], second["history"][-1]["note"]), ("1.1", "tweak", "Add snapshot tests"))
        overhaul = arch.parse_proposal(reply_text(chosen="React Native + Firebase", stack={"language": "typescript", "framework": "React Native"}))
        third = arch.approve(self.root, self.rt, overhaul)
        self.assertEqual((third["version"], third["history"][-1]["kind"], len(third["history"])), ("2.0", "overhaul", 3))
        self.assertTrue(any("Primary language: swift → typescript" in c for c in third["history"][-1]["changes"]))

    def test_approving_nothing_is_refused(self):
        with self.assertRaises(arch.ArchitectureError):
            arch.approve(self.root, self.rt, {})

    def test_edits_pick_another_candidate_and_fix_commands(self):
        p = arch.edit_proposal(arch.parse_proposal(reply_text()), {"chosen": "React Native + Firebase", "language": "typescript",
                                                                   "test_command": "npm test"})
        self.assertEqual((p["chosen"], p["stack"]["language"], p["stack"]["test_command"]), ("React Native + Firebase", "typescript", "npm test"))
        with self.assertRaises(arch.ArchitectureError):
            arch.edit_proposal(p, {"chosen": "Something else"})
        with self.assertRaises(arch.ArchitectureError):
            arch.edit_proposal(p, {"language": "cobol"})

    def test_context_blocks_constrain_each_reader(self):
        self.assertIn("not decided yet", arch.context_block(self.root, "planner"))
        self.assertEqual(arch.context_block(self.root, "builder"), "")
        arch.approve(self.root, self.rt, arch.parse_proposal(reply_text()))
        planner = arch.context_block(self.root, "planner")
        self.assertIn("## Architecture (approved", planner)
        self.assertIn("Sync: CloudKit (not Firebase, Own server)", planner)
        self.assertIn("Code organisation: 100% Layers", planner)
        self.assertIn("doesn't cover yet: Android players", planner)
        self.assertIn("don't change it silently", planner)
        self.assertIn("Don't add a framework", arch.context_block(self.root, "builder"))
        self.assertIn("Raise a concern", arch.context_block(self.root, "verifier"))

    def test_stack_detection_uses_the_decision_before_the_prd_guess(self):
        self.assertIn("from PRD", detect_project_stack(self.root).display_name)  # "web app" in the pitch
        arch.approve(self.root, self.rt, arch.parse_proposal(reply_text()))
        stack = detect_project_stack(self.root)
        self.assertEqual((stack.language, stack.test_command), ("swift", "xcodebuild test -scheme WordDuel"))
        self.assertIn("from architecture", stack.display_name)

    def test_real_manifests_win_over_the_decision(self):
        arch.approve(self.root, self.rt, arch.parse_proposal(reply_text()))
        (self.root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8")
        self.assertEqual(detect_project_stack(self.root).language, "rust")

    def test_home_summary_says_what_it_is_or_what_it_needs(self):
        self.assertEqual(arch.summary(self.root)["state"], "undecided")
        arch.approve(self.root, self.rt, arch.parse_proposal(reply_text()), prd_text="A web app game.")
        s = arch.summary(self.root, "A web app game, now with Android support for launch.")
        self.assertEqual((s["state"], s["version"], s["prd_gaps"], s["prd_changed"]), ("approved", "1.0", 1, True))
        self.assertEqual(s["profile"]["code"], [{"style": "layered", "name": "Layers", "weight": 100}])
        self.assertEqual(s["revisit"], ["Android support becomes required"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.go").write_text("package main\n", encoding="utf-8")
            self.assertEqual(arch.summary(Path(tmp))["state"], "undescribed")

    def test_described_projects_are_recorded_as_described(self):
        (self.root / "app.py").write_text("print(1)\n", encoding="utf-8")
        record = arch.approve(self.root, self.rt, arch.parse_proposal(reply_text(), "describe"))
        self.assertEqual((record["origin"], record["history"][0]["kind"]), ("described", "described"))
        self.assertIn("Described from the existing code", arch.doc_path(self.root).read_text(encoding="utf-8"))

    def test_revisit_reasons_flag_triggers_the_requirements_now_mention(self):
        record = arch.parse_proposal(reply_text())
        self.assertEqual(arch.revisit_reasons(record, "We now want Android support for launch."), ["Android support becomes required"])
        self.assertEqual(arch.revisit_reasons(record, "Nicer colours"), [])
        self.assertEqual(arch.revisit_reasons(None, "Android"), [])


class ProfileAndDiagramTests(unittest.TestCase):
    def test_profiles_keep_catalog_styles_and_sum_to_100_per_category(self):
        p = arch.normalize_profile({"shape": {"hosted_backend": 3, "client_only": 1, "made_up": 50}, "code": {"layered": 0.5, "clean": 0.5},
                                    "mvvm": 2, "nonsense": "x"})
        self.assertEqual(p["shape"], {"hosted_backend": 75, "client_only": 25})
        self.assertEqual(sum(p["code"].values()), 100)
        self.assertEqual(p["ui"], {"mvvm": 100})  # a flat style id finds its own category
        thirds = arch.normalize_profile({"code": {"layered": 1, "clean": 1, "flat": 1}})
        self.assertEqual(sum(thirds["code"].values()), 100)
        self.assertEqual(arch.normalize_profile("junk"), {})
        self.assertEqual(arch.profile_line(p, "shape"), "75% App with hosted backend services, 25% App only")

    def test_diagrams_are_trimmed_to_valid_simple_shapes(self):
        nodes = [{"id": f"n{i}", "label": f"Box {i}", "kind": "service"} for i in range(14)]
        d = arch.normalize_diagram({"nodes": nodes + [{"id": "n1", "label": "dup"}, {"label": "Weird kind", "kind": "galaxy"}],
                                    "edges": [{"from": "n0", "to": "n1"}, {"from": "n0", "to": "ghost"}, {"from": "n2", "to": "n2"}]})
        self.assertEqual(len(d["nodes"]), arch.MAX_NODES)
        self.assertEqual(d["edges"], [{"from": "n0", "to": "n1", "label": ""}])
        odd = arch.normalize_diagram({"nodes": [{"label": "Weird kind", "kind": "galaxy"}]})
        self.assertEqual(odd["nodes"], [{"id": "weird_kind", "label": "Weird kind", "kind": "service"}])
        self.assertEqual(arch.normalize_diagram(None), {"nodes": [], "edges": []})

    def test_mermaid_draws_the_diagram_for_the_markdown_page(self):
        text = arch.mermaid(arch.parse_proposal(reply_text())["diagram"])
        self.assertTrue(text.startswith("```mermaid\nflowchart LR"))
        self.assertIn('player(["Player"])', text)
        self.assertIn('cloud{{"CloudKit"}}', text)
        self.assertIn('app -->|"syncs"| cloud', text)
        self.assertEqual(arch.mermaid({"nodes": []}), "")

    def test_each_option_keeps_its_plain_summary_fit_diagram_and_profile(self):
        p = arch.parse_proposal(reply_text())
        first, second = p["candidates"]
        self.assertEqual((first["best_for"], first["fit"]), ("An iPhone-only game", 88))
        self.assertEqual((second["best_for"], second["fit"]), ("Cross-platform.", 100))  # summary stands in; fit clamped
        self.assertEqual(second["profile"], {"shape": {"client_server": 100}})
        self.assertEqual(p["profile"], first["profile"])
        self.assertEqual(len(p["diagram"]["nodes"]), 3)
        self.assertEqual(p["prd_links"][0]["requirement"], "Take turns")
        self.assertEqual(p["decisions"][0]["serves"], ["Take turns"])


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.base = arch.parse_proposal(reply_text())

    def test_small_changes_are_tweaks_and_foundation_changes_are_overhauls(self):
        self.assertEqual(arch.change_kind(None, self.base), "initial")
        tweak = {**self.base, "packages": self.base["packages"] + [{"name": "GRDB", "ecosystem": "swiftpm", "purpose": "offline"}]}
        self.assertEqual(arch.change_kind(self.base, tweak), "tweak")
        renamed = {**self.base, "chosen": "Current architecture"}  # same stack under a new name is still the same choice
        self.assertEqual(arch.change_kind(self.base, renamed), "tweak")
        self.assertEqual(arch.change_kind(self.base, {**self.base, "stack": {**self.base["stack"], "language": "kotlin"}}), "overhaul")
        self.assertEqual(arch.change_kind(self.base, {**self.base, "stack": {**self.base["stack"], "framework": "UIKit"}}), "overhaul")
        self.assertEqual(arch.change_kind(self.base, {**self.base, "profile": {"shape": {"client_server": 100}}}), "overhaul")

    def test_what_changed_lists_plain_differences(self):
        after = {**self.base, "packages": [], "decisions": [{**self.base["decisions"][0], "choice": "Firebase"}],
                 "stack": {**self.base["stack"], "test_command": "swift test"}}
        lines = arch.what_changed(self.base, after)
        self.assertIn("Test command: xcodebuild test -scheme WordDuel → swift test", lines)
        self.assertIn("Package removed: pointfreeco/swift-snapshot-testing", lines)
        self.assertIn("Decision changed: Sync: CloudKit → Firebase", lines)
        self.assertEqual(arch.what_changed(None, after), [])

    def test_versions_bump_minor_for_tweaks_and_major_for_overhauls(self):
        self.assertEqual(arch.next_version(None, "initial"), "1.0")
        self.assertEqual(arch.next_version({"version": "1.3"}, "tweak"), "1.4")
        self.assertEqual(arch.next_version({"version": "1.3"}, "overhaul"), "2.0")
        self.assertEqual(arch.next_version({"version": "garbled"}, "tweak"), "1.1")

    def test_describe_mode_accepts_a_single_renamed_candidate(self):
        p = arch.parse_proposal(reply_text(candidates=[{"name": "Current architecture", "fit": 60}], chosen="What exists"), "describe")
        self.assertEqual((p["chosen"], p["mode"]), ("Current architecture", "describe"))

    def test_the_prompt_changes_with_the_kind_of_proposal(self):
        base = dict(prd_text="A game", platforms=["iOS app"], preferences="", tools=[], platform_needs={}, web=True)
        self.assertIn("2 or 3 genuinely different candidates", arch.proposal_prompt(**base))
        described = arch.proposal_prompt(**base, mode="describe", repo_evidence="Detected stack: Swift (Xcode)")
        self.assertIn("EXISTING project", described)
        self.assertIn("Detected stack: Swift (Xcode)", described)
        self.assertIn("best approximation", described)
        tweak = arch.proposal_prompt(**base, mode="tweak", previous=self.base, feedback="Add offline play")
        self.assertIn("SMALL addition", tweak)
        self.assertIn("The current architecture:", tweak)
        self.assertIn("include the current architecture as one of them", arch.proposal_prompt(**base, mode="overhaul", previous=self.base))
        for text in (described, tweak):
            self.assertIn("Style catalog", text)
            self.assertIn("`diagram`", text)
            self.assertIn("`serves`", text)
        with self.assertRaises(arch.ArchitectureError):
            arch.proposal_prompt(**base, mode="rewrite")


class EvidenceTests(unittest.TestCase):
    def test_the_digest_names_languages_folders_manifests_packages_and_entry_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "server").mkdir(parents=True)
            (root / "src" / "server" / "index.ts").write_text("export {}\n", encoding="utf-8")
            (root / "src" / "server" / "routes.ts").write_text("export {}\n", encoding="utf-8")
            (root / "src" / "web").mkdir()
            (root / "src" / "web" / "App.tsx").write_text("export {}\n", encoding="utf-8")
            (root / "node_modules" / "x").mkdir(parents=True)
            (root / "node_modules" / "x" / "skip.js").write_text("", encoding="utf-8")
            (root / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}, "devDependencies": {"vitest": "^1"}}), encoding="utf-8")
            (root / "README.md").write_text("# Shop\nAn online shop.", encoding="utf-8")
            ev = arch.evidence(root)
            self.assertEqual(ev["languages"], {"TypeScript": 3})
            self.assertIn("src/server", ev["folders"])
            self.assertIn("package.json", ev["manifests"])
            self.assertIn("src/server/index.ts", ev["entry_points"])
            self.assertTrue(any("express" in p for p in ev["packages"]))
            text = arch.evidence_text(ev)
            self.assertIn("TypeScript 3", text)
            self.assertIn("An online shop.", text)
            self.assertNotIn("skip.js", text)
            self.assertEqual(arch.default_mode(root), "describe")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {**GIT_ENV, "ORCHESTRATOR_USER_STATE_DIR": str(self.root / "state")})
        self.env.start()
        (self.root / "docs" / "product").mkdir(parents=True)
        md = new_project.render_prd({"name": "Word Duel", "pitch": "A turn-based word game.", "platform": "iOS app", "stack": "SwiftUI"})
        (self.root / "docs" / "product" / "prd.md").write_text(md, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "start"], cwd=self.root, check=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_proposal_is_asked_for_saved_and_approved_with_a_commit_of_only_its_files(self):
        from orchestrator.web import server as ui
        prompts = []

        def research(root, prompt, timeout=0, progress=None):
            prompts.append(prompt)
            return (reply_text() if len(prompts) > 1 else "sorry, no JSON"), True  # first answer unreadable: asked again

        work, commit = ui.architecture_review_job(self.root, research)
        view = commit(work())
        self.assertEqual(len(prompts), 2)
        self.assertIn(arch.REMINDER.strip()[:40], prompts[1])
        self.assertIn("Platforms chosen: iOS app", prompts[0])
        self.assertIn("SwiftUI", prompts[0])
        self.assertEqual(view["proposal"]["status"], "proposed")
        self.assertTrue(view["needs_decision"])

        (self.root / "notes.txt").write_text("unrelated, uncommitted", encoding="utf-8")
        approved = ui.approve_architecture(self.root)
        self.assertTrue(approved["committed"])
        self.assertIsNone(approved["proposal"])
        self.assertFalse(approved["needs_decision"])
        tracked = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.root, capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(tracked), sorted([arch.RECORD_PATH, arch.DOC_PATH]))
        self.assertIn("notes.txt", subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout)

    def test_feedback_revises_the_previous_proposal(self):
        from orchestrator.web import server as ui
        arch.save_proposal(self.root / ".orchestrator", {**arch.parse_proposal(reply_text()), "feedback_history": ["first"]})
        seen = []
        work, commit = ui.architecture_review_job(self.root, lambda r, p, **kw: (seen.append(p) or reply_text(), False), "Add Android")
        view = commit(work())
        self.assertIn("Your previous proposal", seen[0])
        self.assertIn("Add Android", seen[0])
        self.assertEqual(view["proposal"]["feedback_history"], ["first", "Add Android"])

    def test_an_imported_project_is_described_from_its_code_without_needing_requirements(self):
        from orchestrator.web import server as ui
        (self.root / "docs" / "product" / "prd.md").write_text(new_project.prd.template("X"), encoding="utf-8")
        (self.root / "Sources").mkdir()
        (self.root / "Sources" / "App.swift").write_text("@main struct A {}\n", encoding="utf-8")
        seen = []
        work, commit = ui.architecture_review_job(self.root, lambda r, p, **kw: (seen.append(p) or reply_text(candidates=[REPLY["candidates"][0]]), False))
        view = commit(work())
        self.assertIn("EXISTING project", seen[0])
        self.assertIn("Sources/App.swift", seen[0])
        self.assertEqual((view["proposal"]["mode"], view["proposal"]["kind"], view["default_mode"]), ("describe", "initial", "describe"))
        self.assertTrue(ui.approve_architecture(self.root)["committed"])
        self.assertEqual(arch.load_decision(self.root)["origin"], "described")

    def test_tweaks_need_an_approved_decision_and_a_note_and_are_classed_by_content(self):
        from orchestrator.web import server as ui
        with self.assertRaises(ui.UIError):
            ui.architecture_review_job(self.root, lambda *a, **k: ("", False), "Add offline", "tweak")
        arch.approve(self.root, self.root / ".orchestrator", arch.parse_proposal(reply_text()))
        with self.assertRaises(ui.UIError):
            ui.architecture_review_job(self.root, lambda *a, **k: ("", False), "", "tweak")
        with self.assertRaises(ui.UIError):
            ui.architecture_review_job(self.root, lambda *a, **k: ("", False), "x", "rewrite")
        seen = []
        bigger = reply_text(stack={"language": "typescript", "framework": "React Native"}, chosen="React Native + Firebase")
        work, commit = ui.architecture_review_job(self.root, lambda r, p, **kw: (seen.append(p) or bigger, False), "Add offline play", "tweak")
        view = commit(work())
        self.assertIn("SMALL addition", seen[0])
        self.assertIn("Add offline play", seen[0])
        self.assertEqual(view["proposal"]["kind"], "overhaul")  # the model called it a tweak; the content says otherwise
        self.assertTrue(any(c.startswith("Primary language") for c in view["proposal"]["changes"]))
        self.assertEqual(view["proposal"]["note"], "Add offline play")

    def test_an_empty_product_description_is_refused(self):
        from orchestrator.web import server as ui
        (self.root / "docs" / "product" / "prd.md").write_text(new_project.prd.template("X"), encoding="utf-8")
        with self.assertRaises(ui.UIError):
            ui.architecture_review_job(self.root, lambda *a, **k: ("", False))


if __name__ == "__main__":
    unittest.main()


class PageTests(unittest.TestCase):
    """The architecture lives on the Product page, above History, and is the new-project wizard's next step."""
    STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"

    def setUp(self):
        self.app = (self.STATIC / "app.js").read_text(encoding="utf-8")

    def test_it_is_a_section_of_the_product_page_and_old_links_land_there(self):
        self.assertIn('id="sec-architecture"', self.app)
        self.assertIn('mountArchitecture($("#arch-section"))', self.app)
        self.assertIn('parts[0] === "architecture") return { page: "product"', self.app)
        self.assertNotIn("pages.architecture", self.app)
        self.assertNotIn('href="#/architecture"', (self.STATIC / "index.html").read_text(encoding="utf-8"))

    def test_only_the_product_page_shows_it_above_history_and_the_wizard_points_there(self):
        self.assertNotIn("archStripHtml", self.app)  # not on Home
        self.assertNotIn('api("architecture/summary")', self.app)
        self.assertLess(self.app.index('id="sec-architecture"'), self.app.index("<h2>Keeping it up to date</h2>"))
        self.assertLess(self.app.index("<h2>Keeping it up to date</h2>"), self.app.index("<h2>History</h2>"))
        self.assertIn('href="#/product?section=architecture">Choose the architecture</a>', self.app)

    def test_tweaks_and_overhauls_are_separate_actions(self):
        self.assertIn('mode: "tweak", feedback: note', self.app)
        self.assertIn('mode: "overhaul", feedback: note', self.app)
        self.assertIn('data-mode="describe"', self.app)
        self.assertIn('data-mode="initial"', self.app)

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not installed")
    def test_the_diagram_draws_one_box_per_node_in_columns_by_kind(self):
        start = self.app.index("const ARCH_COLUMNS")
        end = self.app.index("// The weighted style mix")
        script = ("const esc = (s) => String(s).replace(/[&<>\"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'})[c]);\n"
                  + self.app[start:end] + "\n"
                  + "const d = " + json.dumps(arch.parse_proposal(reply_text())["diagram"]) + ";\n"
                  + "process.stdout.write(JSON.stringify({svg: archDiagramSvg(d), empty: archDiagramSvg({nodes: []}),"
                  + " evil: archDiagramSvg({nodes: [{id: 'x', label: '<script>', kind: 'app'}], edges: []})}));")
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        svg = out["svg"]
        self.assertEqual(svg.count("<rect"), 3)
        self.assertEqual(svg.count("<line"), 2)
        self.assertIn("arch-node-external", svg)
        self.assertIn(">syncs<", svg)
        self.assertIn("who uses it → what they open → outside services", svg)
        self.assertEqual(out["empty"], "")
        self.assertNotIn("<script>", out["evil"])


class WorkingAssumptionTests(unittest.TestCase):
    """Soft and skippable: skipping keeps a working assumption, which can be approved, replaced, refreshed or changed later."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rt = self.root / ".orchestrator"
        (self.root / "docs" / "product").mkdir(parents=True)
        (self.root / "docs" / "product" / "prd.md").write_text("# W\n\n## Pitch\n\nA game.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_skipping_an_empty_project_records_a_placeholder_that_jobs_do_not_follow(self):
        record = arch.skip(self.root, self.rt, prd_text="A game.")
        self.assertEqual((record["status"], record["origin"], record["version"], record["chosen"]), ("provisional", "inferred", "0.1", "Not decided"))
        self.assertEqual(record["stack"]["language"], "generic")
        self.assertFalse(arch.is_approved(record))
        self.assertFalse(arch.needs_decision(self.root))  # skipped means no nagging
        self.assertIn("not decided yet", arch.context_block(self.root, "planner"))  # a placeholder isn't something to follow
        self.assertEqual(arch.context_block(self.root, "builder"), "")
        self.assertIsNone(arch.detected_stack_fields(self.root))
        self.assertIn("Working assumption, not approved", arch.doc_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(arch.summary(self.root)["state"], "provisional")

    def test_skipping_a_proposal_keeps_its_recommendation_as_the_working_assumption(self):
        arch.save_proposal(self.rt, arch.parse_proposal(reply_text()))
        record = arch.skip(self.root, self.rt)
        self.assertEqual((record["status"], record["origin"], record["chosen"]), ("provisional", "proposed", "Native SwiftUI + CloudKit"))
        self.assertIsNone(arch.load_proposal(self.rt))
        planner = arch.context_block(self.root, "planner")
        self.assertIn("## Architecture (working assumption", planner)
        self.assertIn("plan the better choice and say so", planner)
        self.assertIn("working assumption", arch.context_block(self.root, "builder"))
        self.assertIn("without saying why", arch.context_block(self.root, "verifier"))
        self.assertEqual(detect_project_stack(self.root).language, "swift")  # a chosen option still steers detection
        self.assertEqual(arch.skip(self.root, self.rt)["version"], "0.2")

    def test_approving_over_a_working_assumption_makes_version_one(self):
        arch.save_proposal(self.rt, arch.parse_proposal(reply_text()))
        arch.skip(self.root, self.rt)
        tweak = {**arch.parse_proposal(reply_text()), "note": "Add analytics"}
        record = arch.approve(self.root, self.rt, tweak)
        self.assertEqual((record["status"], record["version"], record["origin"]), ("approved", "1.0", "proposed"))
        self.assertEqual([h["kind"] for h in record["history"]], ["working assumption", "approved"])
        self.assertIn("## Architecture (approved", arch.context_block(self.root, "planner"))

    def test_the_working_assumption_can_be_approved_as_it_is(self):
        with self.assertRaises(arch.ArchitectureError):
            arch.approve_current(self.root, self.rt)
        (self.root / "main.py").write_text("print(1)\n", encoding="utf-8")
        arch.skip(self.root, self.rt)
        record = arch.approve_current(self.root, self.rt)
        self.assertEqual((record["status"], record["version"], record["origin"]), ("approved", "1.0", "described"))
        with self.assertRaises(arch.ArchitectureError):
            arch.approve_current(self.root, self.rt)  # nothing provisional left

    def test_skipping_never_replaces_an_approved_decision(self):
        approved = arch.approve(self.root, self.rt, arch.parse_proposal(reply_text()))
        arch.save_proposal(self.rt, arch.parse_proposal(reply_text(stack={"language": "go"})))
        self.assertEqual(arch.skip(self.root, self.rt)["version"], approved["version"])
        self.assertTrue(arch.is_approved(arch.load_decision(self.root)))
        self.assertIsNone(arch.load_proposal(self.rt))

    def test_an_inferred_assumption_reads_the_code_and_notices_when_it_changes(self):
        arch.skip(self.root, self.rt)
        self.assertFalse(arch.inferred_is_stale(self.root, arch.load_decision(self.root)))
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}), encoding="utf-8")
        (self.root / "server.js").write_text("require('express')\n", encoding="utf-8")
        self.assertTrue(arch.inferred_is_stale(self.root, arch.load_decision(self.root)))
        self.assertTrue(arch.summary(self.root)["stale"])
        refreshed = arch.skip(self.root, self.rt)
        self.assertEqual(refreshed["stack"]["language"], "javascript")
        self.assertIn("JavaScript", refreshed["summary"])
        self.assertTrue(any(p["name"] == "express" for p in refreshed["packages"]))
        self.assertFalse(arch.inferred_is_stale(self.root, refreshed))
        self.assertEqual(detect_project_stack(self.root).language, "javascript")  # detection, not the record
        self.assertFalse(arch.inferred_is_stale(self.root, arch.parse_proposal(reply_text())))  # only inferred ones go stale

    def test_a_working_assumption_can_be_tweaked_like_a_decision(self):
        from orchestrator.web import server as ui
        arch.save_proposal(self.rt, arch.parse_proposal(reply_text()))
        arch.skip(self.root, self.rt)
        seen = []
        work, commit = ui.architecture_review_job(self.root, lambda r, p, **kw: (seen.append(p) or reply_text(), False), "Add push", "tweak")
        view = commit(work())
        self.assertIn("The current architecture:", seen[0])
        self.assertEqual((view["proposal"]["kind"], view["approved"]), ("tweak", False))


class SkipServerTests(ServerTests):
    """Skipping and approving through the server commit only the architecture files."""

    def committed_files(self):
        return subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.root, capture_output=True, text=True).stdout.split()

    def test_skip_commits_a_working_assumption_and_approve_promotes_it(self):
        from orchestrator.web import server as ui
        view = ui.skip_architecture(self.root)
        self.assertTrue(view["committed"])
        self.assertFalse(view["approved"])
        self.assertEqual(sorted(self.committed_files()), sorted([arch.RECORD_PATH, arch.DOC_PATH]))
        self.assertEqual(view["decision"]["status"], "provisional")
        (self.root / "app.py").write_text("print(1)\n", encoding="utf-8")
        ui.skip_architecture(self.root)  # refreshed from the code: now python, so worth approving
        approved = ui.approve_architecture(self.root)
        self.assertTrue(approved["committed"] and approved["approved"])
        self.assertEqual(approved["decision"]["version"], "1.0")
        self.assertIn("app.py", subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout)

    def test_skipping_a_change_to_an_approved_decision_only_discards_it(self):
        from orchestrator.web import server as ui
        arch.approve(self.root, self.root / ".orchestrator", arch.parse_proposal(reply_text()))
        arch.save_proposal(self.root / ".orchestrator", arch.parse_proposal(reply_text()))
        view = ui.skip_architecture(self.root)
        self.assertFalse(view["committed"])
        self.assertTrue(view["approved"])
        self.assertIsNone(view["proposal"])

    def test_approving_with_nothing_waiting_or_provisional_is_refused(self):
        from orchestrator.web import server as ui
        with self.assertRaises(ui.UIError):
            ui.approve_architecture(self.root)


class SkipPageTests(PageTests):
    def test_every_entry_point_can_be_skipped(self):
        self.assertEqual(self.app.count('id="arch-skip"'), 1)  # waiting proposal ("Decide later"); the Product card doesn't need Skip for now
        self.assertIn('id="np-arch-skip"', self.app)
        self.assertIn('api("architecture/skip"', self.app)
        self.assertIn('post("architecture/skip"', self.app)

    def test_working_assumptions_are_labelled_and_can_be_approved_or_refreshed(self):
        self.assertIn("Working assumption · v", self.app)
        self.assertIn('id="arch-approve-current"', self.app)
        self.assertIn('id="arch-refresh"', self.app)


class DocumentationTests(unittest.TestCase):
    """Every part of the architecture step is documented: the reference covers each mode, status, style, box kind, file and
    route; the user guide (both copies) covers what a person sees and does."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.reference = (self.ROOT / "docs" / "architecture-step.md").read_text(encoding="utf-8")
        self.guides = [(self.ROOT / p).read_text(encoding="utf-8") for p in ("docs/user-guide.md", "orchestrator/web/static/docs/user-guide.md")]

    def test_the_reference_covers_the_whole_model(self):
        for name in [*arch.MODES, *arch.STATUSES, *arch.CATEGORIES, *arch.STYLES, *arch.DIAGRAM_KINDS]:
            self.assertIn(f"`{name}`", self.reference, name)
        for style in arch.STYLES.values():
            self.assertIn(style["name"], self.reference)
        for path in (arch.RECORD_PATH, arch.DOC_PATH, f".orchestrator/{arch.PROPOSAL_FILE}"):
            self.assertIn(path, self.reference)
        for fn in ("normalize_profile", "normalize_diagram", "mermaid", "change_kind", "what_changed", "evidence", "infer_record",
                   "inferred_is_stale", "prd_fingerprint", "prd_changed", "revisit_reasons", "context_block", "detected_stack_fields",
                   "default_mode", "is_codeless", "parse_proposal", "platforms_from_prd", "available_tools"):
            self.assertTrue(callable(getattr(arch, fn)), fn)
            self.assertIn(f"`{fn}`", self.reference, fn)
        self.assertIn(str(arch.MAX_NODES), self.reference)
        self.assertIn(str(arch.MAX_EDGES), self.reference)

    def test_the_reference_lists_every_route_the_server_has(self):
        from orchestrator.web import server as ui
        source = Path(ui.__file__).read_text(encoding="utf-8")
        routes = set(re.findall(r'parts == \["architecture"(?:, "([a-z]+)")?\]', source))
        self.assertEqual(routes, {"", "summary", "propose", "edit", "approve", "skip", "discard"})
        for route in routes:
            self.assertIn(f"/api/architecture{'/' + route if route else ''}`", self.reference, route)

    def test_both_user_guides_explain_what_a_person_sees_and_does(self):
        for guide in self.guides:
            section = guide[guide.index("## Architecture"):guide.index("## Packages")]
            for phrase in ("Choose the architecture", "Describe the architecture", "Skip for now", "Decide later", "working assumption",
                           "Approve as it is", "Refresh from the code", "Best for", "Fit for your product", "diagram", "Good and Watch out",
                           "Style mix", "How it serves your product requirements", "Not covered yet", "Check it against the requirements",
                           "Add or adjust", "Rethink the architecture", "What would change", "History", "docs/architecture-step.md",
                           arch.RECORD_PATH, arch.DOC_PATH, "1.3 to 1.4", "1.3 to 2.0", "Home"):
                self.assertIn(phrase, section, phrase)


class EdgeCaseTests(unittest.TestCase):
    def test_an_xcode_project_alone_counts_as_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Game.xcodeproj").mkdir()
            self.assertFalse(arch.is_codeless(Path(tmp)))

    def test_a_huge_tree_is_not_treated_as_a_new_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(6):
                (Path(tmp) / f"note{i}.txt").write_text("x", encoding="utf-8")
            self.assertFalse(arch.is_codeless(Path(tmp), limit=5))
            self.assertTrue(arch.is_codeless(Path(tmp), limit=50))

    def test_the_summary_can_be_edited_before_approving(self):
        p = arch.edit_proposal(arch.parse_proposal(reply_text()), {"summary": "  Short   and plain.  "})
        self.assertEqual(p["summary"], "Short and plain.")

    def test_malformed_model_output_is_cleaned_rather_than_trusted(self):
        p = arch.parse_proposal(reply_text(
            candidates=["junk", {"name": ""}, {"name": "Only one", "fit": "high", "profile": {"shape": {"client_only": "lots"}},
                                               "diagram": {"nodes": ["junk", {"label": "App", "kind": "app"}], "edges": ["junk"]}}],
            chosen="something else", layers="not a list", decisions=[{"topic": "T", "choice": "C", "alternatives": "x"}]))
        self.assertEqual(p["chosen"], "Only one")  # a single option is the choice even when misnamed
        self.assertIsNone(p["candidates"][0]["fit"])
        self.assertEqual(p["candidates"][0]["profile"], {})
        self.assertEqual(p["diagram"]["nodes"], [{"id": "app", "label": "App", "kind": "app"}])
        self.assertEqual(p["layers"], [])
        self.assertEqual(p["decisions"][0]["alternatives"], [])
        with self.assertRaises(arch.ArchitectureError):
            arch.parse_proposal("```json\n[1, 2]\n```")
        with self.assertRaises(arch.ArchitectureError):
            arch.parse_proposal("{not json}")
