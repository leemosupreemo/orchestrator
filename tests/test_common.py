from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402


class CommonTests(unittest.TestCase):
    def test_generic_commands_override_legacy_docs_without_simulator_probe(self):
        config = SimpleNamespace(build_command="cargo build", test_command="cargo test",
                                 xcode_project=None, xcode_workspace=None, scheme=None)
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "build-test-commands.md").write_text(
                "## iOS app build\n```bash\nswift build\n```\n"
                "## iOS app tests\n```bash\nswift test\n```\n")
            with patch.object(common, "DOCS_DIR", docs), patch.object(common, "PROJECT_CONFIG", config), \
                 patch.object(common, "get_best_simulator_destination") as simulator:
                self.assertEqual(common.extract_commands(), ("cargo build", "cargo test"))
                simulator.assert_not_called()

    def test_generic_documented_commands_and_missing_command_error(self):
        config = SimpleNamespace(build_command=None, test_command=None,
                                 xcode_project=None, xcode_workspace=None, scheme=None,
                                 derived_data_path="/tmp/test-generic-dd")
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            with patch.object(common, "DOCS_DIR", docs), patch.object(common, "PROJECT_CONFIG", config):
                with self.assertRaisesRegex(ValueError, "build_command"):
                    common.extract_commands()
                (docs / "build-test-commands.md").write_text(
                    "## Build\n```bash\nnpm run build\n```\n"
                    "## Tests\n```bash\nnpm test\n```\n")
                self.assertEqual(common.extract_commands(), ("npm run build", "npm test"))

    def test_empty_build_doc_section_does_not_borrow_test_command(self):
        config = SimpleNamespace(build_command=None, test_command=None,
                                 xcode_project=None, xcode_workspace=None, scheme=None)
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "build-test-commands.md").write_text(
                "## Build\nNot configured yet.\n## Tests\n```bash\nnpm test\n```\n")
            with patch.object(common, "DOCS_DIR", docs), patch.object(common, "PROJECT_CONFIG", config):
                with self.assertRaisesRegex(ValueError, "build_command"):
                    common.extract_commands()

    @patch("common.get_best_simulator_destination", return_value="platform=iOS Simulator,id=TEST_SIM")
    def test_extract_commands_reads_ios_app_tests_section(self, _mock_destination) -> None:
        project_config = SimpleNamespace(
            build_command=None,
            test_command=None,
            xcode_project="App.xcodeproj",
            xcode_workspace=None,
            scheme="App",
            derived_data_path="/tmp/dd",
            root=common.ROOT,
            runtime_dir=common.ORCHESTRATOR_RUNTIME_DIR,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(common, "DOCS_DIR", Path(tmp)), patch.object(common, "PROJECT_CONFIG", project_config):
                build_command, test_command = common.extract_commands()

        self.assertIn("xcodebuild build", build_command)
        self.assertIn("xcodebuild test", test_command)
        self.assertIn("-destination", test_command)
        self.assertIn("platform=iOS Simulator,id=TEST_SIM", test_command)

    @patch("common.get_best_simulator_destination", return_value="platform=iOS Simulator,id=TEST_SIM")
    def test_extract_commands_no_duplicate_destination(self, _mock_destination) -> None:
        project_config = SimpleNamespace(
            build_command=None,
            test_command=None,
            xcode_project="App.xcodeproj",
            xcode_workspace=None,
            scheme="App",
            derived_data_path="/tmp/dd",
            root=common.ROOT,
            runtime_dir=common.ORCHESTRATOR_RUNTIME_DIR,
        )
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            (docs_dir / "build-test-commands.md").write_text(
                """## iOS app build
```bash
xcodebuild build -destination 'platform=iOS Simulator,id=EXISTING'
```

## iOS app tests
```bash
xcodebuild test -destination 'platform=iOS Simulator,id=EXISTING'
```
""",
                encoding="utf-8",
            )
            with patch.object(common, "DOCS_DIR", docs_dir), patch.object(common, "PROJECT_CONFIG", project_config):
                build_command, test_command = common.extract_commands()
        
        # Count occurrences of -destination
        self.assertEqual(build_command.count("-destination"), 1)
        self.assertEqual(test_command.count("-destination"), 1)

    @patch("common.get_best_simulator_destination", return_value="platform=iOS Simulator,id=TEST_SIM")
    def test_extract_commands_resolves_pwd_before_shell_quoting(self, _mock_destination) -> None:
        project_config = SimpleNamespace(
            build_command=None,
            test_command=None,
            xcode_project="App.xcodeproj",
            xcode_workspace=None,
            scheme="App",
            derived_data_path="/tmp/dd",
            root=common.ROOT,
            runtime_dir=common.ORCHESTRATOR_RUNTIME_DIR,
        )
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            (docs_dir / "build-test-commands.md").write_text(
                """## iOS app build
```bash
xcodebuild build CLANG_MODULE_CACHE_PATH=$(pwd)/.clang-module-cache
```

## iOS app tests
```bash
xcodebuild test CLANG_MODULE_CACHE_PATH=$(pwd)/.clang-module-cache
```
""",
                encoding="utf-8",
            )
            with patch.object(common, "DOCS_DIR", docs_dir), patch.object(common, "PROJECT_CONFIG", project_config):
                build_command, test_command = common.extract_commands()

        expected_cache_path = f"CLANG_MODULE_CACHE_PATH={common.ROOT}/.clang-module-cache"

        self.assertNotIn("$(pwd)", build_command)
        self.assertNotIn("$(pwd)", test_command)
        self.assertIn(expected_cache_path, build_command)
        self.assertIn(expected_cache_path, test_command)


    def test_extract_commands_does_not_add_xcode_flags_to_custom_commands(self) -> None:
        project_config = SimpleNamespace(
            build_command="swift build",
            test_command="swift test",
            xcode_project=None,
            xcode_workspace=None,
            scheme="TrialPackage",
            derived_data_path="/tmp/trial_dd",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(common, "DOCS_DIR", Path(tmp)), patch.object(common, "PROJECT_CONFIG", project_config):
                build_command, test_command = common.extract_commands()

        self.assertEqual(build_command, "swift build")
        self.assertEqual(test_command, "swift test")
        self.assertNotIn("-derivedDataPath", build_command)
        self.assertNotIn("-derivedDataPath", test_command)

    def test_choice_prompt_shows_cursor_and_positions_it_in_field(self) -> None:
        prompt = common.get_choice_prompt("Choice:", "(number or letter)")

        self.assertTrue(prompt.startswith("\033[?25h"))
        self.assertTrue(prompt.endswith("\033[20D"))
        self.assertIn("\033[48;5;236m\033[90m (number or letter) ", prompt)

    def test_header_string_normalizes_title_to_caps(self) -> None:
        header = common.get_header_string("select models")

        self.assertIn("SELECT MODELS", header)
        self.assertNotIn("select models", header)

    def test_header_string_keeps_parenthetical_helper_text_lowercase(self) -> None:
        header = common.get_header_string(
            "Load spec from a local file or web address? (useful for large multi-page docs)"
        )

        self.assertIn("LOAD SPEC FROM A LOCAL FILE OR WEB ADDRESS?", header)
        self.assertIn("(useful for large multi-page docs)", header)
        self.assertNotIn("(USEFUL FOR LARGE MULTI-PAGE DOCS)", header)

    def test_visible_width_and_fitting(self) -> None:
        self.assertEqual(common.visible_width("Hello World"), 11)
        self.assertEqual(common.visible_width("\033[1;96mHello World\033[0m"), 11)
        # Emojis count as 2 visible cells
        self.assertEqual(common.visible_width("🧠"), 2)
        self.assertEqual(common.visible_width("📦 Box"), 6)

        fitted = common.fit_to_visible_width("Very Long Title That Needs Truncating", 15)
        self.assertLessEqual(common.visible_width(fitted), 15)
        self.assertTrue(fitted.endswith("…"))

    def test_header_string_readjusts_on_small_form_factors(self) -> None:
        long_title = "Identified Risks & Mitigation Strategies"
        
        for cols in [30, 40, 60, 80]:
            with patch("os.get_terminal_size", return_value=(cols, 24)):
                header = common.get_header_string(long_title)
                # Ensure no single line in header exceeds cols - 2
                clean_lines = [re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line) for line in header.split("\n") if line.strip()]
                for line in clean_lines:
                    self.assertLessEqual(common.visible_width(line), cols - 2)

    def test_section_and_subtitle_formatting(self) -> None:
        sec = common.format_section_header("Current Project")
        self.assertIn("Current Project", sec)
        self.assertIn("\033[1;97m", sec)

        sub = common.format_subtitle("Description text")
        self.assertIn("Description text", sub)
        self.assertIn("\033[90m", sub)

        hint = common.format_subtitle("(Arrows: navigate)", hint=True)
        self.assertIn("\033[1;90m", hint)

    @patch("common.sys.stdout.isatty", return_value=True)
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_progress_indicator_hides_cursor_while_rendering(self, mock_write, _mock_flush, _mock_isatty) -> None:
        indicator = common.ProgressIndicator()

        indicator.render(force=True)
        indicator.clear()

        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("\033[?25l", written)
        self.assertIn("\033[?25h", written)
        self.assertNotIn("\0337", written)
        self.assertNotIn("\0338", written)

    @patch("common.sys.stdout.isatty", return_value=False)
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_progress_indicator_is_silent_when_stdout_is_not_tty(self, mock_write, _mock_flush, _mock_isatty) -> None:
        indicator = common.ProgressIndicator()

        indicator.render(force=True)
        indicator.clear()

        mock_write.assert_not_called()

    @patch("common.os.get_terminal_size", return_value=(45, 24))
    def test_progress_indicator_clamps_to_terminal_width(self, _mock_size) -> None:
        indicator = common.ProgressIndicator(
            label="Thinking: Deep Reasoning model active",
            hint="Ctrl-C to cancel"
        )
        line = indicator.get_line(last_activity_time=100.0)
        # Strip ANSI escape codes to measure visible character length
        import re
        plain = re.sub(r"\033\[[0-9;]*m", "", line)
        self.assertLessEqual(len(plain), 43)
        self.assertNotIn("......", plain)

    def test_progress_indicator_does_not_duplicate_trailing_dots(self) -> None:
        indicator = common.ProgressIndicator(label="Consulting gemini...")
        line = indicator.get_line()
        import re
        plain = re.sub(r"\033\[[0-9;]*m", "", line)
        self.assertNotIn("......", plain)
        self.assertIn("Consulting gemini...", plain)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.sys.stdout.isatty", return_value=True)
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_processing_status_bar_hides_cursor_until_reset(
        self, mock_write, _mock_flush, _mock_stdout_isatty, _mock_stdin_isatty
    ) -> None:
        status_bar = common.StatusBar(is_processing=True)
        status_bar.anchor_to_bottom = True

        status_bar.set_scroll_region()
        status_bar.reset_scroll_region()

        writes = [call.args[0] for call in mock_write.call_args_list]
        self.assertTrue(writes[0].startswith("\033[?25l"))
        self.assertIn("\033[?25h", writes[-1])

    @patch("common.StatusBar._get_branch", return_value="main")
    @patch("common.StatusBar._get_size", return_value=(100, 24))
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_status_bar_prompt_has_compact_pipe_spacing(
        self, mock_write, _mock_flush, _mock_size, _mock_branch
    ) -> None:
        status_bar = common.StatusBar(sub_menu=True)

        status_bar.render(force=True)

        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn(" B to go back | dir:", written)
        self.assertNotIn(" B to go back       | dir:", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_radio_prompt_uses_aligned_selector_gutter(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = [
            "✨ Brand New Feature (Design-First)",
            "🛠️ Iterating / Small Refactor (Plan or Quick Mode)",
        ]

        choice = common.prompt_radio("Select Job Type:", options, default=options[1])

        self.assertEqual(choice, options[1])
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("\033[?25l\033[r\033[2J\033[H", written)
        self.assertIn("(Arrows: navigate, Enter: select, B: back)\033[0m\n\n", written)
        self.assertIn("  [ ]  ✨ Brand New Feature", written)
        self.assertIn("> [x]  🛠️ Iterating", written)
        self.assertIn("[\033[1;91mB\033[0m] Back", written)
        self.assertIn("\033[J", written)
        self.assertIn("Choice: \033[1;96m🛠️ Iterating / Small Refactor", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_radio_prompt_renders_parenthetical_text_as_description(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = [
            "new (creates a new branch automatically)",
            "manual (no git actions; skip checkout/pull)",
        ]

        choice = common.prompt_radio("Branch selection:", options, default=options[0])

        self.assertEqual(choice, options[0])
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("> [x]  new", written)
        self.assertIn("       \033[90mcreates a new branch automatically\033[0m", written)
        self.assertNotIn("> [x]  new (creates a new branch automatically)", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_confirm_prompt_can_show_description_under_short_title(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        result = common.prompt_confirm(
            "YOLO mode?",
            default=False,
            description="Automatically dispatch after planning.",
        )

        self.assertFalse(result)
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("YOLO MODE?", written)
        self.assertIn("\033[90mAutomatically dispatch after planning.\033[0m", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_confirm_prompt_uses_plain_banner_title(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        result = common.prompt_confirm("Load spec from a local file or web address?", default=False)

        self.assertFalse(result)
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("LOAD SPEC FROM A LOCAL FILE OR WEB ADDRESS?", written)
        self.assertNotIn("\033[1;97mLoad spec", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_footer_actions_are_visible_below_options(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        footer = "[R] Sync Registry  [D] Live Discovery  [B] Back"

        common.prompt_checkbox("select models", ["model-a"], ["model-a"], footer=footer)

        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertGreater(written.index(footer), written.index("model-a"))
        self.assertIn("(Arrows: navigate, Space: toggle, A: all, N: none, Enter: save, B: back)\033[0m\n\n", written)
        self.assertIn(f"\n{footer}", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["a", "enter"])
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_select_all_hotkey(
        self, _mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = ["--- Google ---", "gemini-flash", "gemini-pro", "--- Anthropic ---", "claude-sonnet"]
        result = common.prompt_checkbox("select models", options, [])
        self.assertEqual(result, ["gemini-flash", "gemini-pro", "claude-sonnet"])

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["f", "enter"])
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_select_free_only_hotkey(
        self, _mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = [
            "--- Google ---",
            "gemini-2.5-flash \033[1;92m(Free)\033[0m",
            "gemini-3.1-pro",
            "--- OpenCode ---",
            "opencode/qwen:free"
        ]
        result = common.prompt_checkbox("select models", options, ["gemini-3.1-pro"])
        self.assertEqual(result, ["gemini-2.5-flash \033[1;92m(Free)\033[0m", "opencode/qwen:free"])

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["f", "enter"])
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_select_free_none_found(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = ["model-paid-1", "model-paid-2"]
        result = common.prompt_checkbox("select models", options, ["model-paid-1"])
        self.assertEqual(result, ["model-paid-1"])
        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("No free options found in list.", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["n", "enter"])
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_deselect_all_hotkey(
        self, _mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        options = ["model-a", "model-b", "model-c"]
        result = common.prompt_checkbox("select models", options, ["model-a", "model-b"])
        self.assertEqual(result, [])

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["n"])
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_extra_keys_precedence_over_bulk_keys(
        self, _mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        with self.assertRaises(common.KeyInterruptException) as ctx:
            common.prompt_checkbox("active machines", ["mac1", "mac2"], ["mac1"], extra_keys=["n"])
        self.assertEqual(ctx.exception.key, "n")

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("common.sys.stdout.flush")
    @patch("common.sys.stdout.write")
    def test_checkbox_max_selection_line_shows_selected_count(
        self, mock_write, _mock_flush, _mock_get_key, _mock_isatty
    ) -> None:
        common.prompt_checkbox("active machines", ["macair"], ["macair"], max_selections=10)

        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertIn("Selected machines:", written)
        self.assertIn("1 of 10", written)
        self.assertIn("machine limit", written)
        self.assertNotIn("Fleet limit:", written)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key")
    @patch("sys.stdout.write")
    def test_prompt_input_back_behavior(self, mock_write, mock_get_key, mock_isatty) -> None:
        # If allow_back=False (default), typing 'b' should just insert 'b' and not raise BackException
        mock_get_key.side_effect = ["b", "u", "i", "l", "d", "enter"]
        res = common.prompt_input("Path:", allow_back=False)
        self.assertEqual(res, "build")

        # If allow_back=True, typing 'b' as the first key should raise BackException
        mock_get_key.side_effect = ["b"]
        with self.assertRaises(common.BackException):
            common.prompt_input("Path:", allow_back=True)

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("sys.stdout.write")
    def test_prompt_input_default_hint_confirms_enter(self, _mock_write, _mock_get_key, _mock_isatty) -> None:
        status_bar = SimpleNamespace(render=Mock())
        with patch.object(common, "_ACTIVE_STATUS_BAR", status_bar):
            res = common.prompt_input("Remote repo path:", default="/repo/path")

        self.assertEqual(res, "/repo/path")
        status_bar.render.assert_called_with(at_bottom=True, force=True, q_msg="Enter to confirm")

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", return_value="enter")
    @patch("sys.stdout.write")
    def test_prompt_input_empty_default_hint_submits_enter(self, _mock_write, _mock_get_key, _mock_isatty) -> None:
        status_bar = SimpleNamespace(render=Mock())
        with patch.object(common, "_ACTIVE_STATUS_BAR", status_bar):
            res = common.prompt_input("Enter release notes for this build:")

        self.assertEqual(res, "")
        status_bar.render.assert_called_with(at_bottom=True, force=True, q_msg="Enter to submit | Ctrl-C to exit")

    @patch("common.sys.stdin.isatty", return_value=True)
    @patch("common.get_key", side_effect=["u", "p", "d", "a", "t", "e", "enter"])
    @patch("sys.stdout.write")
    def test_prompt_input_can_render_field_below_label(self, mock_write, mock_get_key, _mock_isatty) -> None:
        result = common.prompt_input("Briefly describe what should change", placeholder="specific behavior", field_below=True)

        written = "".join(call.args[0] for call in mock_write.call_args_list)
        self.assertEqual(result, "update")
        self.assertIn("Briefly describe what should change", written)
        self.assertIn("specific behavior", written)
        self.assertEqual(written.count("Briefly describe what should change"), 1)
        self.assertIn("\033[?25h", written)
        self.assertGreaterEqual(mock_get_key.call_count, 1)


class MarkdownFormatterTests(unittest.TestCase):
    def test_format_inline_markdown_bold(self) -> None:
        text = "This is **bold** text."
        formatted = common.format_inline_markdown(text)
        self.assertEqual(formatted, "This is \033[1;97mbold\033[0m text.")

    def test_format_inline_markdown_italic(self) -> None:
        text = "This is *italic* text."
        formatted = common.format_inline_markdown(text)
        self.assertEqual(formatted, "This is \033[3mitalic\033[0m text.")

    def test_format_inline_markdown_code(self) -> None:
        text = "Run `orchestrator check` to verify."
        formatted = common.format_inline_markdown(text)
        self.assertEqual(formatted, "Run \033[1;93morchestrator check\033[0m to verify.")

    def test_format_inline_markdown_link(self) -> None:
        text = "Go to [User Guide](docs/user-guide.md) now."
        formatted = common.format_inline_markdown(text)
        self.assertEqual(formatted, "Go to \033[4;94mUser Guide\033[0m \033[90m(docs/user-guide.md)\033[0m now.")

    def test_format_inline_markdown_no_overlap(self) -> None:
        text = "Run `orchestrator_check_env` with [Guide](doc_link.md)."
        formatted = common.format_inline_markdown(text)
        self.assertIn("\033[1;93morchestrator_check_env\033[0m", formatted)
        self.assertIn("\033[4;94mGuide\033[0m \033[90m(doc_link.md)\033[0m", formatted)

    def test_format_markdown_for_terminal_headers(self) -> None:
        text = "# H1 Title\n## H2 Subtitle\n### H3 Section"
        formatted = common.format_markdown_for_terminal(text)
        self.assertIn("\033[1;95mH1 TITLE\033[0m", formatted)
        self.assertIn("\033[1;96mH2 Subtitle\033[0m", formatted)
        self.assertIn("\033[1;93mH3 Section\033[0m", formatted)

    def test_format_markdown_for_terminal_lists(self) -> None:
        text = "- Item 1\n- Item 2\n1. Numbered Item"
        formatted = common.format_markdown_for_terminal(text)
        self.assertIn("•", formatted)
        self.assertIn("1.", formatted)

    def test_format_markdown_for_terminal_code_blocks(self) -> None:
        text = "```bash\nswift build\n```"
        formatted = common.format_markdown_for_terminal(text)
        self.assertIn("┌", formatted)
        self.assertIn("│", formatted)
        self.assertIn("└", formatted)
        self.assertIn("swift build", formatted)


    def test_get_simulator_diagnostic(self) -> None:
        diag = common.get_simulator_diagnostic()
        self.assertIn("has_simctl", diag)
        self.assertIn("has_runtimes", diag)
        self.assertIn("available_devices_count", diag)
        self.assertIn("best_destination", diag)
        self.assertIsInstance(diag["has_simctl"], bool)
        self.assertIsInstance(diag["has_runtimes"], bool)

    def test_extract_destination_from_command(self) -> None:
        cmd = "xcodebuild test -workspace App.xcworkspace -scheme App -destination 'platform=iOS Simulator,id=123-ABC'"
        dest = common.extract_destination_from_command(cmd)
        self.assertEqual(dest, "platform=iOS Simulator,id=123-ABC")

    def test_extract_destination_none_when_missing(self) -> None:
        cmd = "xcodebuild test -workspace App.xcworkspace -scheme App"
        dest = common.extract_destination_from_command(cmd)
        self.assertIsNone(dest)

    def test_command_with_destination_replaces_existing(self) -> None:
        cmd = "xcodebuild test -workspace App.xcworkspace -destination 'platform=iOS Simulator,name=iPhone 15' -scheme App"
        new_cmd = common.command_with_destination(cmd, "platform=iOS Simulator,id=NEW-ID")
        self.assertIn("-destination 'platform=iOS Simulator,id=NEW-ID'", new_cmd)
        self.assertNotIn("iPhone 15", new_cmd)

    def test_command_with_destination_appends_when_missing(self) -> None:
        cmd = "xcodebuild test -workspace App.xcworkspace -scheme App"
        new_cmd = common.command_with_destination(cmd, "platform=iOS Simulator,name=iPhone 16")
        self.assertIn("-destination 'platform=iOS Simulator,name=iPhone 16'", new_cmd)

    def test_get_fallback_simulator_destinations(self) -> None:
        fallbacks = common.get_fallback_simulator_destinations("platform=iOS Simulator,id=MY-ID,arch=arm64")
        self.assertTrue(len(fallbacks) > 0)
        self.assertIn("platform=iOS Simulator,id=MY-ID", fallbacks)
        self.assertIn("platform=iOS Simulator,name=iPhone 16", fallbacks)
        self.assertIn("platform=iOS Simulator,OS=latest", fallbacks)


class StepExtractorTests(unittest.TestCase):
    def test_extract_phase_steps(self) -> None:
        self.assertEqual(common.extract_step_from_line("=== 🧠 PLANNING ==="), "Planning feature")
        self.assertEqual(common.extract_step_from_line("=== ⚙️ SUB-TASK 1/3: ADD AUTH SERVICE ==="), "Task 1/3: Add Auth Service")
        self.assertEqual(common.extract_step_from_line("=== 📡 SCHEDULING: SYNCING CODE TO MAC2 ==="), "Syncing code to worker")
        self.assertEqual(common.extract_step_from_line("=== 🌿 GIT_PREP ==="), "Preparing git branch")
        self.assertEqual(common.extract_step_from_line("=== ⚙️ STATUS_UPDATE ==="), "Updating job status")
        self.assertEqual(common.extract_step_from_line("=== 🔨 IMPLEMENTATION ==="), "Generating code changes")
        self.assertEqual(common.extract_step_from_line("=== ⚙️ BUILDING ==="), "Building project (xcodebuild)")
        self.assertEqual(common.extract_step_from_line("=== 🧪 TESTING ==="), "Running test suite")
        self.assertEqual(common.extract_step_from_line("=== 👀 REVIEW ==="), "Reviewing changes")
        self.assertEqual(common.extract_step_from_line("=== 🚀 PULL_REQUEST ==="), "Creating pull request")
        self.assertEqual(common.extract_step_from_line("=== ⚙️ DELIVERY: FIREBASE DISTRIBUTION ==="), "Distributing build (Firebase)")
        self.assertEqual(common.extract_step_from_line("=== 🐞 DEBUG 1/8: DIAGNOSING ERRORS ==="), "Debugging (1/8): Diagnosing Errors")

    def test_extract_bracketed_steps(self) -> None:
        self.assertEqual(
            common.extract_step_from_line("[1/3] Planning feature (Stitch AI Mode: Off) using gemini-3.1-pro-preview..."),
            "[1/3] Planning feature"
        )
        self.assertEqual(
            common.extract_step_from_line("[2/3] Creating GitHub issue: Flush Test..."),
            "[2/3] Creating GitHub issue"
        )
        self.assertEqual(
            common.extract_step_from_line("[1/4] Preparing git branch: ai/issue-123..."),
            "[1/4] Preparing git branch"
        )
        self.assertEqual(
            common.extract_step_from_line("[2/4] Updating issue #123 status to 'executing'..."),
            "[2/4] Updating issue #123 status to 'executing'"
        )
        self.assertEqual(
            common.extract_step_from_line("[3/4] Reviewing and validating changes..."),
            "[3/4] Reviewing and validating changes"
        )
        self.assertEqual(
            common.extract_step_from_line("[4/4] Creating pull request and updating issue..."),
            "[4/4] Creating pull request and updating issue"
        )

    def test_extract_compiler_and_tool_steps(self) -> None:
        self.assertEqual(common.extract_step_from_line("CompileSwiftSources normal arm64 ..."), "Compiling Swift sources")
        self.assertEqual(common.extract_step_from_line("Test Suite 'All Tests' started at 2026-09-05"), "Running test suite")
        self.assertEqual(common.extract_step_from_line("      - Running tests: swift test (Attempt 1)"), "Running test suite")
        self.assertEqual(common.extract_step_from_line("      - Running build: xcodebuild (Attempt 1)"), "Building project")
        self.assertEqual(common.extract_step_from_line("        [progress] received 30 lines of response..."), "Generating code")
        self.assertEqual(common.extract_step_from_line("      - Committing 2 modified and 1 untracked files..."), "Committing changes")
        self.assertEqual(common.extract_step_from_line("      - Syncing code to worker mac2..."), "Syncing code to worker")

    def test_extract_step_from_line_test_runner_events(self) -> None:
        self.assertEqual(
            common.extract_step_from_line("Test Case '-[ThemisTests.AlgorithmTests testGreedyMatching]' started."),
            "Testing: AlgorithmTests.testGreedyMatching"
        )
        self.assertEqual(
            common.extract_step_from_line("Test Suite 'AlgorithmTests' started at 2026-09-06 14:00:00.000"),
            "Running suite: AlgorithmTests"
        )
        self.assertEqual(
            common.extract_step_from_line("CompileSwift normal arm64 /Users/dev/Themis/AuthManager.swift (in target 'Themis')"),
            "Compiling AuthManager.swift"
        )
        self.assertEqual(
            common.extract_step_from_line("Fetching https://github.com/google/abseil-cpp-binary.git (cached)"),
            "Fetching abseil-cpp-binary"
        )
        self.assertEqual(
            common.extract_step_from_line("Executed 50 tests, with 0 failures (0 unexpected) in 1.234s"),
            "Finished 50 tests (0 failures)"
        )
        self.assertEqual(
            common.extract_step_from_line("Passing AlgorithmTests.testGreedyMatching (0.012 seconds)"),
            "Passed: AlgorithmTests.testGreedyMatching"
        )
        self.assertEqual(
            common.extract_step_from_line("Failing AlgorithmTests.testGreedyMatching (0.045 seconds)"),
            "Failed: AlgorithmTests.testGreedyMatching"
        )
        self.assertEqual(
            common.extract_step_from_line("Testing on 'iPhone 16' (id=12345)"),
            "Testing on iPhone 16"
        )

    def test_extract_ai_tool_calls(self) -> None:
        self.assertEqual(
            common.extract_step_from_line("        ^[[Z        [stderr] → Read ThemisPlayground/ChatGPTAPI.swift [limit=60, offset=1]"),
            "Reading ChatGPTAPI.swift"
        )
        self.assertEqual(
            common.extract_step_from_line("        [stderr] → Read ThemisPlayground/Models.swift [limit=120, offset=1]"),
            "Reading Models.swift"
        )
        self.assertEqual(
            common.extract_step_from_line("        [stderr] → Edit ThemisPlayground/DocumentProcessorViewModel.swift"),
            "Editing DocumentProcessorViewModel.swift"
        )
        self.assertEqual(
            common.extract_step_from_line("        [stderr] → Write ThemisPlayground/NewView.swift"),
            "Writing NewView.swift"
        )
        self.assertEqual(
            common.extract_step_from_line("        [stderr] → Run command: xcodebuild test -scheme Themis -destination id=SIM"),
            "Running: xcodebuild test"
        )
        self.assertEqual(
            common.extract_step_from_line("        [stderr] → Grep \"riskSummary\""),
            "Searching: riskSummary"
        )
        self.assertEqual(
            common.extract_step_from_line("        [tool] → List directory ThemisPlayground"),
            "Listing ThemisPlayground"
        )

    @patch("os.get_terminal_size", return_value=(45, 24))
    def test_progress_indicator_preserves_label_over_meta(self, _mock_size) -> None:
        indicator = common.ProgressIndicator(label="Generating code changes", hint="Ctrl-C to abort")
        line = indicator.get_line()
        # Should not truncate "Generating code changes" to "Generating co..." on 45 column screen
        self.assertIn("Generating code changes...", line)


class StatusReportTests(unittest.TestCase):
    def test_format_distributed_status(self) -> None:
        from worker_run import format_distributed_status
        
        success_msg = "SUCCESS via Firebase App Distribution (ad-hoc) at 2026-09-05 13:16:28"
        colored_success = format_distributed_status(success_msg)
        self.assertIn("\033[1;92mSUCCESS\033[0m", colored_success)
        self.assertIn("Firebase App Distribution", colored_success)
        
        failed_msg = "FAILED (exit code 1)"
        colored_failed = format_distributed_status(failed_msg)
        self.assertIn("\033[1;91mFAILED\033[0m", colored_failed)
        
        skipped_msg = "Skipped (Firebase distribution disabled)"
        colored_skipped = format_distributed_status(skipped_msg)
        self.assertIn("\033[90mSkipped", colored_skipped)
        
        self.assertEqual(format_distributed_status(None), "")

    @patch("worker_run.sys.stdout.isatty", return_value=True)
    def test_print_status_report_prefers_builder_summary(self, _mock_isatty) -> None:
        from io import StringIO
        from worker_run import print_status_report
        
        job = {
            "job_id": "20260905-130559-bug-79",
            "status": "review-needed",
            "plan": {"summary": "The risk tab fails to display risks. This requires investigating full data flow."},
            "builder_summary": "Fixed model binding in ReportViewModel and added async state listener."
        }
        
        saved_stdout = sys.stdout
        try:
            sys.stdout = out = StringIO()
            print_status_report(
                job,
                build_ok=True,
                test_ok=True,
                pr_number=75,
                pr_url="https://github.com/leemosupreemo/Themis/pull/75",
                distributed_status="SUCCESS via Firebase App Distribution (debugging) at 2026-09-05 13:16:28"
            )
            output = out.getvalue()
        finally:
            sys.stdout = saved_stdout
            
        self.assertIn("Accomplishment:", output)
        self.assertIn("Fixed model binding in ReportViewModel", output)
        self.assertNotIn("This requires investigating full data flow", output)

    @patch("worker_run.sys.stdout.isatty", return_value=True)
    def test_print_status_report_falls_back_to_plan_summary_label(self, _mock_isatty) -> None:
        from io import StringIO
        from worker_run import print_status_report
        
        job = {
            "job_id": "20260905-130559-bug-79",
            "status": "review-needed",
            "plan": {"summary": "The risk tab fails to display risks."}
        }
        
        saved_stdout = sys.stdout
        try:
            sys.stdout = out = StringIO()
            print_status_report(job, build_ok=True, test_ok=True, pr_number=75)
            output = out.getvalue()
        finally:
            sys.stdout = saved_stdout
            
        self.assertIn("Plan Summary:", output)
        self.assertIn("The risk tab fails to display risks.", output)


class ClarificationHelperTests(unittest.TestCase):
    def test_record_clarification_appends_history_and_clears_active_question(self) -> None:
        job = {
            "job_id": "20260905-130559-bug-79",
            "status": "human-needed",
            "human_clarification_question": "Should risks default to high severity?",
            "last_error": "Builder clarification needed: Should risks default to high severity?"
        }
        common.record_clarification(job, "Should risks default to high severity?", "Yes, default to high severity.")
        
        self.assertIsNone(job["human_clarification_question"])
        self.assertIsNone(job["last_error"])
        self.assertEqual(len(job["clarification_history"]), 1)
        self.assertEqual(job["clarification_history"][0]["question"], "Should risks default to high severity?")
        self.assertEqual(job["clarification_history"][0]["answer"], "Yes, default to high severity.")
        self.assertTrue(job["clarification_history"][0]["timestamp"])

    def test_format_clarification_history_renders_markdown(self) -> None:
        history = [
            {"question": "What database framework?", "answer": "SwiftData with schema v2"},
            {"question": "Include dark mode?", "answer": "Yes, support automatic appearance"}
        ]
        markdown = common.format_clarification_history(history)
        self.assertIn("### User Clarifications & Technical Decisions", markdown)
        self.assertIn("1. **Question**: What database framework?", markdown)
        self.assertIn("**Answer / Decision**: SwiftData with schema v2", markdown)
        self.assertIn("2. **Question**: Include dark mode?", markdown)
        self.assertIn("**Answer / Decision**: Yes, support automatic appearance", markdown)

    def test_format_clarification_history_handles_empty(self) -> None:
        self.assertEqual(common.format_clarification_history([]), "")
        self.assertEqual(common.format_clarification_history(None), "")

    def test_record_interactive_investigation_persists_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_output = Path(tmp) / ".orchestrator" / "output"
            old_output = common.OUTPUT_DIR
            common.OUTPUT_DIR = tmp_output
            try:
                job = {
                    "job_id": "test-job-456",
                    "issue_number": 456,
                    "title": "Fix Risk View",
                }
                job = common.record_interactive_investigation(
                    job,
                    tool="Codex CLI",
                    cli_key="codex",
                    duration="1m 45s",
                    notes="Identified bug in RiskViewModel state machine",
                    new_commits=["a1b2c3d Fix state transition in risk tab"],
                    duration_seconds=105,
                )
                self.assertEqual(len(job["interactive_investigations"]), 1)
                inv = job["interactive_investigations"][0]
                self.assertEqual(inv["tool"], "Codex CLI")
                self.assertEqual(inv["cli_key"], "codex")
                self.assertEqual(inv["duration"], "1m 45s")
                self.assertEqual(inv["duration_seconds"], 105)
                self.assertEqual(inv["notes"], "Identified bug in RiskViewModel state machine")
                self.assertEqual(inv["new_commits"], ["a1b2c3d Fix state transition in risk tab"])

                self.assertEqual(len(job["investigation_notes"]), 1)
                self.assertEqual(job["investigation_notes"][0]["note"], "Identified bug in RiskViewModel state machine")

                self.assertTrue(job["updated_at"])
                self.assertTrue(job["llm_sessions"])

                inv_file = tmp_output / "test-job-456" / "investigations.md"
                self.assertTrue(inv_file.exists())
                content = inv_file.read_text(encoding="utf-8")
                self.assertIn("Interactive Investigation Log for Job #456: Fix Risk View", content)
                self.assertIn("## Session 1: Codex CLI", content)
                self.assertIn("Identified bug in RiskViewModel state machine", content)
                self.assertIn("a1b2c3d Fix state transition in risk tab", content)
            finally:
                common.OUTPUT_DIR = old_output

    def test_format_investigation_history_renders_markdown(self) -> None:
        job = {
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "timestamp": "2026-09-06T14:30:00-05:00",
                    "duration": "2m 10s",
                    "notes": "Traced memory leak to unclosed sink",
                    "new_commits": ["1234abc Clean up Combine subscription"],
                },
                {
                    "tool": "Antigravity CLI (agy)",
                    "timestamp": "2026-09-06T14:45:00-05:00",
                    "duration": "45s",
                    "notes": "Verified unit test passes",
                    "new_commits": [],
                }
            ]
        }
        md = common.format_investigation_history(job)
        self.assertIn("### 🔍 Interactive Investigation Findings & CLI Notes", md)
        self.assertIn("1. **Codex CLI | 2m 10s | 2026-09-06 14:30**", md)
        self.assertIn("Traced memory leak to unclosed sink", md)
        self.assertIn("`1234abc Clean up Combine subscription`", md)
        self.assertIn("2. **Antigravity CLI (agy) | 45s | 2026-09-06 14:45**", md)
        self.assertIn("Verified unit test passes", md)

    def test_format_investigation_history_handles_empty(self) -> None:
        self.assertEqual(common.format_investigation_history({}), "")
        self.assertEqual(common.format_investigation_history([]), "")
        self.assertEqual(common.format_investigation_history(None), "")

    def test_parse_test_output_spm_format(self) -> None:
        spm_output = """
Test Suite 'All tests' passed at 2026-09-06 14:00:00.000.
Test Suite 'ThemisTests.xctest' started at 2026-09-06 14:00:00.001.
Test Case '-[ThemisTests.RiskViewModelTests testTabSelection]' failed (0.012 seconds).
Test Case '-[ThemisTests.RiskViewModelTests testActiveFilter]' passed (0.005 seconds).
Executed 15 tests, with 1 failure (0 unexpected) in 0.234 (0.234) seconds
"""
        parsed = common.parse_test_output(spm_output)
        self.assertEqual(parsed["total_run"], 15)
        self.assertEqual(parsed["failed_count"], 1)
        self.assertEqual(parsed["passed_count"], 14)
        self.assertEqual(parsed["failing_tests"], ["RiskViewModelTests.testTabSelection"])

    def test_parse_test_output_unittest_format(self) -> None:
        unit_output = """
FAIL: test_auth_recovery (tests.test_auth.AuthTests.test_auth_recovery)
----------------------------------------------------------------------
Traceback (most recent call last):
  ...
AssertionError: False is not true

----------------------------------------------------------------------
Ran 42 tests in 1.250s

FAILED (failures=1)
"""
        parsed = common.parse_test_output(unit_output)
        self.assertEqual(parsed["total_run"], 42)
        self.assertEqual(parsed["failed_count"], 1)
        self.assertEqual(parsed["passed_count"], 41)
        self.assertEqual(parsed["failing_tests"], ["AuthTests.test_auth_recovery"])

    def test_parse_test_output_xcbeautify_and_swift_testing_format(self) -> None:
        xcbeautify_output = """
✔ AuthViewModelTests.testInitialState (0.005 seconds)
✔ AuthViewModelTests.testValidLogin (0.010 seconds)
✖ -[ThemisTests.AuthViewModelTests testInvalidPassword], failed - Expected error message
Executed 3 tests, with 1 failure (0 unexpected) in 0.050 seconds
"""
        parsed_xc = common.parse_test_output(xcbeautify_output)
        self.assertEqual(parsed_xc["total_run"], 3)
        self.assertEqual(parsed_xc["failed_count"], 1)
        self.assertEqual(parsed_xc["passed_count"], 2)
        self.assertEqual(parsed_xc["failing_tests"], ["AuthViewModelTests.testInvalidPassword"])

        swift_testing_output = """
Test "testDataLoading()" started on 'iPhone 16'
Test "testDataLoading()" passed on 'iPhone 16' (0.012 seconds)
Test "testNetworkErrorRetry()" started on 'iPhone 16'
Test "testNetworkErrorRetry()" failed on 'iPhone 16' (0.030 seconds)
Executed 2 tests, with 1 failure (0 unexpected) in 0.050 seconds
"""
        parsed_st = common.parse_test_output(swift_testing_output)
        self.assertEqual(parsed_st["total_run"], 2)
        self.assertEqual(parsed_st["failed_count"], 1)
        self.assertEqual(parsed_st["passed_count"], 1)
        self.assertEqual(parsed_st["failing_tests"], ["testNetworkErrorRetry"])

    def test_get_job_test_summary_derives_metrics_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            output_dir = tmp_root / ".orchestrator" / "output"
            job_output = output_dir / "test-job-999"
            job_output.mkdir(parents=True)

            test_log = job_output / "test.log"
            test_log.write_text("Executed 10 tests, with 2 failures (0 unexpected)\nTest Case '-[AppTests.ViewTests testRender]' failed\nTest Case '-[AppTests.ViewTests testClick]' failed", encoding="utf-8")

            job = {
                "job_id": "test-job-999",
                "status": "debugging",
                "created_test_count": 3,
                "plan": {
                    "test_recommendations": ["Test render", "Test click", "Test scroll"]
                }
            }

            with patch.object(common, "OUTPUT_DIR", output_dir):
                summary = common.get_job_test_summary(job)

            self.assertEqual(summary["created_count"], 3)
            self.assertEqual(summary["planned_count"], 3)
            self.assertEqual(summary["status"], "failing")
            self.assertEqual(summary["total_run"], 10)
            self.assertEqual(summary["failed_count"], 2)
            self.assertEqual(summary["passed_count"], 8)
            self.assertFalse(summary["tests_ok"])
            self.assertIn("ViewTests.testRender", summary["failing_tests"])
            self.assertIn("ViewTests.testClick", summary["failing_tests"])




class FollowupAndLogTests(unittest.TestCase):
    def test_find_latest_runtime_log_discovers_newest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            output_dir = tmp_root / ".orchestrator" / "output"
            logs_dir = tmp_root / "logs"
            job_output = output_dir / "test-job-123"
            manual_dir = output_dir / "manual"

            job_output.mkdir(parents=True)
            logs_dir.mkdir(parents=True)
            manual_dir.mkdir(parents=True)

            log1 = logs_dir / "20260905-010000-build.log"
            log1.write_text("log 1", encoding="utf-8")

            # Older batch test results should be ignored
            log_batch = logs_dir / "batch_test_results.log"
            log_batch.write_text("batch log", encoding="utf-8")

            # Newest log in job_output
            log2 = job_output / "runtime.log"
            log2.write_text("log 2", encoding="utf-8")

            with patch.object(common, "ROOT", tmp_root), \
                 patch.object(common, "OUTPUT_DIR", output_dir), \
                 patch.object(common, "LOGS_DIR", logs_dir):
                result = common.find_latest_runtime_log("test-job-123")

            self.assertEqual(result, ".orchestrator/output/test-job-123/runtime.log")

    def test_format_log_path_handles_all_timestamp_variants(self) -> None:
        # Compact timestamp
        self.assertEqual(common.format_log_path("20260906-171714"), "2026-09-06 17:17:14")
        self.assertEqual(common.format_log_path("manual/20260906-171714"), "manual/2026-09-06 17:17:14")
        # Hyphenated/underscored timestamp with prefix and extension
        self.assertEqual(common.format_log_path("test_2026-09-05_23-48-26.log"), "test_2026-09-05 23:48:26.log")
        self.assertEqual(common.format_log_path("test_2026-09-05_13-14-07.log"), "test_2026-09-05 13:14:07.log")
        self.assertEqual(common.format_log_path("system_2026-09-05_23-48-26.log"), "system_2026-09-05 23:48:26.log")
        # Standard filenames without timestamp
        self.assertEqual(common.format_log_path("build.log"), "build.log")
        self.assertEqual(common.format_log_path("test.log"), "test.log")

    @patch("debug_job.run_debug_iteration")
    def test_trigger_followup_iteration_updates_job_and_runs_debug(self, mock_debug_iter) -> None:
        import worker_run
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job_path = tmp_root / "job.json"
            job = {
                "job_id": "test-job-123",
                "status": "review-needed",
                "iteration": 1,
                "last_manual_log_paths": ["logs/old.log"]
            }
            common.write_json(job_path, job)

            with patch("worker_run.find_latest_runtime_log", return_value="logs/latest.log"):
                worker_run.trigger_followup_iteration(job_path, job, "Risk tab is not populated")

            saved_job = common.read_json(job_path)
            self.assertEqual(saved_job["status"], "debugging")
            self.assertEqual(saved_job["debug_phase"], "propose")
            self.assertEqual(saved_job["iteration"], 2)
            self.assertIn("logs/latest.log", saved_job["last_manual_log_paths"])
            self.assertEqual(len(saved_job["debug_history"]), 1)
            self.assertEqual(saved_job["debug_history"][0]["implementation_plan"], "Risk tab is not populated")
            mock_debug_iter.assert_called_once_with(job_path)

    @patch("debug_job.run_debug_iteration")
    def test_trigger_followup_iteration_for_feature_job(self, mock_debug_iter) -> None:
        import worker_run
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job_path = tmp_root / "job.json"
            job = {
                "job_id": "test-job-456",
                "type": "feature-plan",
                "status": "review-needed",
                "iteration": 0,
            }
            common.write_json(job_path, job)

            with patch("worker_run.find_latest_runtime_log", return_value=None):
                worker_run.trigger_followup_iteration(job_path, job, "Save button missing loading indicator")

            saved_job = common.read_json(job_path)
            self.assertEqual(saved_job["status"], "debugging")
            self.assertEqual(saved_job["iteration"], 1)
            self.assertEqual(saved_job["debug_history"][0]["hypothesis"], "User reported bug or missing functionality in new feature.")
            self.assertEqual(saved_job["debug_history"][0]["implementation_plan"], "Save button missing loading indicator")
            mock_debug_iter.assert_called_once_with(job_path)

    def test_format_file_link_terminal_hyperlink(self) -> None:
        p = Path("/tmp/export.zip")
        resolved = p.resolve()
        with patch("sys.stdout.isatty", return_value=True):
            link = common.format_file_link(p)
            self.assertIn(f"\033]8;;file://{resolved}\033\\", link)
            self.assertIn(f"{resolved}\033]8;;\033\\", link)

        with patch("sys.stdout.isatty", return_value=False):
            plain = common.format_file_link(p)
            self.assertEqual(plain, f"{resolved} (file://{resolved})")

    def test_get_github_url_returns_cached_or_queries_gh(self) -> None:
        job_pr = {
            "job_id": "test-job",
            "pr_number": 75,
            "pr_url": "https://github.com/leemosupreemo/Themis/pull/75"
        }
        label, url = common.get_github_url(job_pr)
        self.assertEqual(label, "Pull Request #75")
        self.assertEqual(url, "https://github.com/leemosupreemo/Themis/pull/75")

        job_issue = {
            "job_id": "test-job",
            "issue_number": 79,
            "issue_url": "https://github.com/leemosupreemo/Themis/issues/79"
        }
        label_issue, url_issue = common.get_github_url(job_issue)
        self.assertEqual(label_issue, "Issue #79")
        self.assertEqual(url_issue, "https://github.com/leemosupreemo/Themis/issues/79")

    @patch("subprocess.run")
    def test_get_repo_github_base_url(self, mock_run) -> None:
        # Test SSH remote
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "git@github.com:leemosupreemo/Themis.git\n"
        mock_run.return_value = mock_proc

        base = common.get_repo_github_base_url()
        self.assertEqual(base, "https://github.com/leemosupreemo/Themis")

        # Test HTTPS remote
        mock_proc.stdout = "https://github.com/leemosupreemo/Themis.git\n"
        base_https = common.get_repo_github_base_url()
        self.assertEqual(base_https, "https://github.com/leemosupreemo/Themis")

    @patch("common.get_repo_github_base_url", return_value="https://github.com/leemosupreemo/Themis")
    def test_get_github_links_multiple(self, _mock_base) -> None:
        job = {
            "job_id": "test-job",
            "issue_number": 79,
            "pr_number": 75,
        }
        with patch("common.gh_text", side_effect=lambda *args: "https://github.com/leemosupreemo/Themis/pull/75" if args[0] == "pr" else "https://github.com/leemosupreemo/Themis/issues/79"):
            links = common.get_github_links(job)
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0]["label"], "Issue #79")
            self.assertEqual(links[0]["url"], "https://github.com/leemosupreemo/Themis/issues/79")
            self.assertEqual(links[1]["label"], "Pull Request #75")
            self.assertEqual(links[1]["url"], "https://github.com/leemosupreemo/Themis/pull/75")

    @patch("common.get_repo_github_base_url", return_value="https://github.com/leemosupreemo/Themis")
    def test_get_github_url_falls_back_to_issue_when_pr_invalid(self, _mock_base) -> None:
        job = {
            "job_id": "test-job",
            "issue_number": 79,
            "pr_number": 99999,
        }
        # PR view fails, issue view succeeds
        def mock_gh(*args):
            if args[0] == "pr":
                raise subprocess.CalledProcessError(1, ["gh"])
            return "https://github.com/leemosupreemo/Themis/issues/79"

        with patch("common.gh_text", side_effect=mock_gh):
            label, url = common.get_github_url(job)
            self.assertEqual(label, "Issue #79")
            self.assertEqual(url, "https://github.com/leemosupreemo/Themis/issues/79")

    @patch("os.get_terminal_size", return_value=(36, 24))
    def test_print_phase_narrow_screen_does_not_overflow(self, _mock_size) -> None:
        import io
        import re
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            common.print_phase("exporting", subtext="20260905-130559-bug-79")
        output = captured.getvalue()
        # Clean ansi codes
        clean_lines = [re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line) for line in output.split("\n") if line.strip()]
        self.assertTrue(len(clean_lines) >= 1)
        for line in clean_lines:
            self.assertLessEqual(len(line), 36)
        self.assertIn("EXPORTING", clean_lines[0])

    def test_detector_detects_upstream_502_overload_error(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        notices1 = detector.record_line("502 Upstream error from Nvidia: Service temporarily overloaded", now=1000.0)
        self.assertTrue(len(notices1) >= 1)
        self.assertIn("502 Upstream Error", notices1[0])
        self.assertIn("Ctrl-C to abort", notices1[0])

    def test_detector_detects_repeated_tool_access_loop(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        # 3 accesses: no notice yet
        detector.record_line("→ Read ThemisPlayground/Models.swift [limit=60, offset=1]", now=1000.0)
        detector.record_line("→ Read ThemisPlayground/Models.swift [limit=120, offset=60]", now=1001.0)
        detector.record_line("→ Read ThemisPlayground/Models.swift [limit=50, offset=120]", now=1002.0)
        
        # 4th access: loop notice triggered
        notices = detector.record_line("→ Read ThemisPlayground/Models.swift [limit=50, offset=170]", now=1003.0)
        self.assertEqual(len(notices), 1)
        self.assertIn("Potential tool loop detected", notices[0])
        self.assertIn("Models.swift", notices[0])
        self.assertIn("Ctrl-C to abort", notices[0])

    def test_detector_detects_ping_pong_loop(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        detector.record_line("→ Read ThemisPlayground/FileA.swift", now=1000.0)
        detector.record_line("→ Read ThemisPlayground/FileB.swift", now=1001.0)
        detector.record_line("→ Read ThemisPlayground/FileA.swift", now=1002.0)
        detector.record_line("→ Read ThemisPlayground/FileB.swift", now=1003.0)
        detector.record_line("→ Read ThemisPlayground/FileA.swift", now=1004.0)
        notices = detector.record_line("→ Read ThemisPlayground/FileB.swift", now=1005.0)
        
        self.assertTrue(any("Alternating ping-pong loop detected" in n for n in notices))
        self.assertTrue(any("FileA.swift" in n and "FileB.swift" in n for n in notices))

    def test_detector_detects_step_count_milestones(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        for i in range(14):
            detector.record_line(f"→ Read ThemisPlayground/File{i}.swift", now=1000.0 + i)
        
        # 15th step triggers milestone notice
        notices = detector.record_line("→ Read ThemisPlayground/File15.swift", now=1015.0)
        self.assertTrue(any("Agent has executed 15 steps" in n for n in notices))
        self.assertTrue(any("Ctrl-C to abort" in n for n in notices))

    def test_detector_detects_duration_milestone(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        detector.record_line("Compiling Sources", now=1195.0)
        # Not yet at 5 minutes (300s)
        self.assertEqual(detector.check_time_triggers(now=1200.0), [])
        
        # At 5 minutes (1000 + 301 = 1301)
        detector.record_line("Running test suite", now=1300.0)
        notices = detector.check_time_triggers(now=1301.0)
        self.assertEqual(len(notices), 1)
        self.assertIn("Task has been running for 5m", notices[0])
        self.assertIn("Ctrl-C to abort", notices[0])
        
        # Subsequent check before 10m should not repeat 5m notice
        detector.record_line("Running test suite", now=1399.0)
        self.assertEqual(detector.check_time_triggers(now=1400.0), [])

    def test_detector_detects_idle_stall_milestone(self) -> None:
        detector = common.LoopTroubleDetector(start_time=1000.0)
        detector.record_line("Reading sources", now=1000.0)
        
        # 125s idle (2m+)
        notices = detector.check_time_triggers(now=1125.0, last_output_time=1000.0)
        self.assertEqual(len(notices), 1)
        self.assertIn("No output received for 2m", notices[0])
        self.assertIn("Ctrl-C to abort", notices[0])


class ConvergenceAnalysisTests(unittest.TestCase):
    def test_ready_state_with_empty_history(self) -> None:
        job = {"job_id": "test-1", "debug_history": []}
        result = common.analyze_debug_loop_convergence(job)
        self.assertEqual(result["health"], "ready")
        self.assertFalse(result["is_stuck"])

    def test_convergence_detected_when_failing_tests_decrease(self) -> None:
        job = {
            "job_id": "test-2",
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "Fix models",
                    "action": "Updated Models.swift",
                    "result": {"build_ok": True, "tests_ok": False, "failing_tests": ["TestA", "TestB", "TestC"]},
                },
                {
                    "iteration": 2,
                    "hypothesis": "Fix view models",
                    "action": "Updated ViewModel.swift",
                    "result": {"build_ok": True, "tests_ok": False, "failing_tests": ["TestA"]},
                },
            ]
        }
        result = common.analyze_debug_loop_convergence(job)
        self.assertEqual(result["health"], "converging")
        self.assertEqual(result["failing_tests_delta"], -2)
        self.assertFalse(result["is_stuck"])
        self.assertIn("failing tests reduced", result["description"])

    def test_stagnation_detected_when_failing_tests_unchanged_across_attempts(self) -> None:
        job = {
            "job_id": "test-3",
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "Try approach 1",
                    "action": "Edit 1",
                    "result": {"build_ok": True, "tests_ok": False, "failing_tests": ["TestA"]},
                },
                {
                    "iteration": 2,
                    "hypothesis": "Try approach 2",
                    "action": "Edit 2",
                    "result": {"build_ok": True, "tests_ok": False, "failing_tests": ["TestA"]},
                },
            ]
        }
        summary = {"failing_tests": ["TestA"]}
        result = common.analyze_debug_loop_convergence(job, current_test_summary=summary)
        self.assertEqual(result["health"], "stagnant")
        self.assertTrue(result["is_stuck"])
        self.assertGreaterEqual(result["stagnant_streak"], 1)

    def test_oscillation_detected_when_duplicate_hypothesis_repeated(self) -> None:
        job = {
            "job_id": "test-4",
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "Add optional binding in Models.swift to prevent nil crash",
                    "action": "Added if-let",
                    "result": {"build_ok": True, "tests_ok": False},
                },
                {
                    "iteration": 2,
                    "hypothesis": "Remove optional binding because property is non-optional",
                    "action": "Removed if-let",
                    "result": {"build_ok": True, "tests_ok": False},
                },
                {
                    "iteration": 3,
                    "hypothesis": "Add optional binding in Models.swift to prevent nil crash",
                    "action": "Added if-let back",
                    "result": "pending",
                },
            ]
        }
        result = common.analyze_debug_loop_convergence(job)
        self.assertEqual(result["health"], "oscillating")
        self.assertTrue(result["is_stuck"])
        self.assertTrue(result["oscillation_detected"])
        self.assertIn("repeated hypothesis", result["description"])


if __name__ == "__main__":
    unittest.main()
