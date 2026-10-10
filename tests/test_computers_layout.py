import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


class ComputerScreen(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = []
        self.details = 0
        self.card = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "account-machine" in attrs.get("class", "").split():
            self.card = {"text": [], "all_text": [], "visible_actions": [], "hidden_actions": []}
            self.cards.append(self.card)
        if tag == "details" and "open" not in attrs:
            self.details += 1
        if self.card and attrs.get("data-account-action"):
            key = "hidden_actions" if self.details else "visible_actions"
            self.card[key].append(attrs["data-account-action"])

    def handle_endtag(self, tag):
        if tag == "details":
            self.details -= 1
        if tag == "article":
            self.card = None

    def handle_data(self, text):
        if self.card:
            self.card["all_text"].append(text.strip())
        if self.card and not self.details:
            self.card["text"].append(text.strip())


class ComputersLayoutTests(unittest.TestCase):
    def render(self, api_version=2):
        static = Path(__file__).resolve().parents[1] / "orchestrator/web/static"
        script = """
require(process.argv[1]); require(process.argv[2]);
const machines = [
  {id: 'bad', name: 'Laptop', online: true, reachable: true, api_version: Number(process.argv[3]), endpoint: 'https://old.trycloudflare.com'},
  {id: 'good', name: 'Studio', online: true, reachable: true, api_version: 2},
];
process.stdout.write(JSON.stringify(Account.renderMachines(machines, {
  email: 'user@example.com', troubleshoot: {machine: machines[0], error: 'Load failed', endpoint: machines[0].endpoint}
})));
"""
        result = subprocess.run(["node", "-e", script, str(static / "html.js"), str(static / "account.js"), str(api_version)],
                                check=True, capture_output=True, text=True)
        return ComputerScreen(json.loads(result.stdout))

    def test_failed_connection_is_shown_on_only_the_affected_computer(self):
        screen = self.render()
        failed, healthy = screen.cards
        self.assertIn("Web connection unavailable", " ".join(failed["text"]))
        self.assertIn("this browser couldn’t reach its web connection", " ".join(failed["text"]))
        self.assertIn("SSH uses a separate connection", " ".join(failed["all_text"]))
        self.assertNotIn("Make sure Orchestrator is running", " ".join(failed["text"]))
        self.assertNotIn("Online", " ".join(failed["text"]))
        self.assertIn("Retry", " ".join(failed["text"]))
        self.assertIn("Running", " ".join(healthy["text"]))
        self.assertNotIn("Connected", " ".join(healthy["text"]))
        self.assertIn("Open", " ".join(healthy["text"]))

    def test_secondary_actions_are_hidden_until_more_is_opened(self):
        for card in self.render().cards:
            self.assertEqual(card["visible_actions"], ["open"])
            self.assertIn("remove", card["hidden_actions"])
            self.assertEqual(card["hidden_actions"].count("reset-tunnel"), 1)

    def test_update_help_does_not_claim_a_failed_connection_is_online(self):
        failed = self.render(api_version=1).cards[0]
        self.assertNotIn("Online", " ".join(failed["all_text"]))
        self.assertIn("orchestrator update", " ".join(failed["all_text"]))
