"""Home's Product card in every state, and the "last updated" line that says when and by whom (including the AI).

The card's real functions are read out of app.js and run in Node, so what is tested is what ships."""
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from orchestrator import prd
from tests.test_web_ui import ServerTestCase

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


SECTION_IDS = [s["id"] for s in prd.SECTIONS]
SECTION_BODIES = {"pitch": "A word game for two friends. Players take turns building words.", "who": "Two friends with a spare minute.",
                  "features": "- Turn-based play\n- A scoreboard", "look": "Calm, paper-like.", "not": "No ads.", "success": "Friends play twice a week."}


def document(count: int) -> str:
    text = prd.template()
    for section in SECTION_IDS[:count]:
        text = prd.replace_section(text, section, SECTION_BODIES[section])
    return text


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class HomePageStatesTests(unittest.TestCase):
    """Home assembled for real (its own page function, the card's real code) with the server's answer for each state:
    the card is on Home when nothing is written, when it's partly written, and when it's complete."""

    STATIC = APP.parent

    def home(self, product: dict | None, jobs: list | None = None) -> str:
        script = r"""
const fs=require('fs'),vm=require('vm'),dir=process.argv[1];
require(dir+'/html.js');require(dir+'/home-status.js');
const source=fs.readFileSync(dir+'/app.js','utf8');
const product=JSON.parse(process.argv[2]), jobs=JSON.parse(process.argv[3]);
const context={HomeStatus,URLSearchParams,esc:Html.escape,projectDownloads:{pending:()=>null},setupState:{complete:true},loadSetup:async()=>{},getBackendUrl:()=>'',
 state:{project:{root:'/sample',name:'Snake',branch:'main',branches:[],machine_count:1,model_count:1},runs:[],you:{role:'owner'}},pages:{},
 api:async path=>{if(path==='jobs')return {jobs};if(path==='inbox')return {elsewhere:[]};if(path==='product'){if(product===null)throw Error('older backend');return product;}if(path==='jobs/archived')return {archived:[]};throw Error('older backend');},
 sortJobs:x=>x,homeProjectTitle:p=>p.name,homeHealthIndicator:()=>'',previewCard:()=>'',elsewhereLine:()=>'',HOME_JOBS_SHOWN:10,
 sortArrow:()=>'',jobTableRow:()=>'<tr></tr>',jobPill:()=>'',jobIsRunning:()=>false,kindPill:()=>'',ago:()=>'',plural:(n,w)=>n+' '+w,pill:()=>'',runningMark:''};
vm.createContext(context);
const take=(from,to)=>{const a=source.indexOf(from);return source.slice(a,source.indexOf(to,a));};
vm.runInContext(take('const prdStatusPill','const pill = '),context);
vm.runInContext(take('function prdSnippet','// The AI updated the product requirements by itself'),context);
vm.runInContext(take('const PRD_ACTOR','pages.product = async'),context);
vm.runInContext(take('function statusLine(','\n}\n')+'\n}\n',context);
vm.runInContext(take('pages.home = async','function jobHeaderActions('),context);
(async()=>{const page=await context.pages.home([],new URLSearchParams());console.log(JSON.stringify({html:page.html}));})().catch(e=>{console.error(e);process.exit(1)});
"""
        done = subprocess.run(["node", "-e", script, str(self.STATIC), json.dumps(product), json.dumps(jobs or [])], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["html"]

    def sections(self, count: int) -> list[dict]:
        return prd.sections_view(document(count))

    def card(self, html: str) -> str:
        start = html.index('id="product-strip"')
        return html[start:html.index("</section>", start)]

    def test_state_1_nothing_written_the_card_is_on_home_with_the_prompt(self):
        html = self.home({"sections": self.sections(0), "can_draft": True, "updated": None})
        card = self.card(html)
        self.assertIn("Tell us what you're building", card)
        self.assertIn("Write it", card)
        self.assertNotIn("Last updated", card)
        self.assertNotIn("sections filled in", card)

    def test_state_2_partly_written_the_card_stays_on_home_with_progress_and_a_way_to_finish(self):
        for count in (1, 3, 5):
            with self.subTest(filled=count):
                card = self.card(self.home({"sections": self.sections(count), "updated": {"at": time.time(), "source": "you", "job": ""}}))
                self.assertIn("Partially completed", card)
                self.assertIn(f"{count} of 6 sections filled in", card)
                self.assertIn("Finish it", card)
                self.assertIn("Last updated", card)
                self.assertNotIn("Tell us what you're building", card)

    def test_state_3_complete_the_card_stays_on_home_with_the_pitch_and_no_missing_list(self):
        card = self.card(self.home({"sections": self.sections(6), "updated": {"at": time.time(), "source": "you", "job": ""}}))
        self.assertIn("Completed", card)
        self.assertIn("A word game for two friends", card)
        self.assertNotIn("Still to fill in", card)
        self.assertNotIn("Finish it", card)
        self.assertIn("Last updated", card)

    def test_the_card_names_the_job_behind_an_automatic_update_from_the_jobs_home_already_has(self):
        updated = {"at": time.time(), "source": "auto", "job": "job-5"}
        card = self.card(self.home({"sections": self.sections(6), "updated": updated}, jobs=[{"id": "job-5", "title": "Add a scoreboard", "status": "done", "state": {"label": "Done", "tone": "done"}}]))
        self.assertIn("by AI after “Add a scoreboard”", card)

    def test_an_older_server_without_the_product_endpoint_just_has_no_card(self):
        self.assertNotIn('id="product-strip"', self.home(None))

    def test_a_server_that_does_not_send_the_update_record_shows_the_card_without_a_date(self):
        card = self.card(self.home({"sections": self.sections(3)}))
        self.assertIn("Partially completed", card)
        self.assertNotIn("Last updated", card)

    def test_the_card_sits_right_under_the_project_status_in_every_state(self):
        for count in (0, 3, 6):
            with self.subTest(filled=count):
                html = self.home({"sections": self.sections(count), "updated": None})
                self.assertLess(html.index('id="home-project-status"'), html.index('id="product-strip"'))


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class StatusPillThresholdTests(unittest.TestCase):
    def pill(self, filled: int, total: int = 6) -> str:
        script = r"""
const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
globalThis.esc = (s) => String(s);
vm.runInThisContext(code.slice(code.indexOf('const prdStatusPill'), code.indexOf('const pill = ')));
const [filled, total] = JSON.parse(process.argv[2]);
console.log(JSON.stringify(prdStatusPill(Array.from({length: total}, (_, i) => ({filled: i < filled})))));
"""
        done = subprocess.run(["node", "-e", script, str(APP), json.dumps([filled, total])], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_none_some_and_all_sections_give_three_different_statuses(self):
        none, some, every = self.pill(0), self.pill(1), self.pill(6)
        self.assertIn("Needs completion", none)
        self.assertIn('class="pill failed"', none)
        self.assertIn("Partially completed", some)
        self.assertIn('class="pill attention"', some)
        self.assertIn("Completed", every)
        self.assertIn('class="pill done"', every)
        self.assertIn("1 of 6 sections filled in", some)

    def test_the_edges_between_the_states(self):
        self.assertIn("Partially completed", self.pill(5))
        self.assertIn("Completed", self.pill(6))
        self.assertIn("Needs completion", self.pill(0, 0))  # no sections at all counts as nothing written


class ProductEndpointStatesTests(ServerTestCase):
    """The server's answer for each state, from a real document on disk: what Home is given."""

    def doc(self):
        return prd.Prd(self.root, self.root / ".orchestrator")

    def product(self) -> dict:
        res, data = self.request("GET", "/api/product")
        self.assertEqual(res.status, 200, data)
        return data

    def filled(self, data) -> int:
        return sum(1 for s in data["sections"] if s["filled"])

    def test_state_1_no_document_nothing_filled_and_no_update(self):
        data = self.product()
        self.assertEqual((self.filled(data), len(data["sections"]), data["updated"]), (0, 6, None))

    def test_state_1b_a_template_only_document_is_still_nothing_filled(self):
        self.doc().write(prd.template(), "draft", "Drafted")
        self.assertEqual(self.filled(self.product()), 0)

    def test_state_2_partly_written_counts_what_is_filled_and_says_who_wrote_it(self):
        self.doc().write(document(3), "you", "Wrote three sections")
        data = self.product()
        self.assertEqual(self.filled(data), 3)
        self.assertEqual([s["id"] for s in data["sections"] if not s["filled"]], ["look", "not", "success"])
        self.assertEqual((data["updated"]["source"], data["updated"]["summary"], data["updated"]["job"]), ("you", "Wrote three sections", ""))
        self.assertLess(abs(data["updated"]["at"] - time.time()), 30)

    def test_state_3_complete_has_every_section_filled(self):
        self.doc().write(document(6), "you", "Finished it")
        data = self.product()
        self.assertEqual(self.filled(data), 6)

    def test_an_automatic_update_after_a_job_is_reported_as_the_ais_with_the_job(self):
        self.doc().write(document(6), "you", "Finished it")
        self.doc().write(document(6) + "\n\nAlso: a scoreboard.", "auto", "Noted the scoreboard", job="20260922-bug-1")
        updated = self.product()["updated"]
        self.assertEqual((updated["source"], updated["job"]), ("auto", "20260922-bug-1"))

    def test_the_page_can_name_that_job_because_the_jobs_list_carries_its_title(self):
        res, data = self.request("GET", "/api/jobs")
        self.assertEqual(res.status, 200, data)
        self.assertEqual({j["id"]: j["title"] for j in data["jobs"]}["20260922-bug-1"], "Lobby seat stays empty")

    def test_a_document_edited_outside_the_app_is_reported_as_such(self):
        import os
        self.doc().write(document(6), "you", "Finished it")
        later = time.time() + 3600
        os.utime(self.doc().path, (later, later))
        updated = self.product()["updated"]
        self.assertEqual((updated["source"], updated["job"]), ("outside", ""))
        self.assertAlmostEqual(updated["at"], later, delta=2)


if __name__ == "__main__":
    unittest.main()
