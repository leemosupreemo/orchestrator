"""Free chat with AI: the project is fed in even when its requirements aren't written, it fits the model's window, and a failure
says why (never just a bracket).

Reported: a free DeepSeek answered as if it knew nothing of the project, and an Alibaba model's failure showed only a bracket."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

from orchestrator import ollama_api, project_chat, project_scan  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402


def write(root: Path, rel: str, text: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def git_project(root: Path):
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "Start the word game"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        project_scan._CACHE.clear()
        self.addCleanup(project_scan._CACHE.clear)

    def files(self):
        return project_scan._walk(self.root)[0]


class WalkTests(ScanTestCase):
    def test_dependencies_build_output_and_tooling_folders_are_skipped(self):
        for rel in ("src/app.ts", "node_modules/dep/index.js", "build/out.js", "Pods/X/x.m", ".git/config", ".orchestrator/project.json", "DerivedData/a.o", "venv/lib/x.py"):
            write(self.root, rel)
        self.assertEqual(self.files(), ["src/app.ts"])

    def test_secrets_are_never_listed(self):
        for rel in (".env", ".env.production", "keys/AuthKey_ABC123DEFG.p8", "certs/server.pem", "id_rsa", "config/credentials.json", "android/app/google-services.json",
                    "ios/GoogleService-Info.plist", "release.keystore", "service-account-prod.json", "terraform.tfvars", ".npmrc", "secrets.yaml", "src/app.ts"):
            write(self.root, rel)
        self.assertEqual(self.files(), ["src/app.ts"])

    def test_a_linked_folder_is_not_followed(self):
        outside = Path(self.temp.name).parent / "outside-folder"
        outside.mkdir(exist_ok=True)
        (outside / "leak.txt").write_text("x")
        self.addCleanup(lambda: (outside / "leak.txt").unlink(missing_ok=True))
        write(self.root, "src/a.py")
        (self.root / "linked").symlink_to(outside)
        self.assertEqual(self.files(), ["src/a.py"])

    def test_a_huge_repository_stops_early_and_says_so(self):
        for i in range(30):
            write(self.root, f"src/f{i}.py")
        with patch.object(project_scan, "MAX_FILES", 10):
            files, truncated = project_scan._walk(self.root)
        self.assertTrue(truncated)
        self.assertLess(len(files), 30)
        self.assertIn("stops early", project_scan.structure(files, 5000, truncated))


class StructureAndLanguageTests(ScanTestCase):
    def test_folders_are_listed_with_their_file_counts_biggest_first(self):
        for rel in ("README.md", "package.json", "src/a.ts", "src/b.ts", "src/ui/c.ts", "src/ui/d.ts", "docs/x.md"):
            write(self.root, rel)
        text = project_scan.structure(self.files(), 5000)
        self.assertIn("Top level files: README.md, package.json", text)
        self.assertLess(text.index("src/ (4 files)"), text.index("docs/ (1 file)"))
        self.assertIn("ui/ (2)", text)

    def test_a_long_listing_is_trimmed_to_its_limit(self):
        for i in range(200):
            write(self.root, f"area{i:03d}/file.py")
        text = project_scan.structure(self.files(), 600)
        self.assertLessEqual(len(text), 640)
        self.assertTrue(text.endswith("…(trimmed)"))

    def test_the_language_mix_counts_source_files(self):
        for rel in ("a.swift", "b.swift", "c.swift", "d.py", "e.ts", "notes.md"):
            write(self.root, rel)
        self.assertEqual(project_scan.languages(self.files())[:2], [("Swift", 3), ("Python", 1)])


class ManifestTests(ScanTestCase):
    def test_package_json_says_what_the_project_is_and_how_it_runs(self):
        write(self.root, "package.json", json.dumps({"name": "word-game", "description": "A word game for two friends", "scripts": {"dev": "vite", "test": "vitest"},
                                                    "dependencies": {"react": "^18", "vite": "^5"}, "devDependencies": {"vitest": "^1"}}))
        text = project_scan.manifests(self.root, self.files())
        for bit in ("name word-game", '"A word game for two friends"', "scripts dev, test", "dependencies react, vite", "1 dev dependencies"):
            self.assertIn(bit, text)

    def test_swift_android_python_rust_go_and_flutter_build_files_are_read(self):
        write(self.root, "Package.swift", 'let package = Package(name: "Lobby", targets: [.target(name: "LobbyCore"), .testTarget(name: "LobbyTests")])')
        write(self.root, "android/settings.gradle", "include ':app'\ninclude ':core'\n")
        write(self.root, "android/app/build.gradle", "plugins { id 'com.android.application'\n id 'kotlin-android' }\nandroid { defaultConfig { applicationId \"com.example.lobby\" } }")
        write(self.root, "pyproject.toml", '[project]\nname = "lobby-server"\ndescription = "The lobby service"\n')
        write(self.root, "Cargo.toml", '[package]\nname = "lobby-cli"\ndescription = "A command line lobby"\n')
        write(self.root, "go.mod", "module example.com/lobby\n\ngo 1.22\n")
        write(self.root, "pubspec.yaml", "name: lobby_app\ndescription: The Flutter lobby\n")
        write(self.root, "Podfile", "pod 'Alamofire'\npod \"SnapKit\"\n")
        write(self.root, "Lobby.xcodeproj/project.pbxproj", "x")
        write(self.root, "Dockerfile", "FROM python")
        text = project_scan.manifests(self.root, self.files())
        for bit in ("name Lobby; targets LobbyCore, LobbyTests", "plugins com.android.application, kotlin-android", "application id com.example.lobby", "modules app, core",
                    'name lobby-server; "The lobby service"', 'name lobby-cli; "A command line lobby"', "module example.com/lobby", 'name lobby_app; "The Flutter lobby"',
                    "Podfile pods: Alamofire, SnapKit", "Xcode project: Lobby", "Runs in Docker"):
            self.assertIn(bit, text)

    def test_a_broken_manifest_is_skipped_not_fatal(self):
        write(self.root, "package.json", "{ not json")
        self.assertEqual(project_scan.manifests(self.root, self.files()), "")


class KeyFileTests(ScanTestCase):
    def test_entry_points_are_chosen_and_tests_dependencies_and_lockfiles_are_not(self):
        write(self.root, "src/main.ts", "export const start = () => 1\n")
        write(self.root, "src/utils/helpers.ts", "export const h = 1\n")
        write(self.root, "tests/main.test.ts", "test('x', () => {})\n")
        write(self.root, "src/app.min.js", "var a=1")
        write(self.root, "package-lock.json", "{}")
        text = project_scan.key_files(self.root, self.files(), 5, 500)
        self.assertIn("#### src/main.ts", text)
        self.assertIn("export const start", text)
        for left_out in ("helpers", "main.test", "app.min", "package-lock"):
            self.assertNotIn(left_out, text)

    def test_the_start_of_a_file_is_quoted_within_its_limit(self):
        write(self.root, "main.py", "\n".join(f"line_{i} = {i}" for i in range(500)))
        text = project_scan.key_files(self.root, self.files(), 3, 300)
        self.assertIn("line_0 = 0", text)
        self.assertNotIn("line_400", text)
        self.assertLess(len(text), 500)

    def test_things_that_look_like_secrets_are_removed_from_the_code_that_is_quoted(self):
        write(self.root, "main.py", 'API_KEY = "abcd1234efgh5678ijkl"\ntoken: "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\nopenai = "sk-abcdefghijklmnopqrstuvwx"\n'
                                    'password = "hunter2hunter2"\nprint("hello")\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----\n')
        text = project_scan.key_files(self.root, self.files(), 3, 2000)
        for secret in ("abcd1234efgh5678ijkl", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "sk-abcdefghijklmnopqrstuvwx", "hunter2hunter2", "MIIEvQIBADANBgkq"):
            self.assertNotIn(secret, text)
        self.assertIn('print("hello")', text)
        self.assertIn("<redacted>", text)

    def test_a_very_large_or_binary_file_is_not_quoted(self):
        write(self.root, "main.py", "x" * 500_000)
        (self.root / "app.swift").write_bytes(b"\x00\x01\x02" * 100)
        self.assertEqual(project_scan.key_files(self.root, self.files(), 5, 500), "")


class ScanSummaryTests(ScanTestCase):
    def test_nothing_secret_appears_anywhere_in_the_scan(self):
        write(self.root, ".env", "DATABASE_URL=postgres://user:pa55w0rd@host/db")
        write(self.root, "config/credentials.json", '{"private_key": "TOPSECRETVALUE"}')
        write(self.root, "main.py", "print('hi')\n")
        everything = json.dumps(project_scan.scan(self.root, "balanced"))
        for secret in ("pa55w0rd", "TOPSECRETVALUE", "DATABASE_URL", "credentials.json", ".env"):
            self.assertNotIn(secret, everything)

    def test_the_result_is_remembered_for_a_couple_of_minutes_and_then_rebuilt(self):
        write(self.root, "main.py", "print(1)\n")
        first = project_scan.scan(self.root, "free", now=1000.0)
        write(self.root, "added/new.py", "print(2)\n")
        self.assertIs(project_scan.scan(self.root, "free", now=1050.0), first)
        self.assertIn("added/", project_scan.scan(self.root, "free", now=1000.0 + project_scan.CACHE_SECONDS + 1)["Project structure"])

    def test_bigger_tiers_quote_more_of_the_code(self):
        write(self.root, "main.py", "\n".join(f"value_{i} = {i}" for i in range(400)))
        sizes = {tier: len(project_scan.scan(self.root, tier)["Key source files (the start of each)"]) for tier in ("free", "balanced", "complex")}
        self.assertLess(sizes["free"], sizes["balanced"])
        self.assertLessEqual(sizes["balanced"], sizes["complex"])


class ChatContextTests(ScanTestCase):
    def project(self, requirements: str | None = None):
        write(self.root, "README.md", "# Lobby\nA two-player word game where friends take turns building words on a shared board.\n")
        write(self.root, "package.json", json.dumps({"name": "lobby", "description": "Word game", "scripts": {"dev": "vite"}}))
        write(self.root, "src/main.ts", "import { Board } from './board'\nexport const game = new Board()\n")
        write(self.root, "src/board.ts", "export class Board {}\n")
        git_project(self.root)
        if requirements is not None:
            from orchestrator import prd
            prd.Prd(self.root, self.root / ".orchestrator").write(prd.replace_section(prd.template(), "pitch", requirements), "you", "Wrote it")

    def context(self, tier="free", **kw):
        return project_chat.project_context(self.root, tier, [{"id": "j1", "title": "Add a timer", "status": "done"}], "Lobby", **kw)

    def test_with_no_requirements_written_the_readme_stack_structure_and_code_are_fed_in(self):
        self.project()
        text = self.context()
        self.assertIn("Not written yet", text)
        self.assertIn("A two-player word game where friends take turns", text)  # the README
        self.assertIn("name lobby", text)  # what it is built with
        self.assertIn("src/ (2 files)", text)  # its structure
        self.assertIn("#### src/main.ts", text)  # the start of its main file
        self.assertIn("export const game = new Board()", text)
        self.assertIn("Start the word game", text)  # its history
        self.assertIn("Add a timer", text)  # its jobs

    def test_written_requirements_are_used_and_the_not_written_note_is_not(self):
        self.project(requirements="A word game where two friends build words together.")
        text = self.context()
        self.assertIn("A word game where two friends build words together.", text)
        self.assertNotIn("Not written yet", text)

    def test_the_sections_come_in_the_order_that_matters_when_requirements_are_missing(self):
        self.project()
        text = self.context()
        order = [text.index(h) for h in ("## Project\n", "## Product requirements", "## What it is built with", "## Project notes and docs",
                                          "## Project structure", "## Recent commits", "## Key source files", "## Jobs")]
        self.assertEqual(order, sorted(order))

    def test_the_free_context_stays_within_its_budget_and_a_given_window(self):
        self.project()
        for i in range(60):
            write(self.root, f"src/module{i}/main.ts", "export const x = 1\n" * 200)
        git_project(self.root)
        self.assertLessEqual(len(self.context("free")), project_chat.CONTEXT_BUDGET["free"] + 200)
        small = self.context("free", max_chars=5_000)
        self.assertLessEqual(len(small), 5_200)
        self.assertIn("## Project\n", small)  # the essentials survive a small window

    def test_a_large_docs_folder_cannot_use_up_the_room_for_the_rest(self):
        """This repository's 49 docs files filled 21.8k of the Free tier's 24k, so its structure, commits and code never reached the model."""
        self.project()
        for i in range(60):
            write(self.root, f"docs/topic{i:02d}.md", f"# Topic {i}\n" + ("Details about this topic. " * 400))
        git_project(self.root)
        text = self.context("free")
        for heading in ("## Project notes and docs", "## Project structure", "## Recent commits", "## Key source files", "## Jobs"):
            self.assertIn(heading, text)
        notes = text[text.index("## Project notes and docs"):text.index("## Project structure")]
        self.assertLessEqual(len(notes), int(project_chat.CONTEXT_BUDGET["free"] * .25) + 400)
        self.assertIn("A two-player word game", notes)  # the README is still first and whole
        self.assertLessEqual(len(text), project_chat.CONTEXT_BUDGET["free"] + 300)

    def test_a_section_that_does_not_fit_does_not_stop_smaller_ones_after_it(self):
        self.project()
        text = self.context("free", max_chars=2_500)
        self.assertIn("## Project\n", text)
        self.assertGreater(len(text), 300)

    def test_an_unreadable_repository_scan_never_stops_a_question(self):
        self.project()
        with patch.object(project_scan, "scan", side_effect=OSError("disk gone")):
            self.assertIn("## Project\n", self.context())

    def test_the_instructions_tell_the_model_what_it_has_and_what_to_do_without_requirements(self):
        prompt = project_chat.build_prompt("CONTEXT", [], "What is this?")
        for bit in ("README", "folder structure", "main source files", "When the requirements aren't written"):
            self.assertIn(bit, prompt)


class LocalModelWindowTests(ScanTestCase):
    def tiers(self, models):
        return {"free": {"available": True, "models": models, "model": models[0], "reason": ""}}

    def test_the_context_cap_is_the_smallest_window_among_the_local_models_of_the_turn(self):
        windows = {"deepseek-coder": 24_000, "qwen2.5-coder": 9_000}
        with patch.object(ollama_api, "window_chars", side_effect=lambda name, **kw: windows.get(name)):
            self.assertEqual(project_chat.context_cap(["deepseek", "qwen2.5-coder"]), 9_000)
            self.assertEqual(project_chat.context_cap(["deepseek"]), 24_000)

    def test_hosted_models_have_no_cap_and_an_unknown_window_is_no_cap(self):
        with patch.object(ollama_api, "window_chars", return_value=None):
            self.assertIsNone(project_chat.context_cap(["deepseek"]))
        self.assertIsNone(project_chat.context_cap(["opencode/beam-free", "claude-sonnet-4-6"]))

    def ask_with(self, cap):
        write(self.root, "README.md", "# Lobby\n" + "More about the lobby. " * 300)
        for i in range(40):
            write(self.root, f"src/m{i}/main.ts", "export const x = 1\n" * 100)
        git_project(self.root)
        seen = []
        llm = lambda prompt, models: (seen.append(prompt) or ("An answer.", models[0]))
        with patch.object(project_chat, "context_cap", return_value=cap), patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": self.temp.name + "/state"}):
            project_chat.ask(self.root, "What is this project?", "free", llm, self.tiers(["deepseek"]), [], "Lobby")
        return seen[0]

    def test_a_local_models_window_decides_how_much_project_is_sent(self):
        small, large = self.ask_with(9_000), self.ask_with(None)
        self.assertLessEqual(len(small), 9_000)
        self.assertGreater(len(large), len(small))
        self.assertIn("What is this project?", small)  # the question is never what gets cut

    def test_the_question_and_the_instructions_are_always_whole(self):
        prompt = self.ask_with(6_000)
        self.assertTrue(prompt.rstrip().endswith("What is this project?"))
        self.assertIn("You answer questions about one software project", prompt)


class ErrorMessageTests(unittest.TestCase):
    def summary(self, text):
        from orchestrator.scripts.model_registry import summarize_cli_error
        return summarize_cli_error(text)

    def test_a_message_inside_json_output_is_what_is_shown(self):
        out = 'opencode failed (code 1):\n{\n  "error": {\n    "message": "Your plan does not include qwen-3.7-max",\n    "code": 403\n  }\n}\n]'
        self.assertEqual(self.summary(out), "Your plan does not include qwen-3.7-max")

    def test_lines_that_are_only_brackets_are_never_the_answer(self):
        for out in ("Something went wrong\n}\n]", "}\n]\n)", "[\n{\n}\n]"):
            with self.subTest(out=out):
                self.assertNotRegex(self.summary(out), r"^[\s\[\]{}()]*$")

    def test_a_traceback_shows_the_exception_not_its_first_line(self):
        self.assertEqual(self.summary('Traceback (most recent call last):\n  File "x.py", line 3\nValueError: unknown model\n}'), "ValueError: unknown model")

    def test_the_old_cases_still_read_the_same(self):
        self.assertEqual(self.summary("Error: rate limit exceeded"), "Error: rate limit exceeded")
        self.assertEqual(self.summary(""), "unknown error")
        self.assertEqual(self.summary("opencode failed (code 1):\nNot signed in. Run opencode auth login."), "Not signed in. Run opencode auth login.")

    def test_the_reason_kept_against_a_failed_model_is_never_a_bracket(self):
        for text in ('{"error": {"message": "model not available on this plan"}}\n}', "Boom\n}\n]"):
            with self.subTest(text=text):
                self.assertNotRegex(project_chat.failure_reason(text), r"^[\s\[\]{}()]*$")

    def test_every_model_failing_gives_a_readable_final_message(self):
        import llm
        errors = []

        def fail(model, *a, **k):
            raise RuntimeError(f'{model} failed (code 1):\n{{\n "error": {{"message": "No access to {model}"}}\n}}\n]')
        with patch.object(llm, "_run_llm_single", side_effect=fail), patch.object(llm, "get_prioritized_models", return_value=["gemini", "deepseek"]):
            try:
                llm.run_llm("deepseek", "hi", allowed_models=["gemini", "deepseek"], raw=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        self.assertEqual(len(errors), 1)
        self.assertNotIn("quota limits reached", errors[0])  # it said so whatever the cause was
        self.assertIn("No access to", errors[0])
        self.assertNotRegex(errors[0].strip().splitlines()[-1], r"^[\s\[\]{}()]*$")


class ModelTurnFailureTests(unittest.TestCase):
    """The real model_turn: a runner that fails and prints a JSON error ending in a bracket (what an Alibaba model's failure looked like)."""

    def failing_runner(self, stderr_text: str):
        code = f"import sys; sys.stdin.read(); sys.stderr.write({stderr_text!r}); sys.exit(1)"
        return lambda *args, **kw: [sys.executable, "-c", code]

    def run_turn(self, stderr_text: str):
        server = type("S", (), {"child_env": lambda self: dict(os.environ)})()
        with tempfile.TemporaryDirectory() as tmp, patch.object(ui, "orchestrator_argv", self.failing_runner(stderr_text)):
            try:
                ui.model_turn(server, Path(tmp), "hello", ["--allow", "opencode/qwen-3.7-max"], timeout=60)
            except ui.UIError as exc:
                return str(exc)
        self.fail("the turn should have failed")

    def test_the_message_is_the_models_own_explanation_not_the_last_line(self):
        message = self.run_turn('No model could answer. Last error: opencode failed (code 1):\n{\n  "error": {"message": "Alibaba token plan: this model needs a plan you do not have"}\n}\n]\n')
        self.assertEqual(message, "Alibaba token plan: this model needs a plan you do not have")

    def test_output_that_ends_in_a_bracket_never_shows_just_the_bracket(self):
        for text in ("Model refused the request\n}\n", "Unexpected response\n]\n", "}\n"):
            with self.subTest(text=text):
                self.assertNotRegex(self.run_turn(text), r"^[\s\[\]{}()]*$")

    def test_a_silent_failure_gets_a_plain_default(self):
        self.assertEqual(self.run_turn(""), "The model couldn't answer.")


class ContextReachesTheLocalModelTests(unittest.TestCase):
    """The whole chain for a local model: the prompt the chat builds is longer than Ollama's default window, and it reaches Ollama
    whole with a window big enough for it (a stand-in Ollama records the request)."""

    def test_a_project_sized_prompt_is_sent_whole_with_a_bigger_window(self):
        from tests.test_ollama_context import FakeOllama
        import io
        from orchestrator.scripts import ollama_generate
        ollama_api._CACHE.clear()
        fake = FakeOllama(context_length=16384)
        self.addCleanup(fake.close)
        self.addCleanup(ollama_api._CACHE.clear)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "README.md", "# Lobby\n" + "A two-player word game. " * 400)
            for i in range(30):
                write(root, f"src/m{i}/main.ts", "export const x = 1\n" * 80)
            git_project(root)
            with patch.dict(os.environ, {"OLLAMA_HOST": fake.address}):
                cap = project_chat.context_cap(["deepseek"])
                prompt = project_chat.build_prompt(project_chat.project_context(root, "free", [], "Lobby", max_chars=cap), [], "What is this project?")
                out = io.StringIO()
                code = ollama_generate.main(["--model", "deepseek-coder"], stdin=io.StringIO(prompt), stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        sent = fake.generate_requests()[0]
        self.assertGreater(len(prompt), ollama_api.RUN_MAX_CHARS)  # too long for `ollama run`'s default window
        self.assertEqual(sent["prompt"], prompt)  # all of it, with the project in front and the question at the end
        self.assertGreater(sent["options"]["num_ctx"], ollama_api.DEFAULT_WINDOW)
        self.assertIn("A two-player word game", sent["prompt"])


if __name__ == "__main__":
    unittest.main()


class RejectedKeyTests(unittest.TestCase):
    """Alibaba Token Plan answered every call with "Invalid API-key provided." The hyphen slipped past the matcher, so the chat
    tried its models one after another, each failing the same way, and only ever showed the raw error."""
    ALIBABA_ERROR = "Error: Invalid API-key provided. For details, see: https://www.alibabacloud.com/help/en/model-studio/error-code#apikey-error"

    def models(self, *ids):
        from orchestrator.scripts.model_registry import get_model
        return [m for m in (get_model(i) for i in ids) if m]

    def test_every_way_of_writing_api_key_is_recognised(self):
        for text in (self.ALIBABA_ERROR, "invalid api_key", "Invalid apikey", "Incorrect API key provided", "invalid x-api-key"):
            with self.subTest(text=text):
                self.assertEqual(project_chat.failure_reason(text), project_chat.KEY_REJECTED)

    def test_other_failures_keep_their_own_reasons(self):
        self.assertEqual(project_chat.failure_reason("rate limit exceeded"), "out of quota for now")
        self.assertEqual(project_chat.failure_reason("model not found"), "not offered to this sign-in")

    def test_the_provider_is_the_part_before_the_slash_and_free_hosts_have_none(self):
        self.assertEqual(project_chat.provider_of("alibaba-token-plan/qwen3.7-max"), "alibaba-token-plan")
        for model_id in ("opencode/beam-free", "ollama/qwen3:8b", "claude-sonnet-4-6", "deepseek"):
            self.assertEqual(project_chat.provider_of(model_id), "")

    def tiers_with(self, checks):
        models = self.models("claude-sonnet-4-6", "gemini-3.5-flash-high", "gpt-5.2")
        extra = [type("M", (), {"id": i, "tier": 2, "cost_factor": 5.0, "family": "opencode", "capabilities": ["coding"], "required_clis": ["opencode"]})()
                 for i in ("alibaba-token-plan/qwen3.7-max", "alibaba-token-plan/glm-5.2")]
        return project_chat.chat_tiers(models + extra, checks=checks, now=1000.0)

    def test_one_rejected_key_takes_every_model_of_that_provider_out_of_the_tier(self):
        tiers = self.tiers_with({"alibaba-token-plan/glm-5.2": {"ok": False, "reason": project_chat.KEY_REJECTED, "at": 990.0}})
        for tier in tiers.values():
            self.assertFalse([m for m in tier["models"] if m.startswith("alibaba-token-plan/")])
        skipped = {x["model"]: x["reason"] for t in tiers.values() for x in t["skipped"]}
        self.assertEqual(skipped.get("alibaba-token-plan/qwen3.7-max"), project_chat.KEY_REJECTED)  # never tried, and says why
        self.assertEqual(skipped.get("alibaba-token-plan/glm-5.2"), project_chat.KEY_REJECTED)

    def test_other_providers_and_a_failure_that_is_not_the_key_are_left_alone(self):
        tiers = self.tiers_with({"alibaba-token-plan/glm-5.2": {"ok": False, "reason": "out of quota for now", "at": 990.0}})
        balanced = tiers["balanced"]["models"] + [x["model"] for x in tiers["balanced"]["skipped"]]
        self.assertIn("alibaba-token-plan/qwen3.7-max", tiers["balanced"]["models"] + tiers["balanced"]["candidates"])  # quota isn't a bad key
        self.assertTrue(any(m.startswith("claude-") or m.startswith("gemini-") or m.startswith("gpt-") for m in balanced))

    def test_the_rejection_is_forgotten_once_the_hold_is_over(self):
        tiers = self.tiers_with({"alibaba-token-plan/glm-5.2": {"ok": False, "reason": project_chat.KEY_REJECTED, "at": 1000.0 - project_chat.FAIL_HOLD - 5}})
        self.assertIn("alibaba-token-plan/qwen3.7-max", sum((t["candidates"] for t in tiers.values()), []))

    def test_checking_stops_asking_a_provider_whose_key_was_just_refused(self):
        models = [type("M", (), {"id": i, "tier": 2, "cost_factor": 5.0, "family": "opencode", "capabilities": ["coding"], "required_clis": ["opencode"]})()
                  for i in ("alibaba-token-plan/glm-5.2", "alibaba-token-plan/glm-5.3", "alibaba-token-plan/qwen3.7-max")]
        asked = []

        def probe(model):
            asked.append(model)
            return False, self.ALIBABA_ERROR if False else project_chat.KEY_REJECTED, ""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": tmp}):
            project_chat.check_tiers(models, probe, retry_after=0)
        self.assertEqual(len([m for m in asked if m == asked[0]]), 2)  # the one retry, as before
        self.assertEqual(sorted(set(asked)), [asked[0]])  # and none of the provider's other models
