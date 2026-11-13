from __future__ import annotations

import unittest

from orchestrator.env_issues import detect_simulator_environment_issue


class EnvIssuesTests(unittest.TestCase):
    def test_detect_stale_spm_cache(self):
        output = (
            "error: There is no XCFramework found at "
            "/Users/dev/Library/Caches/org.swift.swiftpm/artifacts/MyLib.xcframework"
        )
        issue = detect_simulator_environment_issue(output)
        self.assertIsNotNone(issue)
        self.assertIn("Stale or incomplete SwiftPM binary artifact cache", issue)
        self.assertIn("Clear the worker's DerivedData", issue)

    def test_detect_coresimulator_mismatch(self):
        output = "CoreSimulator is out of date for selected Xcode version."
        issue = detect_simulator_environment_issue(output)
        self.assertIsNotNone(issue)
        self.assertIn("Xcode/CoreSimulator mismatch", issue)

    def test_detect_missing_ios_platform(self):
        output1 = "iOS 26.5 is not installed on this machine."
        issue1 = detect_simulator_environment_issue(output1)
        self.assertIsNotNone(issue1)
        self.assertIn("Missing iOS simulator platform", issue1)

        output2 = "Please download and install the platform from Xcode > Settings > Components"
        issue2 = detect_simulator_environment_issue(output2)
        self.assertIsNotNone(issue2)
        self.assertIn("Missing iOS simulator platform", issue2)

    def test_detect_unavailable_simulator_destination(self):
        output = (
            "Unable to find a device matching the provided destination specifier: "
            "no available devices matched"
        )
        issue = detect_simulator_environment_issue(output)
        self.assertIsNotNone(issue)
        self.assertIn("Unavailable simulator destination", issue)

    def test_unmatched_output_returns_none(self):
        output = "CompileSwift normal arm64 /Users/dev/App.swift\nBuild succeeded"
        self.assertIsNone(detect_simulator_environment_issue(output))


if __name__ == "__main__":
    unittest.main()
