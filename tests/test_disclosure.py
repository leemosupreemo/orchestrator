"""Every <details> opens and closes smoothly (disclosure.js), except the side panel's."""
from __future__ import annotations

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"


class DisclosureTests(unittest.TestCase):
    def test_it_is_loaded_before_the_app_and_handles_every_details(self):
        html = (STATIC / "index.html").read_text()
        self.assertIn('<script src="disclosure.js"></script>', html)
        self.assertLess(html.index("disclosure.js"), html.index("app.js"))
        js = (STATIC / "disclosure.js").read_text()
        self.assertIn('document.addEventListener("click", onClick)', js)
        self.assertIn('const SKIP = ".sidebar, .setup-panel, [data-no-animate]"', js)  # the side panel keeps its own motion
        self.assertIn("prefers-reduced-motion: reduce", js)

    def test_inline_content_slides_and_floating_content_fades(self):
        js = (STATIC / "disclosure.js").read_text()
        self.assertIn("(floats(details) ? pop : slide)(details, opening)", js)
        self.assertIn('position === "absolute" || position === "fixed"', js)  # a popover or a menu
        self.assertIn("{ height: `${start}px` }, { height: `${end}px` }", js)

    def test_a_link_or_button_inside_a_summary_still_does_its_own_thing(self):
        js = (STATIC / "disclosure.js").read_text()
        self.assertIn("if (control && control !== summary && summary.contains(control)) return;", js)


if __name__ == "__main__":
    unittest.main()
