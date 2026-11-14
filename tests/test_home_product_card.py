"""Home's Product card in every state, and the "last updated" line that says when and by whom (including the AI).

The card's real functions are read out of app.js and run in Node, so what is tested is what ships."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js"
TITLES = ["Pitch", "Who it's for", "Core features", "Look and feel", "What to exclude", "Success measures"]


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class HomeProductCardTests(unittest.TestCase):
    def run_js(self, body: str):
        script = r'''
const fs = require('fs');
const code = fs.readFileSync(process.argv[1], 'utf8');
const vm = require('vm');
const take = (from, to) => code.slice(code.indexOf(from), code.indexOf(to));
globalThis.esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
vm.runInThisContext(take('const prdStatusPill', 'const pill = '));
vm.runInThisContext(take('function prdSnippet', '// The AI updated the product requirements by itself'));
vm.runInThisContext(take('const PRD_ACTOR', 'pages.product = async'));
const sections = (filledCount, bodies = {}) => JSON.parse(process.argv[2]).map((title, i) => ({ id: ['pitch','who','features','look','not','success'][i], title, filled: i < filledCount, body: i < filledCount ? (bodies[i] || `Body of ${title}`) : '' }));
''' + body
        done = subprocess.run(["node", "-e", script, str(APP), json.dumps(TITLES)], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_nothing_written_shows_the_prompt_and_the_ways_to_start_and_no_date(self):
        out = self.run_js("console.log(JSON.stringify({html: productStripHtml({sections: sections(0), can_draft: true, updated: null}, [])}));")
        html = out["html"]
        self.assertIn("Tell us what you're building", html)
        for button in ("Draft with AI", "Write it", "Import PRD", "Reverse engineer (REA)"):
            self.assertIn(button, html)
        self.assertNotIn("Last updated", html)
        self.assertNotIn("class=\"pill", html)

    def test_the_ai_draft_button_is_only_offered_when_an_ai_is_available(self):
        out = self.run_js("console.log(JSON.stringify({html: productStripHtml({sections: sections(0), can_draft: false}, [])}));")
        self.assertNotIn("Draft with AI", out["html"])
        self.assertIn("Write it", out["html"])

    def test_partly_written_shows_the_status_what_is_missing_and_a_way_to_finish(self):
        out = self.run_js('''console.log(JSON.stringify({html: productStripHtml({sections: sections(3, {0: "A word game for two friends."}), updated: {at: 1794600000, source: "you", job: ""}}, [])}));''')
        html = out["html"]
        self.assertIn("Partially completed", html)
        self.assertIn('class="pill attention"', html)
        self.assertIn("3 of 6 sections filled in. Still to fill in: Look and feel, What to exclude, Success measures.", html)
        self.assertIn('class="product-pitch" href="#/product"', html)
        self.assertIn("A word game for two friends.", html)
        self.assertIn(">Finish it</a>", html)
        self.assertIn("Last updated", html)

    def test_a_long_missing_list_is_shortened(self):
        out = self.run_js('console.log(JSON.stringify({html: productStripHtml({sections: sections(1)}, [])}));')
        self.assertIn("1 of 6 sections filled in. Still to fill in: Who it's for, Core features, Look and feel and 2 more.", out["html"])

    def test_complete_shows_the_pitch_and_no_finish_button_and_no_separate_open_link(self):
        out = self.run_js('console.log(JSON.stringify({html: productStripHtml({sections: sections(6), updated: {at: 1794600000, source: "you", job: ""}}, [])}));')
        html = out["html"]
        self.assertIn("Completed", html)
        self.assertIn('class="pill done"', html)
        self.assertNotIn("Finish it", html)
        self.assertNotIn("Still to fill in", html)
        self.assertNotIn(">Open</a>", html)  # the card and its pitch open the document themselves
        self.assertIn('<h2><a class="card-title-link" href="#/product">Product</a></h2>', html)

    def test_when_the_pitch_is_empty_the_first_written_section_leads(self):
        out = self.run_js('''const s = sections(0); s[2].filled = true; s[2].body = "Scoring and a leaderboard."; console.log(JSON.stringify({html: productStripHtml({sections: s}, [])}));''')
        self.assertIn("Scoring and a leaderboard.", out["html"])

    def test_an_automatic_update_says_the_ai_made_it_and_names_the_job(self):
        out = self.run_js('''console.log(JSON.stringify({html: productStripHtml({sections: sections(6), updated: {at: 1794600000, source: "auto", job: "job-7"}}, [{id: "job-7", title: "Fix login"}, {id: "job-8", title: "Other"}])}));''')
        self.assertIn("· by AI after “Fix login”", out["html"])

    def test_an_automatic_update_for_a_job_that_is_gone_still_says_ai(self):
        out = self.run_js('console.log(JSON.stringify({html: productStripHtml({sections: sections(6), updated: {at: 1794600000, source: "auto", job: "job-9"}}, [])}));')
        self.assertIn("· by AI after a job", out["html"])

    def test_each_kind_of_change_is_worded_for_a_person(self):
        out = self.run_js('''const words = {};
for (const source of ["you", "auto", "ai", "draft", "reverse", "import", "revert", "answers", "outside", "migration", "earlier", "somethingnew"]) {
  words[source] = prdUpdatedLine({at: 1794600000, source, job: ""}, []).replace(/<[^>]+>/g, "");
}
console.log(JSON.stringify(words));''')
        tail = lambda key: out[key].split("Last updated ", 1)[1].split(" · ", 1)[1] if " · " in out[key] else ""
        self.assertEqual(tail("you"), "by you")
        self.assertEqual(tail("auto"), "by AI after a job")
        self.assertEqual(tail("ai"), "with AI help")
        self.assertEqual(tail("draft"), "drafted by AI")
        self.assertEqual(tail("reverse"), "by AI, from the finished app")
        self.assertEqual(tail("import"), "imported")
        self.assertEqual(tail("outside"), "edited outside Orchestrator")
        self.assertEqual(tail("migration"), "")  # nothing to say about how an old document was carried over
        self.assertEqual(tail("somethingnew"), "")  # a source this page doesn't know is just a date, never "undefined"
        self.assertNotIn("undefined", json.dumps(out))

    def test_no_update_record_means_no_last_updated_line(self):
        out = self.run_js("console.log(JSON.stringify({line: prdUpdatedLine(null, []), none: prdUpdatedLine({}, [])}));")
        self.assertEqual((out["line"], out["none"]), ("", ""))

    def test_a_job_title_with_markup_is_shown_as_text(self):
        out = self.run_js('''console.log(JSON.stringify({line: prdUpdatedLine({at: 1794600000, source: "auto", job: "j"}, [{id: "j", title: "<img src=x onerror=alert(1)>"}])}));''')
        self.assertNotIn("<img", out["line"])
        self.assertIn("&lt;img", out["line"])

    def test_dates_read_as_today_yesterday_or_the_date(self):
        out = self.run_js('''const now = new Date(2026, 10, 13, 12, 0, 0);
const at = (y, m, d, h = 19, min = 2) => new Date(y, m, d, h, min).getTime() / 1000;
console.log(JSON.stringify({today: prdWhen(at(2026, 10, 13), now), yesterday: prdWhen(at(2026, 10, 12), now), sameYear: prdWhen(at(2026, 9, 3), now), otherYear: prdWhen(at(2025, 10, 12), now)}));''')
        self.assertTrue(out["today"].startswith("today, ") and "2" in out["today"], out["today"])
        self.assertTrue(out["yesterday"].startswith("yesterday, "), out["yesterday"])
        self.assertRegex(out["sameYear"], r"^Oct\s3$")
        self.assertRegex(out["otherYear"], r"^Nov\s12,\s2025$")

    def test_the_full_date_and_time_is_in_the_tooltip(self):
        out = self.run_js('console.log(JSON.stringify({line: prdUpdatedLine({at: 1794600000, source: "you", job: ""}, [])}));')
        self.assertRegex(out["line"], r'title="[^"]*\d{4}[^"]*"')

    def test_home_draws_the_card_whenever_the_server_sent_the_product_not_only_when_it_is_empty(self):
        code = APP.read_text()
        home = code[code.index("pages.home = async"):code.index("function jobHeaderActions")]
        self.assertIn('${product ? productStripHtml(product, jobs) : ""}', home)
        self.assertNotIn("sections.some((x) => x.filled) ? productStripHtml", home)


if __name__ == "__main__":
    unittest.main()
