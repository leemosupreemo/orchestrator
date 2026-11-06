from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Mock interactive modules before importing dev_console
orig_common = sys.modules.get('common')
sys.modules['common'] = MagicMock()
import dev_console  # noqa: E402
if orig_common is not None:
    sys.modules['common'] = orig_common
else:
    del sys.modules['common']

class ConsoleSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = dev_console.ROOT
        self.temp_root = Path("/tmp/orchestrator-smoke-root")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        dev_console.ROOT = self.temp_root
        dev_console.CONFIG_DIR = self.temp_root / ".orchestrator" / "config"
        dev_console.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        # Setup dummy PROJECT_CONFIG
        dev_console.PROJECT_CONFIG = MagicMock()
        dev_console.PROJECT_CONFIG.project_name = "SmokeTestProject"
        dev_console.PROJECT_CONFIG.runtime_dir = self.temp_root / ".orchestrator"
        dev_console.PROJECT_CONFIG.validate_distribution_config.return_value = []

    def tearDown(self) -> None:
        dev_console.ROOT = self.old_root
        import shutil
        if self.temp_root.exists():
            shutil.rmtree(self.temp_root)

    def _mock_get_key_side_effect(self, planned_inputs: list[str]):
        """Helper to provide planned inputs followed by empty strings for non-blocking loops."""
        for inp in planned_inputs:
            yield inp
        while True:
            yield ""

    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.print_phase")
    @patch("dev_console.print_header")
    @patch("dev_console.save_job")
    @patch("dev_console.refresh_job")
    @patch("dev_console.read_json")
    @patch("dev_console.write_json")
    @patch("dev_console.PROJECT_CONFIG")
    @patch("dev_console.prompt_input")
    def test_job_menu_smoke(self, mock_prompt_input, mock_config, mock_write, mock_read, mock_refresh, mock_save, mock_header, mock_phase, mock_status, mock_clear, mock_input, mock_get_key):
        """Superficially run through Job Selection menu options to ensure no NameErrors or obvious crashes."""
        job = {
            "job_id": "smoke-job",
            "status": "planned",
            "type": "feature-plan",
            "planner": "gemini",
            "builder": "gemini",
            "reviewer": "gemini",
            "llm_sessions": [
                {"id": "session-1", "model": "gemini-3.1-pro-preview"},
                {"id": "session-2", "model": "gpt-5.5"}
            ],
            "_path": self.temp_root / "smoke-job.json"
        }
        mock_refresh.return_value = job
        mock_read.return_value = job
        
        mock_get_key.side_effect = self._mock_get_key_side_effect(["j", "q", "b"])
        mock_prompt_input.side_effect = ["b"]
        mock_input.return_value = ""
        
        # Mock run_llm for Ask AI
        with patch("dev_console.run_llm", return_value=("AI response", "actual-model", "new-session-id")):
             dev_console.handle_job_selection(job, ["local"], ["gemini"])
        
        self.assertTrue(True)

    @patch("dev_console.prompt_input")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.input")
    def test_config_menu_smoke(self, mock_input, _mock_status, _mock_clear, mock_get_key, mock_prompt_input):
        """Superficially run through Configuration menu options."""
        # Traversal:
        # 'i' (Instructions) -> 'b' (Back)
        # 's' (Docs) -> 'b' (Back)
        # 'b' (Back/Exit menu)
        mock_get_key.side_effect = self._mock_get_key_side_effect(["i", "b", "s", "b"])
        mock_prompt_input.side_effect = ["b"]
        mock_input.return_value = ""
        
        # Avoid running actual scripts during smoke test
        with patch("dev_console.run_script") as mock_run:
            dev_console.handle_configuration_menu(["local"], ["gemini"])
        
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_self_test_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Tooling/Self-Test menu options."""
        # Traversal:
        # 'b' (Back/Exit menu)
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        
        dev_console.handle_tooling_tests(["local"], ["gemini"])
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("subprocess.check_output")
    def test_github_menu_smoke(self, mock_check_output, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through GitHub & Source Control menu options."""
        mock_check_output.return_value = b"main\n"
        # Traversal:
        # 'r' (Refresh) -> 'b' (Back/Exit menu)
        mock_get_key.side_effect = self._mock_get_key_side_effect(["r", "b"])
        
        dev_console.handle_github_menu(["local"], ["gemini"])
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_manage_tests_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Manage Tests & Coverage menu options."""
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        
        dev_console.handle_manage_tests(["local"], ["gemini"])
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_run_tests_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Run Unit Tests menu options."""
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        
        dev_console.handle_run_tests_menu(["local"], ["gemini"])
        self.assertTrue(True)

    def test_discover_test_suites(self):
        """Verify discovery of Swift test suites and test count parsing."""
        test_dir = self.temp_root / "AppTests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "AuthTests.swift"
        test_file.write_text("""import XCTest
class AuthTests: XCTestCase {
    func testLoginSuccess() { XCTAssertTrue(true) }
    func testLoginFailure() { XCTAssertFalse(false) }
    func testTokenExpiry() { }
}
""")
        suites = dev_console.discover_test_suites(self.temp_root, "AppTests")
        self.assertEqual(len(suites), 1)
        self.assertEqual(suites[0]["name"], "AuthTests")
        self.assertEqual(suites[0]["test_count"], 3)

    @patch("dev_console.prompt_input")
    @patch("dev_console.input")
    def test_rename_test_suite(self, mock_input, mock_prompt_input):
        """Verify renaming a Swift test suite updates both the file and class definition."""
        test_dir = self.temp_root / "AppTests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "OldAuthTests.swift"
        test_file.write_text("""import XCTest
class OldAuthTests: XCTestCase {
    func testExample() {}
}
""")
        suite = {
            "path": test_file,
            "rel_path": "AppTests/OldAuthTests.swift",
            "name": "OldAuthTests",
            "test_count": 1
        }
        mock_prompt_input.return_value = "NewAuthTests"
        mock_input.return_value = ""

        success = dev_console.rename_test_suite(suite, self.temp_root)
        self.assertTrue(success)
        self.assertFalse(test_file.exists())
        new_file = test_dir / "NewAuthTests.swift"
        self.assertTrue(new_file.exists())
        self.assertIn("class NewAuthTests: XCTestCase", new_file.read_text())

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_test_frameworks_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Test Frameworks & Plugins menu."""
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        
        dev_console.handle_test_frameworks_menu(["local"], ["gemini"])
        self.assertTrue(True)

    @patch("dev_console.input")
    @patch("dev_console.subprocess.Popen")
    @patch("dev_console.clear_screen")
    def test_run_calculate_coverage_smoke(self, _mock_clear, mock_popen, mock_input):
        """Verify run_calculate_coverage executes without NameError or crash."""
        mock_input.return_value = ""
        mock_proc = MagicMock()
        mock_proc.stdout = ["Test Suite Passed\n"]
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        pct = dev_console.run_calculate_coverage(["local"], ["gemini"])
        # Should return calculated percentage (or float/None without raising NameError)
        self.assertTrue(pct is None or isinstance(pct, (int, float)))

    def test_setup_xcode_cloud_scripts(self):
        """Test generation of standard Xcode Cloud ci_scripts/."""
        temp_dir = Path(tempfile.mkdtemp())
        try:
            created = dev_console.setup_xcode_cloud_scripts(temp_dir)
            self.assertEqual(len(created), 3)
            self.assertTrue((temp_dir / "ci_scripts" / "ci_post_clone.sh").exists())
            self.assertTrue((temp_dir / "ci_scripts" / "ci_pre_xcodebuild.sh").exists())
            self.assertTrue((temp_dir / "ci_scripts" / "ci_post_xcodebuild.sh").exists())
            # Check executable bit
            self.assertTrue(os.access(temp_dir / "ci_scripts" / "ci_post_clone.sh", os.X_OK))
        finally:
            shutil.rmtree(temp_dir)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_xcode_cloud_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Xcode Cloud menu."""
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        dev_console.handle_xcode_cloud_menu(["local"], ["gemini"])
        self.assertTrue(True)

    def test_strip_swift_comments(self):
        code = """
        // Single line comment
        let a = 1
        /* Multi-line
           comment */
        let b = 2 /* nested /* block */ comment */
        let c = "string with // not a comment"
        let d = \"\"\"
        multiline string with /* not a comment */
        \"\"\"
        """
        cleaned = dev_console.strip_swift_comments(code)
        self.assertNotIn("Single line comment", cleaned)
        self.assertNotIn("Multi-line", cleaned)
        self.assertNotIn("nested", cleaned)
        self.assertIn('let c = "string with // not a comment"', cleaned)
        self.assertIn("multiline string with /* not a comment */", cleaned)

    def test_discover_test_suites_swift_testing(self):
        """Verify parsing of modern Swift Testing @Suite and @Test syntax."""
        test_dir = self.temp_root / "Features" / "Auth" / "Tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "AuthFeatureTests.swift"
        test_file.write_text("""import Testing
@testable import Auth

@Suite("Authentication Feature Tests")
struct AuthFeatureTests {
    @Test
    func simpleTest() {
        #expect(true)
    }

    @Test("Custom display name")
    @MainActor
    func customDisplayTest() async throws {
        #expect(true)
    }

    @Test(
        "Multi-line test with traits",
        .tags(.critical),
        .enabled(if: true),
        arguments: ["user1", "user2"]
    )
    mutating func parameterizedTest(user: String) {
        #expect(!user.isEmpty)
    }

    @Test func `test with backticked name`() {
    }

    // @Test func commentedOutTest() {}
}
""")
        suites = dev_console.discover_test_suites(self.temp_root)
        self.assertEqual(len(suites), 1)
        self.assertEqual(suites[0]["name"], "AuthFeatureTests")
        self.assertEqual(suites[0]["test_count"], 4)
        self.assertIn("simpleTest", suites[0]["test_funcs"])
        self.assertIn("customDisplayTest", suites[0]["test_funcs"])
        self.assertIn("parameterizedTest", suites[0]["test_funcs"])
        self.assertIn("test with backticked name", suites[0]["test_funcs"])

    def test_discover_test_suites_quick_nimble_and_base_class(self):
        """Verify parsing of Quick/Nimble BDD specs and custom BaseTestCase inheritance."""
        quick_dir = self.temp_root / "Packages" / "Cart" / "Tests" / "CartTests"
        quick_dir.mkdir(parents=True, exist_ok=True)
        spec_file = quick_dir / "CartSpec.swift"
        spec_file.write_text("""import Quick
import Nimble

class CartSpec: QuickSpec {
    override class func spec() {
        describe("Cart") {
            it("calculates total") {
                expect(1).to(equal(1))
            }
            fit("focused calculation") {
                expect(2).to(equal(2))
            }
            xit("pending calculation") {
            }
            itBehavesLike("persisted cart")
        }
    }
}
""")
        base_dir = self.temp_root / "App" / "Tests"
        base_dir.mkdir(parents=True, exist_ok=True)
        base_file = base_dir / "PaymentIntegrationTests.swift"
        base_file.write_text("""import XCTest

final class PaymentIntegrationTests: BaseIntegrationTestCase {
    func testChargeCard() async throws {
        XCTAssertTrue(true)
    }
    func testRefund() {
        XCTAssertTrue(true)
    }
}
""")
        suites = dev_console.discover_test_suites(self.temp_root)
        self.assertEqual(len(suites), 2)
        suite_dict = {s["name"]: s for s in suites}
        
        self.assertIn("CartSpec", suite_dict)
        self.assertEqual(suite_dict["CartSpec"]["test_count"], 4)
        
        self.assertIn("PaymentIntegrationTests", suite_dict)
        self.assertEqual(suite_dict["PaymentIntegrationTests"]["test_count"], 2)

    def test_discover_test_suites_no_double_counting(self):
        """Verify @Test on func testFoo() is counted only once."""
        test_dir = self.temp_root / "AppTests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "DualTests.swift"
        test_file.write_text("""import Testing
@Suite struct DualTests {
    @Test func testExplicitName() {}
    func testStandardXCTest() {}
}
""")
        suites = dev_console.discover_test_suites(self.temp_root)
        self.assertEqual(len(suites), 1)
        self.assertEqual(suites[0]["test_count"], 2)

    def test_generate_test_index(self):
        """Verify generate_test_index discovers Swift and Python test targets."""
        import generate_test_index
        test_dir = self.temp_root / "AppTests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "ProfileTests.swift").write_text("""import XCTest
class ProfileTests: XCTestCase {
    func testProfile() {}
}
""")
        py_dir = self.temp_root / "tests"
        py_dir.mkdir(parents=True, exist_ok=True)
        (py_dir / "test_api.py").write_text("def test_dummy(): pass")

        index = generate_test_index.generate_test_index(self.temp_root)
        self.assertIn("ProfileTests", index)
        self.assertIn("test_api.py", index)

if __name__ == "__main__":
    unittest.main()

