from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import packages as pk


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class DetectTests(unittest.TestCase):
    def project(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        write(root, "package.json", '{"dependencies": {"react": "^18.2.0"}, "devDependencies": {"vitest": "^1.0.0"}}')
        write(root, "requirements.txt", "requests==2.31.0\nPyYAML>=6 # yaml\n-r other.txt\nflask[async]>=3 ; python_version>'3.8'\n./local\n")
        write(root, "requirements-dev.txt", "pytest>=8\n")
        write(root, "pyproject.toml", '[project]\ndependencies = ["httpx>=0.27", "rich"]\n[project.optional-dependencies]\ndev = ["ruff"]\n'
                                      '[tool.poetry.dependencies]\npython = "^3.11"\nattrs = "^23"\n')
        write(root, "Package.swift", 'let package = Package(name: "A", dependencies: [\n .package(url: "https://github.com/pointfreeco/swift-snapshot-testing.git", from: "1.17.0"),\n'
                                      ' .package(url: "https://github.com/Quick/Nimble.git", .upToNextMajor(from: "13.0.0")),\n .package(url: "https://github.com/x/y", branch: "main")])')
        write(root, "Cargo.toml", '[dependencies]\nserde = "1.0"\ntokio = { version = "1", features = ["full"] }\n[dev-dependencies]\ncriterion = "0.5"\n')
        write(root, "go.mod", "module x\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n\tgolang.org/x/sys v0.1.0 // indirect\n)\nrequire github.com/spf13/cobra v1.8.0\n")
        write(root, "Gemfile", "source 'https://rubygems.org'\ngem 'rails', '~> 7.0'\ngem \"puma\"\n")
        write(root, "Podfile", "pod 'Alamofire', '~> 5.8'\n")
        write(root, "app/build.gradle", 'dependencies {\n  implementation "com.squareup.okhttp3:okhttp:4.12.0"\n  testImplementation("junit:junit:4.13.2")\n  id "x"\n}\n')
        write(root, "Packages/manifest.json", '{"dependencies": {"com.unity.textmeshpro": "3.0.6"}}')
        write(root, "pubspec.yaml", "name: x\ndependencies:\n  flutter:\n    sdk: flutter\n  http: ^1.1.0\ndev_dependencies:\n  lints: ^3.0.0\n")
        write(root, "node_modules/left-pad/package.json", '{"dependencies": {"should-not": "1"}}')  # never read
        return root

    def test_reads_every_supported_manifest(self):
        found = {(i["ecosystem"], i["name"]): i for i in pk.detect(self.project())}
        expected = {
            ("npm", "react"): ("^18.2.0", "runtime"), ("npm", "vitest"): ("^1.0.0", "dev"),
            ("pip", "requests"): ("==2.31.0", "runtime"), ("pip", "PyYAML"): (">=6", "runtime"), ("pip", "flask"): (">=3", "runtime"),
            ("pip", "pytest"): (">=8", "dev"), ("pip", "httpx"): (">=0.27", "runtime"), ("pip", "ruff"): ("", "dev"), ("pip", "attrs"): ("^23", "runtime"),
            ("swiftpm", "swift-snapshot-testing"): ("1.17.0", "runtime"), ("swiftpm", "Nimble"): ("13.0.0", "runtime"),
            ("cargo", "serde"): ("1.0", "runtime"), ("cargo", "tokio"): ("1", "runtime"), ("cargo", "criterion"): ("0.5", "dev"),
            ("go", "github.com/gin-gonic/gin"): ("v1.9.1", "runtime"), ("go", "github.com/spf13/cobra"): ("v1.8.0", "runtime"),
            ("ruby", "rails"): ("~> 7.0", "runtime"), ("ruby", "puma"): ("", "runtime"), ("cocoapods", "Alamofire"): ("~> 5.8", "runtime"),
            ("gradle", "com.squareup.okhttp3:okhttp"): ("4.12.0", "runtime"), ("gradle", "junit:junit"): ("4.13.2", "dev"),
            ("unity", "com.unity.textmeshpro"): ("3.0.6", "runtime"), ("dart", "http"): ("^1.1.0", "runtime"), ("dart", "lints"): ("^3.0.0", "dev"),
        }
        for key, (version, kind) in expected.items():
            self.assertIn(key, found, key)
            self.assertEqual((found[key]["version"], found[key]["kind"]), (version, kind), key)
        self.assertNotIn(("npm", "should-not"), found)  # vendored folders are skipped
        self.assertNotIn(("go", "golang.org/x/sys"), found)  # indirect dependencies aren't the project's choice
        self.assertNotIn(("dart", "flutter"), found)
        self.assertEqual(found[("gradle", "junit:junit")]["manifest"], "app/build.gradle")
        self.assertEqual(found[("unity", "com.unity.textmeshpro")]["manifest"], "Packages/manifest.json")

    def test_a_branch_dependency_keeps_its_requirement_and_an_empty_project_has_nothing(self):
        found = {i["name"]: i for i in pk.detect(self.project())}
        self.assertIn("branch", found["y"]["version"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pk.detect(Path(tmp)), [])


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / ".orchestrator"

    def test_add_edit_status_and_remove(self):
        record = pk.add(self.runtime, {"name": "lodash", "ecosystem": "javascript", "purpose": "Utilities", "url": "https://lodash.com", "feature": "lobby"})
        self.assertEqual((record["id"], record["ecosystem"], record["status"], record["origin"]), ("npm-lodash", "npm", "planned", "manual"))
        edited = pk.update(self.runtime, "npm-lodash", {"version": "4.17.21", "notes": "Pin it"})
        self.assertEqual((edited["version"], edited["notes"], edited["purpose"]), ("4.17.21", "Pin it", "Utilities"))  # others keep their value
        self.assertEqual(pk.set_status(self.runtime, "npm-lodash", "dismissed")["status"], "dismissed")
        pk.remove(self.runtime, "npm-lodash")
        self.assertEqual(pk.load(self.runtime)["packages"], [])

    def test_bad_input_and_duplicates_are_refused(self):
        for raw in ({"name": "", "ecosystem": "npm"}, {"name": "has space", "ecosystem": "npm"}, {"name": "x", "ecosystem": "cobol"}):
            with self.assertRaises(pk.PackageError, msg=raw):
                pk.add(self.runtime, raw)
        pk.add(self.runtime, {"name": "Requests", "ecosystem": "pip"})
        with self.assertRaises(pk.PackageError):
            pk.add(self.runtime, {"name": "requests", "ecosystem": "python"})  # same package, however it is spelled
        with self.assertRaises(pk.PackageError):
            pk.set_status(self.runtime, "pip-requests", "installed")
        with self.assertRaises(pk.PackageError):
            pk.update(self.runtime, "nope", {"notes": "x"})
        self.assertEqual(pk.add(self.runtime, {"name": "x", "ecosystem": "npm", "url": "javascript:alert(1)"})["url"], "")  # only https links

    def test_suggestions_skip_what_the_project_has_or_the_person_decided(self):
        pk.add(self.runtime, {"name": "zod", "ecosystem": "npm"})
        added = pk.merge_suggestions(self.runtime, [{"name": "zod", "ecosystem": "npm", "purpose": "", "why": "", "feature": "", "url": "", "version": ""},
                                                    {"name": "dayjs", "ecosystem": "npm", "purpose": "Dates", "why": "Needs dates", "feature": "lobby", "url": "https://day.js.org", "version": "1.11"}],
                                     {"at": "2026-10-19T10:00:00", "searched": True})
        self.assertEqual([r["name"] for r in added], ["dayjs"])
        self.assertEqual((added[0]["status"], added[0]["origin"]), ("suggested", "ai"))
        self.assertEqual(pk.load(self.runtime)["review"]["searched"], True)

    def test_overview_marks_what_a_manifest_already_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "requirements.txt", "PyYAML==6.0\n")
            pk.add(self.runtime, {"name": "pyyaml", "ecosystem": "pip"})
            pk.add(self.runtime, {"name": "rich", "ecosystem": "pip"})
            view = pk.overview(root, self.runtime)
        self.assertEqual({p["name"]: p["installed"] for p in view["packages"]}, {"pyyaml": True, "rich": False})  # PyYAML == pyyaml
        self.assertEqual([i["name"] for i in view["installed"]], ["PyYAML"])


class LinkTests(unittest.TestCase):
    def test_a_registry_link_says_the_name_and_ecosystem(self):
        cases = {
            "https://www.npmjs.com/package/lodash": ("lodash", "npm"), "https://www.npmjs.com/package/@types/node?activeTab=versions": ("@types/node", "npm"),
            "https://pypi.org/project/requests/": ("requests", "pip"), "https://crates.io/crates/serde": ("serde", "cargo"),
            "https://rubygems.org/gems/rails": ("rails", "ruby"), "https://pub.dev/packages/http": ("http", "dart"),
            "https://cocoapods.org/pods/Alamofire": ("Alamofire", "cocoapods"), "https://pkg.go.dev/github.com/gin-gonic/gin@v1.9.1": ("github.com/gin-gonic/gin", "go"),
            "https://mvnrepository.com/artifact/com.squareup.okhttp3/okhttp": ("com.squareup.okhttp3:okhttp", "gradle"),
            "https://openupm.com/packages/com.cysharp.unitask/": ("com.cysharp.unitask", "unity"),
            "https://github.com/pointfreeco/swift-snapshot-testing": ("pointfreeco/swift-snapshot-testing", ""),
            "https://github.com/Quick/Nimble.git": ("Quick/Nimble", ""), "github.com/x/y/tree/main/docs": ("x/y", ""),
        }
        for url, (name, ecosystem) in cases.items():
            self.assertEqual(pk.from_url(url), {"name": name, "ecosystem": ecosystem}, url)

    def test_other_links_are_not_guessed(self):
        for url in ("", None, "https://example.com/some/page", "not a link", "https://github.com/onlyowner"):
            self.assertIsNone(pk.from_url(url), url)


class AddByLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / ".orchestrator"

    def test_a_link_alone_is_enough_and_keeps_the_link(self):
        record = pk.add(self.runtime, {"url": "https://pypi.org/project/httpx/", "purpose": "HTTP calls"})
        self.assertEqual((record["name"], record["ecosystem"], record["url"], record["id"]), ("httpx", "pip", "https://pypi.org/project/httpx/", "pip-httpx"))

    def test_what_the_person_typed_wins_over_the_link(self):
        record = pk.add(self.runtime, {"url": "https://www.npmjs.com/package/lodash", "name": "lodash-es", "ecosystem": "npm"})
        self.assertEqual(record["name"], "lodash-es")
        other = pk.add(self.runtime, {"url": "https://www.npmjs.com/package/dayjs", "ecosystem": "cargo"})  # an explicit choice is respected
        self.assertEqual(other["ecosystem"], "cargo")

    def test_a_github_link_takes_the_projects_only_ecosystem_or_asks(self):
        link = {"url": "https://github.com/pointfreeco/swift-snapshot-testing"}
        with self.assertRaises(pk.PackageError) as ctx:
            pk.add(self.runtime, link)
        self.assertIn("Choose the ecosystem", str(ctx.exception))
        with self.assertRaises(pk.PackageError):
            pk.add(self.runtime, link, ["swiftpm", "npm"])  # two ecosystems: can't tell
        record = pk.add(self.runtime, link, ["swiftpm", "swiftpm"])
        self.assertEqual((record["name"], record["ecosystem"]), ("pointfreeco/swift-snapshot-testing", "swiftpm"))
        self.assertEqual(pk.add(self.runtime, {"url": "https://github.com/Quick/Nimble", "ecosystem": "swiftpm"})["ecosystem"], "swiftpm")  # or the form's choice

    def test_an_unknown_link_needs_a_name(self):
        with self.assertRaises(pk.PackageError) as ctx:
            pk.add(self.runtime, {"url": "https://example.com/lib"})
        self.assertIn("I don't know that link", str(ctx.exception))
        record = pk.add(self.runtime, {"url": "https://example.com/lib", "name": "mylib", "ecosystem": "npm"})
        self.assertEqual(record["url"], "https://example.com/lib")  # a link we can't read is still kept as the package's link


class SizeOfChangeTests(unittest.TestCase):
    BASE = "A turn-based word game to play with friends. " * 4

    def test_only_new_words_count_not_moves_deletions_or_retyping(self):
        self.assertEqual(pk.new_content(self.BASE, self.BASE), 0)
        self.assertEqual(pk.new_content(self.BASE, "  ".join(self.BASE.split()) + "\n\n"), 0)  # whitespace
        self.assertEqual(pk.new_content(self.BASE, ""), 0)  # deleting everything adds nothing to look at
        self.assertEqual(pk.new_content("", "one two three"), len("one two three"))
        self.assertLess(pk.new_content(self.BASE, self.BASE.replace("friends", "mates")), pk.MIN_NEW_CHARS)  # a word swapped

    def test_a_tweak_does_not_qualify_but_about_a_paragraph_does(self):
        tweak = self.BASE + "Add a dark mode toggle."
        paragraph = self.BASE + ("Players can start a daily puzzle that everyone solves once, share their result as a grid of coloured squares, "
                                 "and see a streak counter. Friends can challenge each other to rematches, chat during a match, and keep a "
                                 "leaderboard that resets every week. Purchases are handled with the platform's store, and sign in uses Apple or Google.")
        self.assertFalse(pk.is_substantial(self.BASE, tweak))
        self.assertTrue(pk.is_substantial(self.BASE, paragraph))
        self.assertTrue(pk.is_substantial("", paragraph))  # the first real set of requirements

    def test_small_edits_add_up_because_the_snapshot_stays_the_last_reviewed_text(self):
        lines = ["Players pick a nickname when they join a lobby for the first time.", "A match ends when both players have used all their tiles or passed twice in a row.",
                 "Scores are shown after every move with a short animation of the tiles being counted.", "Each player gets a timer for their turn that they can turn off in settings.",
                 "A finished match can be shared as an image with the final board and both scores."]
        text, verdicts = self.BASE, []
        for line in lines:
            text += line + " "
            verdicts.append(pk.is_substantial(self.BASE, text))
        self.assertEqual(verdicts, [False, False, False, True, True])  # none of them qualified alone; together they do
        self.assertFalse(pk.is_substantial(self.BASE, self.BASE + lines[0]))


class AutomaticReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / ".orchestrator"

    def test_the_fingerprint_ignores_whitespace_and_notices_real_changes(self):
        self.assertEqual(pk.prd_fingerprint("A word  game\n\nfor friends"), pk.prd_fingerprint(" A word game for friends "))
        self.assertNotEqual(pk.prd_fingerprint("A word game"), pk.prd_fingerprint("A word game with sound"))

    def test_the_switch_the_baseline_and_the_running_marker_are_kept(self):
        data = pk.load(self.runtime)
        self.assertEqual((data["auto"], data["prd_hash"], data["running"]), (True, None, ""))  # on by default; nothing looked at yet
        pk.set_auto(self.runtime, False)
        pk.note_prd(self.runtime, "abc123")
        pk.note_prd(self.runtime, "abc123", running="2026-10-19T22:00:00-07:00")
        data = pk.load(self.runtime)
        self.assertEqual((data["auto"], data["prd_hash"], data["running"]), (False, "abc123", "2026-10-19T22:00:00-07:00"))
        pk.note_prd(self.runtime, "def456", error="The model took too long.", snapshot="the text it read")
        data = pk.load(self.runtime)
        self.assertEqual((data["prd_hash"], data["running"], data["review"]["error"], data["prd_snapshot"]), ("def456", "", "The model took too long.", "the text it read"))

    def test_a_finished_review_records_what_it_read_and_clears_the_marker(self):
        pk.note_prd(self.runtime, "old", running="2026-10-19T22:00:00-07:00")
        pk.merge_suggestions(self.runtime, [], {"at": "2026-10-19T22:05:00-07:00", "searched": True, "suggested": 0, "prd_hash": "new", "prd_text": "what it read"})
        data = pk.load(self.runtime)
        self.assertEqual((data["prd_hash"], data["running"], data["auto"], data["prd_snapshot"]), ("new", "", True, "what it read"))
        self.assertNotIn("prd_text", data["review"])  # the text is kept for measuring changes, not shown with the review

    def test_suggestions_waiting_are_counted_and_every_line_has_a_link(self):
        pk.merge_suggestions(self.runtime, [
            {"name": "dayjs", "ecosystem": "npm", "purpose": "", "why": "", "feature": "", "url": "", "version": ""},
            {"name": "x/y", "ecosystem": "swiftpm", "purpose": "", "why": "", "feature": "", "url": "https://example.com/y", "version": ""}], {"at": "t"})
        self.assertEqual(pk.suggested_count(self.runtime), 2)
        pk.set_status(self.runtime, "npm-dayjs", "dismissed")
        self.assertEqual(pk.suggested_count(self.runtime), 1)
        with tempfile.TemporaryDirectory() as tmp:
            links = {p["name"]: p["link"] for p in pk.overview(Path(tmp), self.runtime)["packages"]}
        self.assertEqual(links, {"dayjs": "https://www.npmjs.com/package/dayjs", "x/y": "https://example.com/y"})  # its own link wins

    def test_a_registry_page_stands_in_when_a_package_has_no_link(self):
        cases = {("npm", "@types/node"): "https://www.npmjs.com/package/@types/node", ("pip", "requests"): "https://pypi.org/project/requests",
                 ("cargo", "serde"): "https://crates.io/crates/serde", ("go", "github.com/gin-gonic/gin"): "https://pkg.go.dev/github.com/gin-gonic/gin",
                 ("gradle", "com.squareup.okhttp3:okhttp"): "https://mvnrepository.com/artifact/com.squareup.okhttp3/okhttp",
                 ("swiftpm", "pointfreeco/swift-snapshot-testing"): "https://github.com/pointfreeco/swift-snapshot-testing",
                 ("swiftpm", "Nimble"): "https://swiftpackageindex.com/search?query=Nimble", ("dart", "http"): "https://pub.dev/packages/http",
                 ("unity", "com.cysharp.unitask"): "https://openupm.com/packages/com.cysharp.unitask"}
        for (ecosystem, name), expected in cases.items():
            self.assertEqual(pk.link_for({"ecosystem": ecosystem, "name": name}), expected, (ecosystem, name))
        self.assertEqual(pk.link_for({"ecosystem": "gradle", "name": "no-colon"}), "")


class ResearchTests(unittest.TestCase):
    FEATURES = [{"id": "lobby", "name": "Lobby", "summary": "Friends meet", "paths": ["app/lobby/"]}, {"id": "chat", "name": "Chat", "summary": ""}]
    INSTALLED = [{"name": "react", "ecosystem": "npm", "version": "18", "manifest": "package.json", "kind": "runtime"}]
    ANSWER = json.dumps({"packages": [
        {"name": "socket.io", "ecosystem": "javascript", "purpose": "Realtime", "why": "Lobby needs live seats", "feature": "lobby", "url": "https://socket.io", "version": "4.7"},
        {"name": "react", "ecosystem": "npm", "purpose": "UI", "why": "x"},
        {"name": "zod", "ecosystem": "npm", "purpose": "Validation", "why": "Chat messages", "feature": "Chat", "url": "http://insecure.example"},
        {"name": "bad name!", "ecosystem": "npm"}, {"name": "fortran-lib", "ecosystem": "fortran"}, "junk"],
        "notes": "Checked npm.", "sources": ["https://socket.io/docs", "ftp://x", 7]})

    def test_the_prompt_carries_the_specs_the_stack_and_what_not_to_repeat(self):
        text = pk.review_prompt("A word game with friends.", self.FEATURES, self.INSTALLED, "TypeScript (npm)", web=True,
                                decided=[{"name": "zod", "ecosystem": "npm", "status": "dismissed"}])
        for expected in ("A word game with friends.", "- Lobby: Friends meet [code: app/lobby/]", "npm: react", "npm: zod (dismissed)", "TypeScript (npm)",
                         "Search the web to confirm", "At most 12", "ONLY one JSON object"):
            self.assertIn(expected, text)
        self.assertIn("cannot search the web", pk.review_prompt("", [], [], "", web=False))
        self.assertIn("(none written yet)", pk.review_prompt("", [], [], "", web=False))

    def test_parse_keeps_valid_new_packages_and_matches_features_by_name(self):
        out = pk.parse_suggestions(self.ANSWER, self.FEATURES, self.INSTALLED)
        self.assertEqual([(p["name"], p["ecosystem"], p["feature"]) for p in out["packages"]], [("socket.io", "npm", "lobby"), ("zod", "npm", "chat")])
        self.assertEqual(out["packages"][1]["url"], "")  # not https
        self.assertEqual((out["notes"], out["sources"]), ("Checked npm.", ["https://socket.io/docs"]))

    def test_decided_packages_are_not_suggested_again_and_an_empty_list_is_fine(self):
        out = pk.parse_suggestions(self.ANSWER, self.FEATURES, self.INSTALLED, decided=[{"name": "Socket.IO", "ecosystem": "npm", "status": "dismissed"}])
        self.assertEqual([p["name"] for p in out["packages"]], ["zod"])
        self.assertEqual(pk.parse_suggestions('{"packages": [], "notes": "Nothing needed."}', [], [])["packages"], [])
        self.assertEqual(len(pk.parse_suggestions("```json\n" + self.ANSWER + "\n```", self.FEATURES, [])["packages"]), 3)

    def test_unusable_answers_raise(self):
        for reply in ("none", "{not json", '{"packages": "x"}'):
            with self.assertRaises(pk.PackageError, msg=reply):
                pk.parse_suggestions(reply, [], [])


if __name__ == "__main__":
    unittest.main()
