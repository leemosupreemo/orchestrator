"""App delivery: everything needed before a build can be sent (where builds go, signing, a server the builds start) sits in one
"Before you can send builds" container. What is still to do is on top; what is already set is tucked under "Already set up", and
once nothing is left the container is a single collapsed line below the send card."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
CSS = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "style.css").read_text()
PAGE = APP[APP.index("pages.delivery = async"):APP.index("pages.delivery = async") + 40000]


class ContainerMarkupTests(unittest.TestCase):
    def container(self) -> str:
        start = PAGE.index('const prereqHtml = web ? ""')
        return PAGE[start:PAGE.index("</details>`;", start)]

    def test_it_holds_where_builds_go_signing_and_the_server_and_a_place_for_what_is_done(self):
        html = self.container()
        for bit in ('id="prereq-target"', 'id="prereq-signing"', 'id="signing-card"', 'id="prereq-server"', 'id="prereq-todo"',
                    'id="prereq-done"', 'id="prereq-done-list"', "Already set up (", "Before you can send builds"):
            self.assertIn(bit, html)

    def test_a_web_project_has_no_container_and_a_ready_target_keeps_it_out_of_sight_until_the_checks_answer(self):
        html = self.container()
        self.assertIn('const prereqHtml = web ? ""', PAGE)
        self.assertIn('${targetReady ? " hidden" : " open"}', html)

    def test_the_page_order_is_prerequisites_then_the_send_card_then_an_ordinary_server_card(self):
        html = PAGE[PAGE.index('html: `${prereqHtml}'):][:400]
        self.assertLess(html.index("${prereqHtml}"), html.index("${branchCard}"))
        self.assertLess(html.index("${branchCard}"), html.index('id="server-card"'))

    def test_the_setup_that_used_to_sit_in_the_send_card_moved_into_the_container(self):
        send = PAGE[PAGE.index("const branchCard ="):PAGE.index("const releaseCard") if "const releaseCard" in PAGE[PAGE.index("const branchCard ="):] else None]
        self.assertNotIn("Not ready to send test builds", PAGE)
        self.assertIn("Finish the steps under <strong>Before you can send builds</strong> first.", PAGE)
        self.assertIn("deliveryProposalHtml(d.proposal, canEditTarget)", self.container() + PAGE[PAGE.index("const targetBody"):PAGE.index("const prereqHtml")])

    def test_the_send_card_can_be_found_so_the_container_can_move_around_it(self):
        self.assertIn('const branchCard = `<section class="card mb-16" id="delivery-send">', PAGE)


class StateTests(unittest.TestCase):
    def test_signing_reports_ready_todo_or_not_applicable(self):
        fn = APP[APP.index("async function loadSigningCard("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("onState = () => {}", fn)
        self.assertIn('item.state !== "no-project" && item.state !== "not-needed"', fn)
        self.assertIn('["ready", "env", "unknown"].includes(item.state)', fn)  # "can't tell" isn't nagged about
        self.assertIn("onState(null)", fn)  # a failed load means it doesn't apply, not that it is missing

    def test_a_server_is_a_prerequisite_only_when_builds_start_it_first(self):
        fn = APP[APP.index("async function loadServerCard("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("required: (view.app || []).some((a) => a.id === picked.id && a.machine === view.machine)", fn)
        self.assertIn('picked.status?.state === "running"', fn)
        self.assertIn("onState(null)", fn)
        wiring = PAGE[PAGE.index("loadServerCard(view.querySelector"):][:600]
        self.assertIn("if (s?.required && items.server)", wiring)
        self.assertIn('report("server", null)', wiring)

    def test_the_container_sorts_items_and_places_itself_by_what_is_left_to_do(self):
        wiring = PAGE[PAGE.index("const refreshPrereqs = () =>"):PAGE.index("const report = ")]
        for bit in ('it.ready === false', 'it.ready === true', 'it.ready === null', '`${todo.length} to do` : "All set"',
                    'const where = todo.length ? "top" : "below"', "send.before(box)", "send.after(box)", "box.open = where === \"top\"",
                    'const waiting = ["target", "signing"].some'):
            self.assertIn(bit, wiring)

    def test_the_slow_server_check_is_not_waited_for(self):
        wiring = PAGE[PAGE.index("const refreshPrereqs = () =>"):PAGE.index("const report = ")]
        self.assertNotIn('"server"].some', wiring)


class StyleTests(unittest.TestCase):
    def test_cards_inside_the_container_do_not_add_their_own_spacing(self):
        self.assertIn(".prereqs .card { margin-bottom: 0; box-shadow: none; }", CSS)


if __name__ == "__main__":
    unittest.main()
