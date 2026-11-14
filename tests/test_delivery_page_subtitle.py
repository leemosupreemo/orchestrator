"""The App delivery page: no sentence under the title for an app project (it used to say "Launch test builds on devices and monitor
pipelines."). A web project keeps its own line."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()


class DeliverySubtitleTests(unittest.TestCase):
    def test_an_app_project_has_no_line_under_the_title(self):
        self.assertNotIn("Launch test builds on devices and monitor pipelines", APP)
        header = APP[APP.index('title: "App delivery",'):][:200]
        self.assertIn('sub: web ? "Publish preview links and track releases." : "",', header)


if __name__ == "__main__":
    unittest.main()
