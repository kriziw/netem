import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask, render_template

import branding


class BrandingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "assets").mkdir()

    def create_app(self, data=None):
        if data is not None:
            (self.root / "branding.json").write_text(json.dumps(data), encoding="utf-8")
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.config["TESTING"] = True
        for endpoint in ("documentation", "overview", "tests", "analytics", "sessions", "settings"):
            app.add_url_rule("/" + endpoint, endpoint, lambda: "")
        app.add_url_rule("/reports/<session_id>.json", "session_report_json", lambda session_id: "")
        branding.init_app(app, self.root)
        return app

    def test_default_and_invalid_pack_fall_back(self):
        for manifest in (None, {"tokens": {"accent": "red;body{display:none}"}}):
            app = self.create_app(manifest)
            self.assertEqual(app.extensions["branding"]["name"], "NetEm WAN Lab")
            self.assertEqual(app.test_client().get("/branding/theme.css").status_code, 404)

    def test_theme_assets_are_allowlisted_and_fonts_are_local(self):
        (self.root / "assets" / "logo.svg").write_text("<svg/>")
        (self.root / "assets" / "regular.woff2").write_bytes(b"font")
        (self.root / "assets" / "private.txt").write_text("private")
        app = self.create_app({
            "name": "Example Lab", "logo": "logo.svg",
            "tokens": {"accent": "#4050D0"},
            "font_family": "Example Sans",
            "fonts": [{"file": "regular.woff2", "weight": 400}],
        })
        client = app.test_client()
        result = client.get("/branding/theme.css")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "text/css")
        self.assertIn("--accent:#4050D0", result.text)
        self.assertIn("/branding/assets/regular.woff2", result.text)
        self.assertIn("--accent-soft:rgba(64, 80, 208,.12)", result.text)
        self.assertIn("--color-36d6b0-rgb:64, 80, 208", result.text)
        with client.get("/branding/assets/logo.svg") as asset:
            self.assertEqual(asset.status_code, 200)
        for path in ("private.txt", "../branding.json", "../../branding.json"):
            self.assertEqual(client.get("/branding/assets/" + path).status_code, 404)
        self.assertEqual(client.get("/branding/branding.json").status_code, 404)

    def test_manifest_rejects_escape_and_css_injection(self):
        (self.root / "outside.svg").write_text("<svg/>")
        for data in (
            {"logo": "../outside.svg"},
            {"font_family": 'Example";}body{display:none}'},
            {"tokens": {"unknown": "#000000"}},
            {"tokens": []},
            {"fonts": [{"file": "missing.woff2"}]},
            {"name": []},
        ):
            app = self.create_app(data)
            self.assertEqual(app.test_client().get("/branding/theme.css").status_code, 404)

    def test_shared_shell_and_print_report_use_escaped_brand(self):
        (self.root / "assets" / "logo.svg").write_text("<svg/>")
        app = self.create_app({"name": "<Example>", "subtitle": "Validation", "logo": "logo.svg"})
        with app.test_request_context():
            output = render_template("base.html", nav_groups=[], page="overview", app_version="test",
                                     global_runtime={"session": {}, "scenario": {}})
            self.assertIn("&lt;Example&gt;", output)
            self.assertNotIn("<Example>", output)
            self.assertIn("/branding/theme.css", output)
            self.assertIn('class="brand-logo"', output)
            output = render_template(
                "session_report.html", nav_groups=[], global_runtime={"session": {}, "scenario": {}},
                report={"session": {"id": "example", "name": "Validation", "duration_s": 1},
                        "result": "passed", "assertions": [], "tests": [], "event_counts": {},
                        "events": [], "telemetry": {}, "probes": {}, "event_count": 0},
            )
            self.assertIn('class="report-brand"', output)
            self.assertIn("Session Report · &lt;Example&gt;", output)
            self.assertIn("<strong>&lt;Example&gt;</strong>", output)

    def test_showroom_uses_the_pack_without_exposing_operator_routes(self):
        from showroom import create_showroom_app

        (self.root / "assets" / "logo.svg").write_text("<svg/>")
        self.create_app({"name": "Example Lab", "logo": "logo.svg", "tokens": {"bg": "#120D20"}})
        viewer = create_showroom_app(lambda: {"links": []}, self.root)
        client = viewer.test_client()
        html = client.get("/")
        self.assertIn("Showroom · Example Lab", html.text)
        self.assertIn("/branding/assets/logo.svg", html.text)
        self.assertIn("/branding/theme.css", html.text)
        self.assertIn("--bg:#120D20", client.get("/branding/theme.css").text)
        self.assertEqual(client.get("/settings").status_code, 404)
        self.assertEqual(client.post("/branding/theme.css").status_code, 405)
        self.assertEqual(client.get("/branding/branding.json").status_code, 404)
        self.assertIn("style-src 'self'", html.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
