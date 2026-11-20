"""Home's collapsed Product card: an icon link in the header instead of an "Open product" button, and the pitch shown in
full behind a "Show more" that slides the card open, rather than cut off at 180 characters with an ellipsis."""
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
APP = (STATIC / "app.js").read_text()
CSS = (STATIC / "style.css").read_text()
SPRITE = (STATIC / "index.html").read_text()
STRIP = APP[APP.index("function productStripHtml"):APP.index("pages.product =")]


class ProductCardTests(unittest.TestCase):
    def test_the_open_button_is_an_icon_link_at_the_header_s_right_edge(self):
        self.assertNotIn(">Open product</a>", STRIP)
        header = STRIP[STRIP.index('<details class="fold">'):STRIP.index("</summary>")]
        self.assertIn('class="btn small ghost icon-only product-open" href="#/product" aria-label="Open product" title="Open product"', header)
        self.assertIn('<use href="#i-external"/>', header)
        self.assertIn('id="i-external"', SPRITE)
        self.assertIn(".fold > summary.card-h .product-open { margin-left: auto; }", CSS)

    def test_the_pitch_is_whole_and_folds_behind_show_more_in_both_states(self):
        self.assertEqual(STRIP.count('data-expandable="${PRD_PITCH_LINES_PX}" data-expandable-slack="0"'), 2)
        self.assertEqual(STRIP.count("prdSnippet(lead.body, Infinity)"), 2)
        self.assertNotIn("prdSnippet(lead.body))", STRIP)  # no more cut at 180 characters

    def test_short_blocks_can_skip_the_expandable_tolerance_and_a_closed_fold_wires_when_opened(self):
        self.assertIn("box.dataset.expandableSlack === undefined ? 120", APP)
        self.assertIn('e.target.matches?.("details.fold") && e.target.open) wireExpandables(e.target)', APP)


if __name__ == "__main__":
    unittest.main()
