"""Every inactive button says why, very briefly, as its tooltip; every required form field shows a red asterisk.

Buttons give their reason in `data-why` (or their own `title`); disabled-reasons.js shows it only while the button is
disabled, and says a button disabled by code mid-request is working. The scan below fails when a button that can be disabled
has no reason, and when code disables a button on a condition without setting one."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
SCRIPTS = sorted(p for p in STATIC.glob("*.js") if p.name != "firebase-messaging-sw.js")


def button_tags(src: str):
    """Each <button>/<input> tag in a page script, following ${…} expressions so a `>` inside one doesn't end the tag."""
    for m in re.finditer(r"<(button|input)\b", src):
        i, depth, quote = m.end(), 0, None
        while i < len(src):
            c = src[i]
            if depth:
                if quote:
                    if c == "\\":
                        i += 2
                        continue
                    if c == quote:
                        quote = None
                elif c in "'\"`":
                    quote = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
            elif src.startswith("${", i):
                depth = 1
                i += 2
                continue
            elif c in ">`":
                break
            i += 1
        yield src.count("\n", 0, m.start()) + 1, src[m.start():i + 1]


class DisabledReasonTests(unittest.TestCase):
    def test_every_button_that_can_be_disabled_says_why(self):
        missing = []
        for path in SCRIPTS:
            for line, tag in button_tags(path.read_text(encoding="utf-8")):
                if not re.search(r"\bdisabled\b", tag) or re.search(r"\b(title|data-why)=", tag):
                    continue
                if tag.startswith("<input") and not re.search(r'type="(submit|button)"', tag):
                    continue
                missing.append(f"{path.name}:{line}: {' '.join(tag.split())[:120]}")
        self.assertEqual(missing, [], "Give each a short data-why reason")

    def test_code_that_disables_a_button_on_a_condition_sets_its_reason(self):
        missing = []
        for path in SCRIPTS:
            lines = path.read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines):
                m = re.search(r"(\w+)\.disabled = (?!true\b|false\b)", line)
                if not m or "Select" in m.group(1) or m.group(1) in ("input", "target"):  # selects and inputs aren't buttons
                    continue
                near = "\n".join(lines[max(0, n - 4):n + 4])
                if "dataset.why" in near or self.markup_has_reason(path, m.group(1), lines[:n + 1]):
                    continue
                missing.append(f"{path.name}:{n + 1}: {line.strip()[:120]}")
        self.assertEqual(missing, [], "Set el.dataset.why next to the condition")

    @staticmethod
    def markup_has_reason(path: Path, name: str, before: list[str]) -> bool:
        """The button the variable holds (found by its id, the latest assignment before the line) gives its reason in its markup."""
        src = path.read_text(encoding="utf-8")
        found = [m for line in before for m in [re.search(rf"\b{name}\b\s*=\s*[^;]*?(?:#([\w-]+)|\[(data-[\w-]+))", line)] if m]
        if not found:
            return False
        ident, attr = found[-1].groups()
        mark = f'id="{ident}"' if ident else f"{attr}="
        return any(mark in tag and "data-why=" in tag for _, tag in button_tags(src))

    def test_the_watcher_loads_before_the_page_scripts(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="html.js"'), html.index('src="disabled-reasons.js"'))
        self.assertLess(html.index('src="disabled-reasons.js"'), html.index('src="app.js"'))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_watcher_shows_the_reason_only_while_disabled_and_restores_the_old_tooltip(self):
        script = (STATIC / "disabled-reasons.js").read_text(encoding="utf-8") + r"""
function el({ why, title, text = "Save", busy = false, disabled = true } = {}) {
  const attrs = {};
  if (title) attrs.title = title;
  if (busy) attrs["aria-busy"] = "true";
  return { dataset: why ? { why } : {}, disabled, textContent: text, matches: () => true, querySelector: () => null,
    getAttribute: (k) => attrs[k] ?? null, setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: (k) => { delete attrs[k]; }, attrs };
}
const R = globalThis.DisabledReasons, out = {};
const a = el({ why: "Sign in to GitHub first." }); R.apply(a); out.reason = a.attrs.title;
a.disabled = false; R.apply(a); out.cleared = a.attrs.title ?? null;
const b = el({ why: "Already last.", title: "Move down" }); R.apply(b); out.replaced = b.attrs.title;
b.disabled = false; R.apply(b); out.restored = b.attrs.title;
const c = el({ text: "Saving…" }); R.apply(c); out.busy = c.attrs.title;
const d = el({ title: "Explains itself" }); R.apply(d); out.own = d.attrs.title;
const e = el({ disabled: false }); R.apply(e); out.enabled = e.attrs.title ?? null;
process.stdout.write(JSON.stringify(out));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, {"reason": "Sign in to GitHub first.", "cleared": None, "replaced": "Already last.", "restored": "Move down",
                               "busy": "Working… wait for it to finish.", "own": "Explains itself", "enabled": None})


class RequiredFieldTests(unittest.TestCase):
    def test_required_fields_get_a_red_asterisk(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn('label.field:has(:is(input, select, textarea):is([required], [aria-required="true"])) > span:first-child::after', css)
        self.assertRegex(css, r'content: " \*" / "";\s*color: var\(--bad\);')

    def test_connection_fields_are_marked_by_the_rule_the_server_enforces(self):
        from orchestrator import integrations
        sentry = next(p for p in integrations.status_view({}, {}) if p["id"] == "sentry") if hasattr(integrations, "status_view") else None
        self.assertTrue(integrations.field_required("org", "Organization slug"))
        self.assertTrue(integrations.field_required("token", "Auth token"))
        self.assertFalse(integrations.field_required("project", "Project slug (optional)"))
        self.assertFalse(integrations.field_required("host", "Sentry address"))  # blank means sentry.io
        self.assertIn('${f.required && !p.connected ? \' aria-required="true"\' : ""}', (STATIC / "app.js").read_text(encoding="utf-8"))
        with self.assertRaises(integrations.IntegrationError):
            integrations.connect("sentry", {"token": "t"})  # the organization is required, as its asterisk says

    def test_fields_every_form_needs_are_marked_required(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for needle in ('name="root" placeholder="e.g. /Users/.../my-app or ~/Projects/my-app" required', 'name="name" value="${esc(projectName)}" placeholder="my-project" required',
                       'name="parent" value="${esc(draft.parent)}" required', 'id="ip-path" placeholder="e.g. /Users/.../my-app or ~/Projects/my-app" required',
                       'id="ip-name" value="${esc(name)}" required', 'fieldset class="field np-multi${q.required ? " is-required" : ""}"'):
            self.assertIn(needle, app)
        self.assertIn('value="${esc(values.base_branch || "main")}" required', (STATIC / "setup.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
