"""Button icons: the glyph class `icon` is for the <svg> only, and every icon the page asks for is defined."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))


class IconTests(unittest.TestCase):
    def test_every_icon_the_scripts_use_is_defined_in_the_sprite(self):
        defined = set(re.findall(r'<symbol id="(i-[\w-]+)"', (STATIC / "index.html").read_text()))
        for script in SCRIPTS:
            for name in re.findall(r"#(i-[\w-]+)", script.read_text()):
                self.assertIn(name, defined, f"{script.name} uses {name}, which index.html does not define")

    def test_the_icon_class_is_never_put_on_a_button_or_summary(self):
        """`.icon` is a 21px stroke-only box for glyphs: on a button it squashed the Stop and Export buttons."""
        pattern = re.compile(r"<(?:button|summary|a)\b[^>]*\bclass=\"[^\"]*(?<![\w-])icon(?![\w-])[^\"]*\"")
        for script in [*SCRIPTS, STATIC / "index.html"]:
            for match in pattern.finditer(script.read_text()):
                self.fail(f"{script.name}: the glyph class 'icon' is on a button: {match.group(0)[:120]}")

    def test_icon_only_buttons_are_square_and_the_run_header_uses_them(self):
        css = (STATIC / "style.css").read_text()
        self.assertRegex(css, r"\.btn\.icon-only \{[^}]*width: var\(--control-md\);[^}]*height: var\(--control-md\);[^}]*padding: 0;")
        app = (STATIC / "app.js").read_text()
        start = app.index("function runHeader(")
        header = app[start:start + 2500]
        self.assertIn('<summary class="btn icon-only" aria-label="Export log"', header)
        self.assertIn('class="btn icon-only danger" data-stop', header)
        self.assertIn('#i-download', header)  # export is a download, not the up arrow that means send/share


if __name__ == "__main__":
    unittest.main()
