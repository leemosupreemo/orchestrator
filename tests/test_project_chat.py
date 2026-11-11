"""Ask AI on Home: which models answer for Free, Balanced and Complex, what they're told, and that a tier never borrows."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import project_chat as pc  # noqa: E402
from orchestrator.scripts.model_registry import ModelTier  # noqa: E402

from tests.test_web_ui import ServerTestCase, UI_HEADERS, ui  # noqa: E402


def model(id, tier, cost):
    return SimpleNamespace(id=id, tier=tier, cost_factor=cost)


CATALOG = [
    model("opus", ModelTier.EXTREME, 10.0), model("fable", ModelTier.EXTREME, 8.0),
    model("sonnet", ModelTier.HIGH, 5.0), model("gpt-mini", ModelTier.MEDIUM, 1.0), model("pricey-pro", ModelTier.HIGH, 9.0),
    model("ollama-big", ModelTier.HIGH, 0.0), model("free-flash", ModelTier.LOW, 0.0), model("copilot", ModelTier.HIGH, 0.0),
]


class TierTests(unittest.TestCase):
    def test_each_model_lands_in_one_tier(self):
        tiers = pc.chat_tiers(CATALOG)
        self.assertEqual(set(tiers["free"]["models"]) | {"free-flash"}, {"ollama-big", "copilot", "free-flash"})
        self.assertEqual(tiers["complex"]["models"], ["opus", "fable"])  # the strongest first
        self.assertEqual(tiers["balanced"]["model"], "sonnet")  # good before cheap; the cheaper of equals first
        self.assertEqual(tiers["balanced"]["models"], ["sonnet", "pricey-pro", "gpt-mini"])
        self.assertEqual(tiers["free"]["model"], "copilot")  # best quality among the free ones (ties by name)

    def test_free_never_includes_a_model_that_costs_anything(self):
        free = pc.chat_tiers(CATALOG)["free"]["models"]
        costs = {m.id: m.cost_factor for m in CATALOG}
        self.assertTrue(free)
        self.assertTrue(all(costs[m] == 0.0 for m in free))

    def test_an_empty_tier_says_why_and_borrows_nothing(self):
        tiers = pc.chat_tiers([model("sonnet", ModelTier.HIGH, 5.0)])
        for tier in ("free", "complex"):
            self.assertFalse(tiers[tier]["available"])
            self.assertEqual(tiers[tier]["models"], [])
            self.assertEqual(tiers[tier]["model"], "")
            self.assertTrue(tiers[tier]["reason"])
        self.assertEqual(pc.default_tier(tiers), "balanced")
        self.assertEqual(pc.default_tier(pc.chat_tiers([model("f", ModelTier.LOW, 0.0)])), "free")

    def test_a_tier_keeps_a_couple_of_fallbacks_at_most(self):
        many = [model(f"x{i}", ModelTier.EXTREME, 9.0) for i in range(6)]
        self.assertEqual(len(pc.chat_tiers(many)["complex"]["models"]), 1 + pc.MAX_FALLBACKS)


def make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Tester")
    (root / "README.md").write_text("# Snake\n\n## Install\nRun it.\n")
    (root / "docs").mkdir()
    (root / "docs" / "design.md").write_text("# Design\nThe board is 20 by 20.\n")
    run("add", ".")
    run("commit", "-q", "-m", "Add the board")


@unittest.skipUnless(shutil.which("git"), "git required")
class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snake"
        make_repo(self.root)
        self.prd = patch("orchestrator.prd.read_text", return_value="# Snake\n\n## Pitch\nA Nokia snake game for phones.\n")
        self.prd.start()

    def tearDown(self):
        self.prd.stop()
        self.tmp.cleanup()

    def test_the_project_goes_with_every_question(self):
        ctx = pc.project_context(self.root, "balanced", [{"title": "Add score", "state": {"label": "Planned"}, "branch": "feat/score"}], "Snake")
        for expected in ("Name: Snake", "Current branch: main", "A Nokia snake game for phones.", "Add the board", "Tester",
                         "Add score (Planned) · branch feat/score", "#### README.md", "The board is 20 by 20."):
            self.assertIn(expected, ctx)
        # A quoted document's headings sit below the context's own sections.
        self.assertNotIn("\n## Install", ctx)
        self.assertNotIn("\n## Pitch", ctx)
        self.assertIn("###### Install", ctx)

    def test_it_stays_within_the_tier_budget(self):
        (self.root / "docs" / "huge.md").write_text("word " * 50_000)
        for tier in pc.TIERS:
            self.assertLessEqual(len(pc.project_context(self.root, tier)), pc.CONTEXT_BUDGET[tier] + 50)
        self.assertLess(pc.CONTEXT_BUDGET["free"], pc.CONTEXT_BUDGET["balanced"])  # free models tend to have small windows

    def test_any_model_gets_the_conversation_and_the_question_last(self):
        prompt = pc.build_prompt("## Project\nName: Snake", [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}], "What next?")
        self.assertIn("User: Hi\nAssistant: Hello", prompt)
        self.assertTrue(prompt.rstrip().endswith("# New question\nWhat next?"))
        self.assertLess(prompt.index("Name: Snake"), prompt.index("# Conversation so far"))


@unittest.skipUnless(shutil.which("git"), "git required")
class AskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snake"
        make_repo(self.root)
        self.tiers = pc.chat_tiers(CATALOG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_chosen_tiers_models_are_asked_and_the_answer_is_saved(self):
        asked = []

        def llm(prompt, models):
            asked.append(models)
            return "It's a snake game.", models[1]

        thread = pc.ask(self.root, "What is this?", "free", llm, self.tiers)
        self.assertEqual(asked, [self.tiers["free"]["models"]])
        self.assertEqual([m["role"] for m in thread], ["user", "assistant"])
        self.assertEqual(thread[1]["model"], self.tiers["free"]["models"][1])  # the one that actually answered
        self.assertEqual(thread[1]["tier"], "free")
        self.assertEqual(pc.messages(self.root), thread)  # saved with the project
        pc.ask(self.root, "And then?", "complex", llm, self.tiers)
        self.assertEqual(len(pc.messages(self.root)), 4)  # one conversation, whichever kind of AI answers
        pc.clear(self.root)
        self.assertEqual(pc.messages(self.root), [])

    def test_a_tier_with_nothing_set_up_refuses_instead_of_upgrading(self):
        tiers = pc.chat_tiers([model("sonnet", ModelTier.HIGH, 5.0)])
        called = []
        with self.assertRaises(pc.ChatError) as caught:
            pc.ask(self.root, "Hi", "free", lambda p, m: called.append(m) or ("x", ""), tiers)
        self.assertIn("No free model", str(caught.exception))
        self.assertEqual(called, [])

    def test_empty_long_and_unknown_requests_are_refused(self):
        llm = lambda p, m: ("x", "")
        for question, tier in (("  ", "free"), ("x" * (pc.MAX_MESSAGE_CHARS + 1), "free"), ("Hi", "turbo")):
            with self.assertRaises(pc.ChatError):
                pc.ask(self.root, question, tier, llm, self.tiers)


class AllowListTests(unittest.TestCase):
    """job_chat_run.py --allow: the model runner never steps outside the list it's given."""

    def run_main(self, argv):
        sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))
        import job_chat_run
        seen = {}

        def fake_run_llm(model, prompt, **kw):
            seen.update(model=model, allowed=kw.get("allowed_models"))
            return "answer", kw["allowed_models"][-1], "s"

        out = io.StringIO()
        with patch.object(job_chat_run, "run_llm", fake_run_llm), \
             patch.object(job_chat_run, "get_prioritized_models", return_value=["opus", "sonnet", "free-flash"]), \
             patch("sys.stdin", io.StringIO("prompt")), redirect_stdout(out):
            code = job_chat_run.main(argv)
        return code, seen, out.getvalue()

    def test_only_the_allowed_models_are_tried_in_order(self):
        code, seen, out = self.run_main(["--allow", "free-flash,ollama-big"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["model"], "free-flash")
        self.assertEqual(seen["allowed"], ["free-flash", "ollama-big"])  # never opus or sonnet
        self.assertIn("<<<ORCHESTRATOR-MODEL>>>ollama-big", out)  # says which one answered

    def test_without_an_allow_list_it_works_as_before(self):
        code, seen, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(seen["model"], "opus")


class EndpointTests(ServerTestCase):
    def tiers(self):
        return patch.object(pc, "ready_models", return_value=CATALOG)

    def test_the_page_gets_the_conversation_and_what_each_tier_would_use(self):
        with self.tiers():
            res, data = self.request("GET", "/api/chat")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["tiers"]["complex"]["model"], "opus")
        self.assertEqual(data["default"], "balanced")

    def test_a_question_runs_in_the_background_with_only_its_tiers_models(self):
        calls = []

        def fake(server, root, prompt, models, timeout=240, progress=None):
            calls.append((prompt, models))
            return "A snake game.", models[0]

        with self.tiers(), patch.object(ui, "chat_call", fake):
            res, data = self.request("POST", "/api/chat", body={"message": "What is this?", "tier": "complex"}, headers=UI_HEADERS)
            self.assertEqual(res.status, 202)
            for _ in range(100):
                _, task = self.request("GET", f"/api/product/task/{data['task']}")
                if task["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(task["status"], "done", task)
        self.assertEqual(calls[0][1], ["opus", "fable"])
        self.assertIn("# New question\nWhat is this?", calls[0][0])
        self.assertEqual(task["result"]["messages"][1]["text"], "A snake game.")
        _, data = self.request("POST", "/api/chat/clear", body={}, headers=UI_HEADERS)
        self.assertEqual(data["messages"], [])

    def test_an_unavailable_tier_is_refused_at_once(self):
        with patch.object(pc, "ready_models", return_value=[model("sonnet", ModelTier.HIGH, 5.0)]):
            res, data = self.request("POST", "/api/chat", body={"message": "Hi", "tier": "free"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("No free model", data["error"])


class PageTests(unittest.TestCase):
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"

    def test_the_chat_button_sits_opposite_the_branch_picker(self):
        app = (self.STATIC / "app.js").read_text()
        line = app[app.index("function statusLine"):][:2000]
        self.assertIn('class="ask-ai-btn" data-ask-ai aria-label="Ask AI about this project"', line)
        self.assertIn('<symbol id="i-chat"', (self.STATIC / "index.html").read_text())
        panel = app[app.index("async function openAskAi"):][:9000]
        self.assertIn('name="ask-ai-tier"', app[app.index("function askAiTierButton"):][:600])
        for bit in ('api("chat")', "waitForTask(started", "Ask AI needs a newer Orchestrator on this computer",
                    "ASK_AI_TIER_KEY", 'send.textContent = "Stop"'):
            self.assertIn(bit, panel)


if __name__ == "__main__":
    unittest.main()
