from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import test_tooling as tooling

MULTILINE = '''// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "App",
    dependencies: [
        .package(url: "https://example.com/other.git", from: "1.0.0")
    ],
    targets: [
        .target(name: "App"),
        .testTarget(
            name: "AppTests",
            dependencies: ["App"]
        ),
    ]
)
'''

NO_DEPENDENCIES = '''import PackageDescription

let package = Package(
    name: "App",
    targets: [
        .target(name: "App"),
        .testTarget(name: "AppTests"),
    ]
)
'''


class AddPackagesTests(unittest.TestCase):
    def test_adds_package_and_links_product_into_existing_dependency_lists(self):
        out = tooling.add_spm_packages(MULTILINE, [tooling.SPM_FRAMEWORKS["snapshot_testing"]], "AppTests")
        self.assertIn('.package(url: "https://example.com/other.git", from: "1.0.0"),\n        .package(url: "https://github.com/pointfreeco/swift-snapshot-testing.git", from: "1.17.0")\n    ],', out)
        self.assertIn('dependencies: ["App", .product(name: "SnapshotTesting", package: "swift-snapshot-testing")]', out.replace("\n            ", " ").replace('"App",\n', '"App", '))

    def test_creates_dependency_lists_when_there_are_none(self):
        out = tooling.add_spm_packages(NO_DEPENDENCIES, [tooling.SPM_FRAMEWORKS["quick_nimble"], *tooling.EXTRA_PACKAGES["quick_nimble"]], "AppTests")
        self.assertIn('    dependencies: [\n        .package(url: "https://github.com/Quick/Quick.git"', out)
        self.assertIn('.testTarget(name: "AppTests", dependencies: [.product(name: "Quick", package: "Quick"), .product(name: "Nimble", package: "Nimble")])', out)

    def test_adding_twice_changes_nothing(self):
        once = tooling.add_spm_packages(MULTILINE, [tooling.SPM_FRAMEWORKS["snapshot_testing"]], "AppTests")
        self.assertEqual(tooling.add_spm_packages(once, [tooling.SPM_FRAMEWORKS["snapshot_testing"]], "AppTests"), once)

    def test_unrecognised_manifest_is_left_for_an_ai_job(self):
        self.assertIsNone(tooling.add_spm_packages("let package = Package(name: \"App\")", [tooling.SPM_FRAMEWORKS["quick_nimble"]], "AppTests"))
        self.assertIsNone(tooling.add_spm_packages(MULTILINE, [tooling.SPM_FRAMEWORKS["quick_nimble"]], "MissingTests"))

    def test_finds_test_target(self):
        self.assertEqual(tooling.find_test_target(MULTILINE), "AppTests")
        self.assertEqual(tooling.find_test_target(MULTILINE, "Nope"), "AppTests")
        self.assertIsNone(tooling.find_test_target("Package()"))


class StarterDirTests(unittest.TestCase):
    def test_prefers_swiftpm_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("")
            self.assertEqual(tooling.starter_dir(root, "AppTests"), root / "Tests" / "AppTests")
            (root / "AppTests").mkdir()
            self.assertEqual(tooling.starter_dir(root, "AppTests"), root / "AppTests")


if __name__ == "__main__":
    unittest.main()
