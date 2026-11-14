"""A provider whose API key was refused: left out of Ask AI with a notice and a link, and shown on the AI tools page with the
way to fix it and a button to check again."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
APP = (STATIC / "app.js").read_text()
CONFIG = (STATIC / "configuration.js").read_text()


class AskAiNoticeTests(unittest.TestCase):
    def test_the_notice_names_the_provider_and_links_to_ai_tools(self):
        fn = APP[APP.index("function askAiProblemsHtml("):]
        fn = fn[:fn.index("\n}\n")]
        for bit in ("esc(x.name)", "is left out: its API key was rejected", "The other AI tools still work.", 'href="#/config/ai"', "Fix in AI tools", "data-ask-ai-fix"):
            self.assertIn(bit, fn)

    def test_the_notice_has_a_place_in_the_dialog_and_is_drawn_with_the_tiers(self):
        self.assertIn('<div class="ask-ai-problems" aria-live="polite" hidden></div>', APP)
        draw = APP[APP.index("const drawTiers = ("):][:900]
        self.assertIn("askAiProblemsHtml(data.problems)", draw)
        self.assertIn("problemBox.hidden = !(data.problems || []).length", draw)

    def test_the_notice_is_refreshed_after_a_model_check_and_after_a_failed_question(self):
        self.assertIn("data.problems = result.problems || data.problems;", APP)
        self.assertIn("data.problems = fresh.problems;", APP)

    def test_following_the_link_closes_the_chat_so_ai_tools_is_what_you_see(self):
        self.assertIn('e.target.closest("[data-ask-ai-fix]")) dlg.close()', APP)


class AiToolsPageTests(unittest.TestCase):
    card = CONFIG[CONFIG.index("const problemCard ="):CONFIG.index("const secondary =")]

    def test_a_needs_attention_card_comes_first_and_only_when_there_is_a_problem(self):
        self.assertIn("problems.length ? `<section", self.card)
        self.assertIn("<h2>Needs attention</h2>", self.card)
        page = CONFIG[CONFIG.index('title: "Manage AI tools"'):]
        self.assertLess(page.index("${problemCard}"), page.index("ai-free-card"))
        self.assertLess(page.index("${problemCard}"), page.index('providerGroup("Installed"'))

    def test_it_says_which_provider_why_how_many_models_and_what_to_run(self):
        for bit in ("its API key was rejected", "left out of Ask AI", "escapeHtml(x.fix)", "<code>opencode auth login</code>", 'data-setup-copy="opencode auth login"'):
            self.assertIn(bit, self.card)

    def test_only_an_owner_can_ask_for_a_new_check(self):
        self.assertIn("${owner ? `<button type=\"button\" class=\"btn small primary\" data-ai-recheck=", self.card)
        self.assertIn("I've fixed it, check again", self.card)

    def test_checking_again_forgets_the_failure_asks_the_models_and_reports_the_outcome(self):
        handler = APP[APP.index("const onRecheck = async"):][:1500]
        self.assertIn('api("ai-providers/recheck"', handler)
        self.assertIn('api("chat/check"', handler)
        self.assertIn("Its key is still rejected.", handler)
        self.assertIn("it is back in Ask AI", handler)
        self.assertIn("button.disabled = false;", handler)  # a failed check leaves the button usable
        self.assertIn("view.addEventListener(\"click\", onRecheck)", APP)
        self.assertIn("cleanup.push(() => view.removeEventListener(\"click\", onRecheck))", APP)


class StylesTests(unittest.TestCase):
    def test_the_notice_is_styled_as_a_warning_row(self):
        css = (STATIC / "style.css").read_text()
        rule = re.search(r"\.ask-ai-problem \{([^}]*)\}", css).group(1)
        for bit in ("display: flex", "var(--warn-soft", "var(--warn"):
            self.assertIn(bit, rule)


if __name__ == "__main__":
    unittest.main()
