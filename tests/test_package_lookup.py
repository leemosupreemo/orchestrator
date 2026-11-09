"""Adding a package from a pasted link: what it is, read from its registry or from the Git repository itself."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import package_lookup as L  # noqa: E402

PACKAGE_SWIFT = '''// swift-tools-version:5.9
let package = Package(
  name: "swift-snapshot-testing",
  products: [
    .library(name: "SnapshotTesting", targets: ["SnapshotTesting"]),
    .library(name: "InlineSnapshotTesting", targets: ["InlineSnapshotTesting"]),
  ])'''
PODSPEC = "Pod::Spec.new do |s|\n  s.name = 'SnapshotTesting'\n  s.summary = 'Snapshot testing.'\nend"


def fake_git(files: dict[str, str], tags: str = "abc\trefs/tags/1.2.0\nabc\trefs/tags/v1.10.1\nabc\trefs/tags/2.0.0-beta\n"):
    calls = []

    def run(*args, cwd=None):
        calls.append(args)
        if args[0] == "ls-remote":
            return tags
        if args[0] == "clone":
            return ""
        if args[0] == "ls-tree":
            return "\n".join(files)
        if args[0] == "show":
            return files[args[1].split(":", 1)[1]]
        raise AssertionError(args)
    run.calls = calls
    return run


class RepositoryAddressTests(unittest.TestCase):
    def test_any_common_way_of_writing_a_repository_becomes_its_https_address(self):
        self.assertEqual(L.repository_url("git@github.com:Alamofire/Alamofire.git"), "https://github.com/Alamofire/Alamofire")
        self.assertEqual(L.repository_url("https://github.com/firebase/flutterfire/tree/main/packages"), "https://github.com/firebase/flutterfire")
        self.assertEqual(L.repository_url("gitlab.com/group/project/-/blob/main/README.md"), "https://gitlab.com/group/project")
        self.assertEqual(L.repository_url("https://git.example.com/team/lib.git"), "https://git.example.com/team/lib")
        self.assertEqual(L.repository_url("not a link"), "")
        self.assertEqual(L.repository_url("file:///etc/passwd"), "")

    def test_the_latest_release_tag_is_the_version_like_xcode(self):
        self.assertEqual(L.latest_tag("a\trefs/tags/1.2.0\na\trefs/tags/v1.10.1\na\trefs/tags/2.0.0-beta\n"), "1.10.1")
        self.assertEqual(L.latest_tag(""), "")


class RepositoryLookupTests(unittest.TestCase):
    def test_a_swift_package_is_found_from_its_repository(self):
        run = fake_git({"Package.swift": PACKAGE_SWIFT, "SnapshotTesting.podspec": PODSPEC, "README.md": ""})
        found = L.from_repository("https://github.com/pointfreeco/swift-snapshot-testing", run)
        self.assertEqual(found["ecosystem"], "swiftpm")
        self.assertEqual(found["name"], "pointfreeco/swift-snapshot-testing")  # added by its address, as SwiftPM does
        self.assertEqual(found["version"], "1.10.1")
        self.assertEqual(found["products"], "SnapshotTesting, InlineSnapshotTesting")
        self.assertEqual(found["ecosystems"], ["swiftpm", "cocoapods"])
        self.assertTrue(all("--" in c for c in run.calls if c[0] in ("ls-remote", "clone")))  # the address can't be read as an option

    def test_the_projects_own_package_manager_wins_when_the_repository_offers_it(self):
        run = fake_git({"Package.swift": PACKAGE_SWIFT, "SnapshotTesting.podspec": PODSPEC})
        found = L.from_repository("https://github.com/pointfreeco/swift-snapshot-testing", run, prefer=["cocoapods"])
        self.assertEqual((found["ecosystem"], found["name"], found["purpose"]), ("cocoapods", "SnapshotTesting", "Snapshot testing."))

    def test_other_ecosystems_are_read_from_their_manifests(self):
        cases = {
            "package.json": (json.dumps({"name": "left-pad", "description": "Pads strings"}), ("npm", "left-pad")),
            "Cargo.toml": ('[package]\nname = "serde"\ndescription = "Serialization"\n', ("cargo", "serde")),
            "pyproject.toml": ('[project]\nname = "requests"\ndescription = "HTTP"\n', ("pip", "requests")),
            "go.mod": ("module github.com/spf13/cobra\n\ngo 1.21\n", ("go", "github.com/spf13/cobra")),
            "pubspec.yaml": ("name: provider\ndescription: State management\n", ("dart", "provider")),
        }
        for filename, (text, expected) in cases.items():
            with self.subTest(filename):
                found = L.from_repository("https://example.com/a/b", fake_git({filename: text}))
                self.assertEqual((found["ecosystem"], found["name"]), expected)
        unity = L.from_repository("https://example.com/a/b", fake_git({"package.json": json.dumps({"name": "com.x.y", "unity": "2021.3"})}))
        self.assertEqual(unity["ecosystem"], "unity")

    def test_a_private_or_missing_repository_says_so_plainly(self):
        def run(*args, cwd=None):
            raise L.PackageLookupError("fatal: could not read Username for 'https://bitbucket.org': terminal prompts disabled")
        with self.assertRaisesRegex(L.PackageLookupError, "private or doesn't exist"):
            L.from_repository("https://bitbucket.org/a/b", run)


class RegistryLookupTests(unittest.TestCase):
    def test_a_registry_page_asks_the_registrys_api(self):
        replies = {"https://registry.npmjs.org/lodash": {"dist-tags": {"latest": "4.17.21"}, "description": "Utilities",
                                                         "repository": {"url": "git+https://github.com/lodash/lodash.git"}}}
        found = L.lookup("https://www.npmjs.com/package/lodash", fetch=lambda url: replies[url])
        self.assertEqual((found["name"], found["ecosystem"], found["version"], found["purpose"]), ("lodash", "npm", "4.17.21", "Utilities"))
        self.assertEqual(found["source"], "https://github.com/lodash/lodash")

    def test_a_registry_that_doesnt_answer_still_gives_the_name_and_ecosystem(self):
        def down(url):
            raise OSError("offline")
        found = L.lookup("https://pypi.org/project/requests/", fetch=down)
        self.assertEqual((found["name"], found["ecosystem"]), ("requests", "pip"))
        self.assertIn("didn't answer", found["error"])

    def test_something_that_isnt_a_link_is_explained(self):
        with self.assertRaisesRegex(L.PackageLookupError, "registry page"):
            L.lookup("just some words")


class AddFormTests(unittest.TestCase):
    def test_the_add_form_fills_itself_in_from_the_link(self):
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn("api(`packages/lookup?url=${encodeURIComponent(url)}`)", app)
        self.assertIn("wirePackageLookup(data);", app)
        self.assertIn('id="pkg-review">Recommend packages</button>', app)
        self.assertNotIn("Find packages with AI", app)


if __name__ == "__main__":
    unittest.main()
