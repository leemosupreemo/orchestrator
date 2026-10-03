"""The message queue behind the in-app overlay (orchestrator/web/static/messages.js), run under node with a fake clock."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "messages.js"


@unittest.skipUnless(shutil.which("node"), "node is needed to test the front-end modules")
class MessageQueueTests(unittest.TestCase):
    def run_js(self, body: str):
        script = f"""
const M = require(process.argv[1]);
let t = 1000;
const store = M.createStore({{ now: () => t }});
const ids = (s = store) => s.list().map((m) => m.id);
const texts = () => store.list().map((m) => m.text);
const tick = (ms) => {{ t += ms; return store.expire().map((m) => m.text); }};
{body}
"""
        res = subprocess.run(["node", "-e", script, str(MODULE)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_each_kind_goes_away_by_itself_after_its_own_time_and_errors_last_longest(self):
        out = self.run_js("""
const a = store.push({ kind: "success", text: "A" });
const early = tick(3999); const late = tick(1);
store.push({ kind: "info", text: "B" }); const b1 = tick(5999); const b2 = tick(1);
store.push({ kind: "warning", text: "C" }); const c1 = tick(9999); const c2 = tick(1);
store.push({ kind: "error", text: "D" }); const d1 = tick(9999); const d2 = tick(1);
console.log(JSON.stringify({ early, late, b1, b2, c1, c2, d1, d2 }));
""")
        self.assertEqual(out, {"early": [], "late": ["A"], "b1": [], "b2": ["B"], "c1": [], "c2": ["C"], "d1": [], "d2": ["D"]})

    def test_a_sticky_message_stays_until_it_is_dismissed(self):
        out = self.run_js("""
store.push({ id: "connection", kind: "warning", text: "Can't reach Orchestrator.", sticky: true });
const after = tick(10 * 60 * 1000);
const removed = store.dismiss("connection");
console.log(JSON.stringify({ after, still: after.length === 0, removed: removed && removed.id, left: ids() }));
""")
        self.assertEqual((out["still"], out["removed"], out["left"]), (True, "connection", []))

    def test_a_message_with_actions_stays_long_enough_to_use_them(self):
        out = self.run_js("""
store.push({ kind: "success", text: "Deleted", actions: [{ label: "Undo", run() {} }] });
console.log(JSON.stringify({ at5s: tick(5000), at9s: tick(4999), at10s: tick(1) }));
""")
        self.assertEqual(out, {"at5s": [], "at9s": [], "at10s": ["Deleted"]})

    def test_the_same_id_updates_in_place_and_restarts_its_clock(self):
        out = self.run_js("""
store.push({ id: "up", kind: "info", text: "Uploading 1 of 3" }); tick(3000);
store.push({ id: "up", kind: "success", text: "Uploaded" });
const first = ids(); const after5 = tick(3999); const after6 = tick(1);
console.log(JSON.stringify({ first, kinds: [], after5, after6, count: first.length }));
""")
        self.assertEqual((out["first"], out["count"]), (["up"], 1))
        self.assertEqual(out["after5"], [])  # the clock restarted when it was updated (success lasts 4 s)
        self.assertEqual(out["after6"], ["Uploaded"])

    def test_a_repeated_message_refreshes_the_one_on_screen_instead_of_stacking(self):
        out = self.run_js("""
store.push({ kind: "error", text: "Can't reach Orchestrator." }); tick(8000);
store.push({ kind: "error", text: "Can't reach Orchestrator." });
console.log(JSON.stringify({ n: store.list().length, gone: tick(3000), left: texts() }));
""")
        self.assertEqual((out["n"], out["gone"], out["left"]), (1, [], ["Can't reach Orchestrator."]))

    def test_at_most_three_are_shown_and_the_oldest_makes_room_but_a_sticky_one_never_does(self):
        out = self.run_js("""
store.push({ id: "conn", kind: "warning", text: "Offline", sticky: true });
for (const n of ["one", "two", "three", "four"]) store.push({ kind: "success", text: n });
console.log(JSON.stringify({ texts: texts() }));
""")
        self.assertEqual(out["texts"], ["Offline", "three", "four"])

    def test_hovering_holds_a_message_open_and_leaving_gives_it_a_moment(self):
        out = self.run_js("""
const id = store.push({ kind: "success", text: "Saved" });
tick(3000); store.pause(id);
const held = tick(60000);
store.resume(id);
const soon = tick(1499); const later = tick(1);
console.log(JSON.stringify({ held, soon, later }));
""")
        self.assertEqual(out, {"held": [], "soon": [], "later": ["Saved"]})  # about a second left, but never less than 1.5 s to read the rest

    def test_empty_or_unknown_input_is_handled(self):
        out = self.run_js("""
const none = store.push({ kind: "success", text: "   " });
const odd = store.push({ kind: "nonsense", text: "Hello" });
console.log(JSON.stringify({ none, kind: store.list()[0].kind, n: store.list().length }));
""")
        self.assertEqual((out["none"], out["kind"], out["n"]), (None, "info", 1))

    def test_dismissing_hands_back_the_message_so_the_caller_can_react(self):
        out = self.run_js("""
let closed = 0;
store.push({ id: "p", kind: "info", text: "Updated", sticky: true, onDismiss: () => { closed++; } });
const m = store.dismiss("p"); m.onDismiss();
console.log(JSON.stringify({ closed, none: store.dismiss("p") }));
""")
        self.assertEqual(out, {"closed": 1, "none": None})


if __name__ == "__main__":
    unittest.main()
