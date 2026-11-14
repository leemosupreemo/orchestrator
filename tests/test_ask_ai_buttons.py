"""The Ask AI chat's send button: an up arrow with no text, a square while it is thinking (to stop), on the same row as the
question box at the far right. Sending and stopping are one button, so only its icon and accessible name change."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"


def ask_ai_source() -> str:
    app = (STATIC / "app.js").read_text()
    return app[app.index("async function openAskAi()"):]


class AskAiButtonMarkupTests(unittest.TestCase):
    def form(self) -> str:
        source = ask_ai_source()
        start = source.index('<form class="ask-ai-form">')
        return source[start:source.index("</form>", start)]

    def test_the_question_box_and_the_button_are_one_row(self):
        form = self.form()
        row = form[form.index('<div class="ask-ai-compose">'):]
        row = row[:row.index("</div>")]
        self.assertIn("<textarea", row)
        self.assertIn('type="submit"', row)
        self.assertLess(row.index("<textarea"), row.index('type="submit"'))  # the button follows the box: to its right

    def test_the_button_is_an_up_arrow_with_no_text_and_a_name_for_screen_readers(self):
        form = self.form()
        button = form[form.index("<button"):form.index("</button>") + len("</button>")]
        self.assertIn('href="#i-arrow-up"', button)
        self.assertIn('aria-label="Send"', button)
        self.assertIn('title="Send"', button)
        self.assertEqual(re.sub(r"<[^>]+>", "", button).strip(), "")  # nothing but the icon

    def test_the_hint_is_under_the_row_not_beside_the_button(self):
        form = self.form()
        self.assertLess(form.index('class="ask-ai-compose"'), form.index("ask-ai-status"))
        self.assertNotIn("clarify-reply-actions", form)

    def test_stopping_changes_the_icon_not_the_text(self):
        source = ask_ai_source()
        self.assertNotIn('send.textContent = "Stop"', source)
        self.assertNotIn('send.textContent = "Send"', source)
        self.assertIn("sendMode(true);", source)
        self.assertIn("sendMode(false);", source)

    def test_the_icons_exist_in_the_page_sprite(self):
        html = (STATIC / "index.html").read_text()
        self.assertRegex(html, r'<symbol id="i-arrow-up"')
        self.assertRegex(html, r'<symbol id="i-stop"')

    def test_the_css_puts_the_button_inside_the_field_at_its_bottom_right(self):
        css = (STATIC / "style.css").read_text()
        compose = re.search(r"\.ask-ai-compose \{([^}]*)\}", css).group(1)
        for bit in ("position: relative", "border: 1px solid", "border-radius: var(--r-lg)"):
            self.assertIn(bit, compose)  # the field's own border and rounding: the box the button sits in
        send = re.search(r"\.ask-ai-send \{([^}]*)\}", css).group(1)
        for bit in ("position: absolute", "right: var(--space-2)", "bottom: var(--space-2)", "border-radius: 50%"):
            self.assertIn(bit, send)
        text = re.search(r"\.ask-ai-form \.ask-ai-compose textarea \{([^}]*)\}", css).group(1)
        self.assertIn("border: 0", text)  # one border around both, not a box inside a box
        self.assertIn("padding: var(--space-3) calc(32px", text)  # the words stop short of the button
        self.assertIn(".ask-ai-send.is-stop", css)


class AskAiHeaderTests(unittest.TestCase):
    """The top of the chat is the project's title dropdown, centred, like Home's: no "Ask AI" heading and no "About <project>" line."""

    def header(self) -> str:
        source = ask_ai_source()
        start = source.index('<header class="ask-ai-head">')
        return source[start:source.index("</header>", start)]

    def test_there_is_no_about_line_and_no_ask_ai_heading(self):
        header = self.header()
        self.assertNotIn("About ", header)
        self.assertNotIn(">Ask AI<", header)

    def test_the_header_holds_the_project_dropdown_between_an_empty_cell_and_the_buttons(self):
        header = self.header()
        self.assertIn("homeProjectTitle(", header)
        self.assertLess(header.index('aria-hidden="true"></span>'), header.index("ask-ai-project"))
        self.assertLess(header.index("ask-ai-project"), header.index("data-ask-ai-clear"))

    def test_the_dropdown_is_centred_by_a_three_column_header(self):
        css = (STATIC / "style.css").read_text()
        head = re.search(r"\.ask-ai-head \{([^}]*)\}", css).group(1)
        self.assertIn("grid-template-columns: 1fr auto 1fr", head)
        self.assertIn("justify-self: center", re.search(r"\.ask-ai-head h2 \{([^}]*)\}", css).group(1))

    def test_the_dialog_still_has_a_name_for_screen_readers(self):
        self.assertIn('dlg.setAttribute("aria-label", "Ask AI")', ask_ai_source() + (STATIC / "app.js").read_text())

    def test_switching_project_in_the_chat_loads_that_projects_conversation(self):
        source = ask_ai_source()
        handler = source[source.index("projectSelect.addEventListener"):]
        handler = handler[:handler.index("const form =")]
        self.assertIn("switchProject(", handler)
        self.assertIn("openAskAi()", handler)  # and not the previous project's thread
        self.assertIn('"__manage__"', handler)
        self.assertIn("github:", handler)


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class AskAiSendModeTests(unittest.TestCase):
    """sendMode, the function that turns the arrow into the square and back, run against a stand-in button."""

    def run_modes(self) -> dict:
        source = ask_ai_source()
        start = source.index("const sendMode = (stopping) => {")
        function = source[start:source.index("\n  };", start) + len("\n  };")]
        script = r"""
const make = () => { const attrs = {}; const use = {href: '#i-arrow-up', setAttribute(k, v) { this.href = v; }};
  return {title: 'Send', classes: new Set(), attrs, use, setAttribute(k, v) { attrs[k] = v; },
    classList: {toggle: (name, on) => { on ? button.classes.add(name) : button.classes.delete(name); }}, querySelector: () => use}; };
const button = make(); const send = button;
""" + function + r"""
const state = () => ({stopping: button.classes.has('is-stop'), label: button.attrs['aria-label'], title: button.title, icon: button.use.href});
const out = {};
sendMode(true); out.stopping = state();
sendMode(false); out.sending = state();
sendMode(true); sendMode(true); out.twice = state();
console.log(JSON.stringify(out));
"""
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_square_means_stop_and_the_arrow_means_send(self):
        out = self.run_modes()
        self.assertEqual(out["stopping"], {"stopping": True, "label": "Stop", "title": "Stop", "icon": "#i-stop"})
        self.assertEqual(out["sending"], {"stopping": False, "label": "Send", "title": "Send", "icon": "#i-arrow-up"})

    def test_asking_for_the_same_mode_twice_changes_nothing_more(self):
        out = self.run_modes()
        self.assertEqual(out["twice"], out["stopping"])


class AskAiClearTests(unittest.TestCase):
    """Clear (top right of the chat) opens a confirmation modal on the first click. It used to turn into "Clear for everyone?" and
    wait for a second tap. Confirming clears this Ask AI conversation and nothing else."""

    def handler(self) -> str:
        source = ask_ai_source()
        start = source.index("clearBtn.addEventListener")
        return source[start:source.index("\n}\n", start)]

    def test_clear_is_a_trash_icon_with_no_text_and_a_name_for_screen_readers(self):
        source = ask_ai_source()
        start = source.index("<button", source.index("ask-ai-head-actions"))
        button = source[start:source.index("</button>", start) + len("</button>")]
        self.assertIn("data-ask-ai-clear", button)
        self.assertIn('href="#i-trash"', button)
        self.assertIn('aria-label="Clear conversation"', button)
        self.assertIn('title="Clear conversation"', button)
        self.assertEqual(re.sub(r"<[^>]+>", "", button).strip(), "")  # nothing but the icon

    def test_the_icon_exists_in_the_page_sprite(self):
        self.assertRegex((STATIC / "index.html").read_text(), r'<symbol id="i-trash"')

    def test_the_first_click_opens_a_confirmation_modal_not_a_second_tap(self):
        source = ask_ai_source()
        self.assertNotIn("Clear for everyone?", source)
        self.assertNotIn("dataset.armed", source)
        handler = self.handler()
        self.assertIn("formDialog(", handler)
        self.assertLess(handler.index("formDialog("), handler.index('api("chat/clear"'))  # asked before anything is removed

    def test_the_modal_is_a_danger_confirmation_that_can_be_cancelled(self):
        handler = self.handler()
        self.assertIn("danger: true", handler)
        self.assertIn("Clear conversation", handler)
        self.assertRegex(handler, r"if \(answer === null\) return;")  # cancelling does nothing

    def test_it_says_what_is_removed_and_what_is_not(self):
        handler = self.handler()
        for bit in ("can't be undone", "Job chats", "product requirements"):
            self.assertIn(bit, handler)

    def test_clearing_while_a_question_is_being_answered_is_refused(self):
        self.assertIn("if (askAiRun)", self.handler().split("formDialog(")[0])

    def test_only_the_chat_endpoint_is_called(self):
        self.assertEqual(re.findall(r'api\("([^"]+)"', self.handler()), ["chat/clear"])


class AskAiClearServerTests(unittest.TestCase):
    def test_clearing_removes_only_the_project_conversation(self):
        import tempfile
        from orchestrator import project_chat
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator").mkdir()
            project_chat._save(root, [{"role": "user", "text": "hi", "tier": "free"}])
            other = project_chat.thread_path(root).parent / "job-1.json"
            other.write_text("{}")
            project_chat.clear(root)
            self.assertEqual(project_chat.messages(root), [])
            self.assertTrue(other.exists())
            project_chat.clear(root)  # clearing an empty chat is fine


if __name__ == "__main__":
    unittest.main()
