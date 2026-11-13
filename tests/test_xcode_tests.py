from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import xcode_tests as xt

SWIFT = "import XCTest\nfinal class ParserTests: XCTestCase {\n func testA() {}\n}\nclass Helper {}\nclass OtherTests: BaseCase, XCTestCase {}\n"
HAS_GEM = bool(shutil.which("ruby")) and subprocess.run(["ruby", "-e", "require 'xcodeproj'"], capture_output=True).returncode == 0


class XcodeTestsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_test_classes_in_a_swift_file_are_found(self):
        file = self.root / "ParserTests.swift"
        file.write_text(SWIFT)
        self.assertEqual(xt.test_classes(file), ["ParserTests", "OtherTests"])
        self.assertEqual(xt.test_classes(self.root / "missing.swift"), [])

    def test_files_whose_classes_never_started_are_the_ones_that_did_not_run(self):
        (self.root / "A").mkdir()
        (self.root / "A/ParserTests.swift").write_text(SWIFT)
        (self.root / "A/Plain.swift").write_text("struct S {}")
        files = ["A/ParserTests.swift", "A/Plain.swift", "A/notes.md"]
        self.assertEqual(xt.not_run(self.root, files, "Test Suite 'Other' started at 1\n"), ["A/ParserTests.swift"])
        self.assertEqual(xt.not_run(self.root, files, "Test Suite 'ParserTests' passed at 1\n"), [])  # a file without test classes is never blamed

    def test_a_project_without_xcode_or_swift_files_is_left_alone(self):
        self.assertEqual(xt.register(self.root, ["tests/test_a.py"]), ([], ""))
        self.assertEqual(xt.register(self.root, ["Tests/ATests.swift"]), ([], ""))  # no .xcodeproj here
        self.assertIsNone(xt.project_file(self.root))

    @unittest.skipUnless(HAS_GEM, "needs ruby with the xcodeproj gem")
    def test_new_test_files_are_added_to_the_test_target_once(self):
        script = ("require 'xcodeproj'; p = Xcodeproj::Project.new(ARGV[0] + '/App.xcodeproj'); "
                  "t = p.new_target(:unit_test_bundle, 'AppTests', :ios); g = p.main_group.new_group('AppTests', 'AppTests'); "
                  "f = g.new_file('Old.swift'); t.source_build_phase.add_file_reference(f); p.save")
        subprocess.run(["ruby", "-e", script, str(self.root)], check=True)
        (self.root / "AppTests").mkdir()
        for name in ("Old", "New"):
            (self.root / f"AppTests/{name}.swift").write_text(SWIFT)
        added, note = xt.register(self.root, ["AppTests/Old.swift", "AppTests/New.swift", "AppTests/data.json"])
        self.assertEqual((added, note), (["AppTests/New.swift"], ""))  # Old was already part of the target
        text = xt.project_file(self.root).read_text()
        self.assertIn("New.swift in Sources", text)
        self.assertEqual(xt.register(self.root, ["AppTests/New.swift"]), ([], ""))  # nothing to do the second time


if __name__ == "__main__":
    unittest.main()
