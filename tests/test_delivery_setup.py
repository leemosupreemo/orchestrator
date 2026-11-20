from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import time
import unittest
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import delivery_setup as ds  # noqa: E402
from test_test_groups import write  # noqa: E402

APP = "1:802761731717:ios:4540e7fc06961c5be243ec"
OTHER_APP = "1:111111111111:ios:aaaaaaaaaaaaaaaaaaaa"
SCRIPT = """#!/usr/bin/env bash
# Build the iOS player, archive it, export an IPA.
#   OUT_DIR   artifact directory (default: .local/release-<version>/ios/)
set -euo pipefail
"""
AGENTS = ("**Development/test delivery exception:** A fresh explicit user request authorizes one Firebase App Distribution upload only to "
          f"project `daoc-mobile-2026`, iOS app `{APP}`, and group `daoc-testers`.")


def unity_project(root: Path) -> None:
    write(root, "Client/ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.30f1\n")
    write(root, "Client/Assets/A.cs", "x")
    write(root, "tools/package_ios.sh", SCRIPT)


def receipt(root: Path, **fields) -> None:
    data = {"firebase_project": "receipt-project", "firebase_app_id": APP, "tester_group": "receipt-testers", **fields}
    write(root, ".local/distribution-receipts/firebase-1.json", json.dumps(data))


class FindingTheFactsTests(unittest.TestCase):
    def test_a_past_upload_receipt_wins_and_each_value_says_where_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_project(root)
            receipt(root)
            write(root, "AGENTS.md", AGENTS)
            facts = ds.firebase_facts(root)
            self.assertEqual(facts["project"][0], ("receipt-project", "a past upload receipt (firebase-1.json)"))
            self.assertEqual(facts["app"][0][0], APP)
            self.assertEqual(facts["group"][0][0], "receipt-testers")
            self.assertIn(("daoc-mobile-2026", "AGENTS.md"), facts["project"])  # still known, just second

    def test_the_apps_own_firebase_files_and_the_docs_fill_in_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "Client/Assets/iOS/GoogleService-Info.plist", "")
            (root / "Client/Assets/iOS/GoogleService-Info.plist").write_bytes(plistlib.dumps({"PROJECT_ID": "plist-project", "GOOGLE_APP_ID": OTHER_APP}))
            write(root, "android/google-services.json", json.dumps({"project_info": {"project_id": "json-project"},
                                                                       "client": [{"client_info": {"mobilesdk_app_id": "1:2:android:bbbbbbbbbbbbbbbb"}}]}))
            write(root, ".firebaserc", json.dumps({"projects": {"default": "rc-project"}}))
            write(root, "AGENTS.md", AGENTS)
            facts = ds.firebase_facts(root)
            self.assertEqual([v for v, _ in facts["project"]][:3], ["plist-project", "json-project", "rc-project"])
            self.assertEqual(facts["project"][0][1], "Client/Assets/iOS/GoogleService-Info.plist")
            self.assertIn(OTHER_APP, [v for v, _ in facts["app"]])
            self.assertEqual(facts["group"], [("daoc-testers", "AGENTS.md")])

    def test_dependency_folders_and_other_repositories_are_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "node_modules/x/.firebaserc", json.dumps({"projects": {"default": "from-deps"}}))
            write(root, "vendor/.git", "gitdir: x")
            write(root, "vendor/.firebaserc", json.dumps({"projects": {"default": "from-clone"}}))
            self.assertEqual(ds.firebase_facts(root)["project"], [])

    def test_artifact_patterns_turn_version_and_build_numbers_into_wildcards(self):
        root = Path("/p")
        self.assertEqual(ds.artifact_pattern(root, root / ".local/release-0.1.4/firebase-build-117/DAoCMobile-0.1.4-117.ipa"),
                         ".local/release-*/firebase-build-*/*.ipa")
        self.assertEqual(ds.artifact_pattern(root, root / "app/build/outputs/apk/debug/app-debug.apk"), "app/build/outputs/apk/debug/*.apk")

    def test_an_existing_ipa_in_a_dependency_folder_is_ignored_and_the_newest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "node_modules/x/Old.ipa", "x")
            write(root, "out/a/First.ipa", "x")
            write(root, "out/b/Second.apk", "x")
            os.utime(root / "out/a/First.ipa", (1000, 1000))
            self.assertEqual(ds.existing_artifact(root), root / "out/b/Second.apk")


class ProposalTests(unittest.TestCase):
    def test_a_unity_project_with_its_own_script_gets_a_complete_proposal_and_never_automatic_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_project(root)
            receipt(root)
            write(root, "AGENTS.md", AGENTS)
            result = ds.propose(root, {})
            self.assertEqual((result["state"], result["platform"], result["missing"]), ("proposal", "file", []))
            self.assertEqual(result["fields"], {
                "delivery_platform": "file", "delivery_build_command": "bash tools/package_ios.sh",
                "delivery_artifact": ".local/release-*/ios/*.ipa", "firebase_app_id": APP,
                "firebase_project_id": "receipt-project", "firebase_groups": "receipt-testers"})
            self.assertNotIn("automatic", result["fields"])
            sources = {f["label"]: f["source"] for f in result["found"]}
            self.assertEqual(sources["File it makes"], "where the build script says it writes")
            self.assertIn("explicitly requested", " ".join(result["notes"]))

    def test_with_no_script_output_documented_the_file_pattern_comes_from_an_earlier_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_project(root)
            write(root, "tools/package_ios.sh", "#!/bin/bash\necho hi\n")
            write(root, ".local/release-0.1.4/firebase-build-117/App-0.1.4-117.ipa", "x")
            receipt(root)
            result = ds.propose(root, {})
            self.assertEqual(result["fields"]["delivery_artifact"], ".local/release-*/firebase-build-*/*.ipa")
            self.assertIn("an earlier build", {f["label"]: f["source"] for f in result["found"]}["File it makes"])

    def test_what_is_missing_is_listed_so_the_person_only_fills_that_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_project(root)
            result = ds.propose(root, {})
            self.assertEqual(result["state"], "proposal")
            self.assertEqual(set(result["missing"]), {"Firebase app ID"})  # the script says where its .ipa goes
            self.assertNotIn("firebase_app_id", result["fields"])

    def test_gradle_and_web_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "gradlew", "#!/bin/sh")
            write(root, "android/google-services.json", json.dumps({"project_info": {"project_id": "p"},
                                                                       "client": [{"client_info": {"mobilesdk_app_id": "1:2:android:bbbbbbbbbbbbbbbb"}}]}))
            result = ds.propose(root, {})
            self.assertEqual((result["platform"], result["fields"]["delivery_build_command"], result["fields"]["firebase_app_id"]),
                             ("android", "./gradlew assembleDebug", "1:2:android:bbbbbbbbbbbbbbbb"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "firebase.json", json.dumps({"hosting": {"public": "dist"}}))
            result = ds.propose(root, {})
            self.assertEqual((result["state"], result["platform"], result["missing"]), ("proposal", "web", []))

    def test_xcode_projects_and_configured_projects_and_empty_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "App.xcodeproj/project.pbxproj", "x")
            self.assertEqual(ds.propose(root, {})["state"], "xcode")
            self.assertEqual(ds.propose(root, {"distribution_script_path": "scripts/d.sh"})["state"], "configured")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_project(root)
            self.assertEqual(ds.propose(root, {"delivery_platform": "file"})["state"], "configured")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ds.propose(Path(tmp), {})["state"], "none")


if __name__ == "__main__":
    unittest.main()
