"""Brand menu and SD-WAN vendor: private MSP brands switch live and are edited in the app;
the vendor under test comes from the simulator or Settings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import app as netem
import branding

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WOFF2 = b"wOF2" + b"\x00" * 32


class BrandStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = self.root / "local"
        (self.local / "assets").mkdir(parents=True)
        (self.local / "assets" / "logo.png").write_bytes(PNG)
        (self.local / "branding.json").write_text(json.dumps({"name": "Installed MSP", "logo": "logo.png",
                                                              "tokens": {"accent": "#A970EF"}}))
        self.store = branding.BrandStore(self.root / "brands", self.local)

    def test_installed_pack_stays_on_until_another_is_chosen(self):
        self.assertEqual(self.store.active_id(), "local")
        self.assertEqual(self.store.current()[1]["name"], "Installed MSP")
        self.assertEqual([(pack["id"], pack["editable"]) for pack in self.store.describe()], [("local", False)])
        self.store.set_active(None)
        root, brand = self.store.current()
        self.assertIsNone(root)
        self.assertEqual(brand["name"], "NetEm WAN Lab")
        with self.assertRaises(ValueError):
            self.store.set_active("missing")

    def test_editor_creates_a_brand_that_switches_live(self):
        pack_id = self.store.save(None, {"name": "BT International", "subtitle": "WAN resilience lab", "mark": "BT",
                                         "font_family": "BT Curve", "token_accent": "#5514b4"},
                                  {"logo": ("bt.png", PNG), "font_regular": ("regular.woff2", WOFF2)})
        self.assertEqual(pack_id, "bt-international")
        manifest = json.loads((self.root / "brands" / pack_id / "branding.json").read_text())
        self.assertEqual((manifest["logo"], manifest["fonts"], manifest["tokens"]["accent"]),
                         ("logo.png", [{"file": "font_regular.woff2", "weight": 400}], "#5514b4"))
        self.store.set_active(pack_id)
        root, brand = self.store.current()
        self.assertEqual((brand["name"], brand["font_family"], brand["id"]), ("BT International", "BT Curve", pack_id))
        # Editing keeps earlier uploads and changes only what was sent.
        self.store.save(pack_id, {"subtitle": "Lab"}, {})
        _root, brand = self.store.current()
        self.assertEqual((brand["subtitle"], brand["logo"]), ("Lab", "logo.png"))
        # The installed pack is read-only: saving it makes an editable copy with its logo.
        copy = self.store.save("local", {"name": "Installed MSP"}, {})
        self.assertEqual(copy, "installed-msp")
        self.assertTrue((self.root / "brands" / copy / "assets" / "logo.png").is_file())
        self.store.delete(pack_id)
        self.assertIsNone(self.store.active_id())
        with self.assertRaises(ValueError):
            self.store.delete("local")

    def test_uploads_must_be_what_they_claim(self):
        cases = [("logo", "x.svg", b"<svg><script>alert(1)</script></svg>"),
                 ("logo", "x.svg", b'<svg onload="alert(1)"/>'),
                 ("logo", "x.svg", b'<svg><image xlink:href="http://example.com/a.png"/></svg>'),
                 ("logo", "x.png", b"GIF89a not a png"),
                 ("logo", "x.exe", PNG),
                 ("favicon", "x.webp", b"RIFF0000WEBPVP8 "),
                 ("font_bold", "x.woff2", b"wOFF" + b"\x00" * 8),
                 ("logo", "x.png", PNG + b"\x00" * branding.UPLOAD_LIMIT)]
        for slot, filename, data in cases:
            with self.subTest(slot=slot, filename=filename):
                with self.assertRaises(ValueError):
                    branding.check_upload(slot, filename, data)
        self.assertEqual(branding.check_upload("logo", "plain.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="#a"/></svg>'), ".svg")
        self.assertEqual(branding.check_upload("logo", "x.webp", b"RIFF0000WEBPVP8 "), ".webp")
        with self.assertRaises(ValueError):
            self.store.save(None, {"name": "Broken", "token_accent": "red;}"}, {})
        self.assertFalse((self.root / "brands" / "broken").exists())

    def test_assets_cannot_run_when_opened_directly(self):
        app = Flask(__name__)
        branding.init_app(app, self.store)
        with app.test_client().get("/branding/assets/logo.png") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])


class BrandMenuRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.store = branding.BrandStore(Path(temporary.name) / "brands")
        mock = patch.object(netem, "BRAND_STORE", self.store)
        mock.start()
        self.addCleanup(mock.stop)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def test_menu_creates_switches_and_deletes_brands(self):
        page = self.client.get("/settings/branding").text
        self.assertIn("Default look", page)
        self.assertIn("New brand", page)
        self.assertEqual(self.client.post("/settings/branding/save", data={"name": "MSP"}).status_code, 400)
        response = self.client.post("/settings/branding/save", content_type="multipart/form-data",
                                    data={"integration_csrf": "token", "name": "Example MSP", "activate": "1",
                                          "logo": (__import__("io").BytesIO(PNG), "logo.png")})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.active_id(), "example-msp")
        with self.client.get("/settings/branding/preview/example-msp/logo.png") as preview:
            self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.client.get("/settings/branding/preview/example-msp/branding.json").status_code, 404)
        self.client.post("/settings/branding/activate", data={"integration_csrf": "token", "pack": ""})
        self.assertIsNone(self.store.active_id())
        self.client.post("/settings/branding/delete", data={"integration_csrf": "token", "pack": "example-msp"})
        self.assertEqual(self.store.packs(), {})


class ApplianceTests(unittest.TestCase):
    def setUp(self):
        mock = patch.object(netem, "APPLIANCE_CACHE", {"checked": None, "payload": None})
        mock.start()
        self.addCleanup(mock.stop)

    def test_simulator_vendor_labels_map_to_built_in_marks(self):
        for label, expected in (("Fortinet", "fortinet"), ("VeloCloud", "velocloud"), ("HPE Aruba EdgeConnect", "hpe-aruba"),
                                ("Palo Alto Networks", "palo-alto"), ("Check Point", "check-point"), ("Cato Networks", "cato"),
                                ("Other", None), ("", None)):
            self.assertEqual(netem.vendor_id(label), expected)
        for vendor in netem.SDWAN_VENDORS:
            self.assertTrue((Path(netem.BASE_DIR) / "static" / "vendors" / f"{vendor}.png").is_file(), vendor)

    def test_vendor_comes_from_the_simulator_unless_set(self):
        route = {"interface": "eth1", "gateway": "10.250.10.1", "target": "198.18.0.1"}
        network = {"selected": route, "appliances": [dict(route, id="a1", name="Lab FortiGate", vendor="Fortinet", model="FortiGate-VM04")]}
        with patch.object(netem, "load_config", return_value={}), \
             patch.object(netem, "traffic_generator_config", return_value={"host": "192.168.0.135"}), \
             patch.object(netem, "traffic_generator_api_key", return_value="key"), \
             patch.object(netem, "traffic_generator_request", return_value=network):
            appliance = netem.appliance_under_test()
        self.assertEqual({key: appliance[key] for key in ("vendor", "vendor_name", "model", "name", "source")},
                         {"vendor": "fortinet", "vendor_name": "Fortinet", "model": "FortiGate-VM04", "name": "Lab FortiGate",
                          "source": "simulator"})
        with patch.object(netem, "load_config", return_value={"appliance": {"vendor": "versa", "model": "FlexVNF"}}):
            appliance = netem.appliance_under_test()
        self.assertEqual((appliance["vendor"], appliance["product"], appliance["source"]), ("versa", "Versa SASE", "setting"))

    def test_settings_names_the_appliance(self):
        cfg = {}
        client = netem.app.test_client()
        with client.session_transaction() as state:
            state["integration_csrf"] = "token"
        with patch.object(netem, "load_config", side_effect=lambda: dict(cfg)), \
             patch.object(netem, "save_config", side_effect=cfg.update):
            client.post("/settings/appliance", data={"integration_csrf": "token", "vendor": "palo-alto", "model": "ION 3000"})
            self.assertEqual(cfg["appliance"], {"vendor": "palo-alto", "model": "ION 3000"})
            client.post("/settings/appliance", data={"integration_csrf": "token", "vendor": "unknown"})
            self.assertEqual(cfg["appliance"]["vendor"], "palo-alto")


if __name__ == "__main__":
    unittest.main()
