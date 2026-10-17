from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.scripts import test_cases as tc


class TestCaseLibraryTests(unittest.TestCase):
    def test_plan_problems_enforces_automated_tests(self):
        # Functionality case lacking tests
        plan_bad = {
            "test_cases": [
                {
                    "title": "Log in to system",
                    "type": "functionality",
                    "expected": "Dashboard visible",
                    "tests": [],
                }
            ]
        }
        problems = tc.plan_problems(plan_bad)
        self.assertTrue(any("automated test cases have no `tests` assigned" in p for p in problems))

        # Usability case permitted to have no tests, but if ALL are manual, requires automated case
        plan_all_manual = {
            "test_cases": [
                {
                    "title": "Review UX layout",
                    "type": "usability",
                    "expected": "Layout looks clean",
                    "tests": [],
                }
            ]
        }
        problems_manual = tc.plan_problems(plan_all_manual)
        self.assertTrue(any("Every test case is usability or user-acceptance" in p for p in problems_manual))

        # Valid mix with automated tests
        plan_good = {
            "test_cases": [
                {
                    "title": "Log in to system",
                    "type": "functionality",
                    "expected": "Dashboard visible",
                    "tests": ["AuthTests/test_login"],
                    "covers": ["User can log in"],
                },
                {
                    "title": "Review UX layout",
                    "type": "usability",
                    "expected": "Layout looks clean",
                    "tests": [],
                    "covers": ["Layout is intuitive"],
                },
            ],
            "acceptance_criteria": [
                "User can log in",
                "Layout is intuitive",
            ],
        }
        self.assertEqual(tc.plan_problems(plan_good), [])

    def test_library_disk_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = tc.normalize_cases([
                {
                    "id": "TC-101-01",
                    "area": "Authentication",
                    "title": "Valid login",
                    "type": "functionality",
                    "expected": "Token issued",
                    "tests": ["AuthTests/test_login"],
                }
            ], 101)

            # Write cases
            written = tc.write_cases(root, cases)
            self.assertTrue(len(written) >= 2)  # case file + README.md
            self.assertTrue((tc.library_dir(root) / "README.md").exists())

            # Load cases
            loaded = tc.load_library(root)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["id"], "TC-101-01")
            self.assertEqual(loaded[0]["area"], "Authentication")

            # Create or update case
            updated = tc.create_or_update_library_case(root, {
                "id": "TC-101-01",
                "title": "Updated login",
                "expected": "Token and profile issued",
            })
            self.assertEqual(updated["title"], "Updated login")

            # Delete case
            deleted = tc.delete_library_case(root, "TC-101-01")
            self.assertTrue(deleted)
            self.assertEqual(tc.load_library(root), [])
            self.assertFalse(tc.delete_library_case(root, "TC-999-99"))

    def test_scan_tests_and_resolve_test_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()

            test_file = tests_dir / "test_auth.py"
            test_file.write_text(
                "import unittest\n\n"
                "class AuthTests(unittest.TestCase):\n"
                "    # TC-101-01\n"
                "    def test_login_success(self):\n"
                "        pass\n"
            )

            # Add test plan
            plan_file = tests_dir / "Multiplayer.xctestplan"
            plan_file.write_text("{}")

            index = tc.scan_tests(root)
            self.assertIn("TC-101-01", index["markers"])
            self.assertEqual(len(index["markers"]["TC-101-01"]), 1)
            self.assertIn("AuthTests", index["suites"])
            self.assertIn("test_login_success", index["methods"])
            self.assertIn("Multiplayer", index["plans"])

            # Test resolve_test_ref
            self.assertEqual(tc.resolve_test_ref("Multiplayer", index), "test plan Multiplayer")
            self.assertIsNotNone(tc.resolve_test_ref("AuthTests/test_login_success", index))
            self.assertIsNotNone(tc.resolve_test_ref("AuthTests", index))
            self.assertIsNone(tc.resolve_test_ref("NonExistentSuite", index))
            self.assertIsNone(tc.resolve_test_ref("", index))

    def test_coverage_and_missing_tests_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()

            (tests_dir / "test_app.py").write_text(
                "class AppTests:\n"
                "    # TC-200-01\n"
                "    def test_feature(self):\n"
                "        pass\n"
            )

            job = {
                "type": "bug",
                "plan": {
                    "test_cases": [
                        {
                            "id": "TC-200-01",
                            "type": "functionality",
                            "title": "Covered Case",
                            "expected": "Passes",
                            "tests": ["AppTests/test_feature"],
                        },
                        {
                            "id": "TC-200-02",
                            "type": "functionality",
                            "title": "Missing Case",
                            "expected": "Fails if missing",
                            "tests": ["AppTests/test_missing"],
                        },
                        {
                            "id": "TC-200-03",
                            "type": "usability",
                            "title": "Manual Case",
                            "expected": "Checked by human",
                            "tests": [],
                        },
                    ]
                }
            }

            missing = tc.missing_tests(root, job)
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["id"], "TC-200-02")

            feedback = tc.missing_tests_feedback(missing)
            self.assertIn("TC-200-02", feedback)

            # Test CLI check
            job_file = root / "job.json"
            job_file.write_text(json.dumps(job))

            with patch("orchestrator.scripts.test_cases._root", return_value=root):
                code = tc.main(["check", str(job_file)])
                self.assertEqual(code, 1)

                # Fix missing test
                (tests_dir / "test_app.py").write_text(
                    "class AppTests:\n"
                    "    # TC-200-01\n"
                    "    # TC-200-02\n"
                    "    def test_feature(self):\n"
                    "        pass\n"
                )
                code_fixed = tc.main(["check", str(job_file)])
                self.assertEqual(code_fixed, 0)


if __name__ == "__main__":
    unittest.main()
