"""Clock times follow one setting: 24-hour by default, or 12-hour. Every page, record and the showroom use it."""
import unittest
from datetime import datetime
from unittest.mock import patch

import app as netem


class TimeFormatSettingTests(unittest.TestCase):
    def test_default_is_24_hour_and_unknown_values_fall_back_to_it(self):
        with patch.object(netem, "load_config", return_value={}):
            self.assertEqual(netem.time_format(), "24h")
        self.assertEqual(netem.time_format({"time_format": "12h"}), "12h")
        self.assertEqual(netem.time_format({"time_format": "24h"}), "24h")
        for value in ("", "AM", None, 12):
            with self.subTest(value=value):
                self.assertEqual(netem.time_format({"time_format": value}), "24h")

    def test_clock_labels_follow_the_format(self):
        afternoon = datetime(2026, 10, 13, 14, 5, 9).timestamp()
        morning = datetime(2026, 10, 13, 9, 30).timestamp()
        midnight = datetime(2026, 10, 13, 0, 5).timestamp()
        noon = datetime(2026, 10, 13, 12, 0).timestamp()
        self.assertEqual(netem.clock_label(afternoon, "24h"), "14:05")
        self.assertEqual(netem.clock_label(afternoon, "12h"), "2:05 PM")
        self.assertEqual(netem.clock_label(morning, "12h"), "9:30 AM")
        self.assertEqual(netem.clock_label(midnight, "24h"), "00:05")
        self.assertEqual(netem.clock_label(midnight, "12h"), "12:05 AM")
        self.assertEqual(netem.clock_label(noon, "12h"), "12:00 PM")


class TimeFormatRouteTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {}
        for mock in (patch.object(netem, "load_config", side_effect=lambda: dict(self.cfg)),
                     patch.object(netem, "save_config", side_effect=self.cfg.update),
                     patch.object(netem, "log_event")):
            mock.start()
            self.addCleanup(mock.stop)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def test_saving_requires_the_form_token(self):
        response = self.client.post("/settings/time-format", data={"time_format": "12h"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("time_format", self.cfg)

    def test_choosing_12_hour_saves_and_returns_to_appearance_settings(self):
        response = self.client.post("/settings/time-format", data={"integration_csrf": "token", "time_format": "12h"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("section=appearance", response.headers["Location"])
        self.assertEqual(self.cfg["time_format"], "12h")

    def test_unknown_values_are_refused_and_nothing_changes(self):
        response = self.client.post("/settings/time-format", data={"integration_csrf": "token", "time_format": "25h"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("time_format", self.cfg)

    def test_pages_and_the_settings_card_show_the_chosen_format(self):
        self.cfg["time_format"] = "12h"
        body = self.client.get("/settings?section=appearance").get_data(as_text=True)
        self.assertIn('data-time-format="12h"', body)
        self.assertIn('value="12h" selected', body)
        self.assertIn("Time format", body)
        self.cfg.pop("time_format")
        body = self.client.get("/settings?section=appearance").get_data(as_text=True)
        self.assertIn('data-time-format="24h"', body)
        self.assertIn('value="24h" selected', body)


if __name__ == "__main__":
    unittest.main()
