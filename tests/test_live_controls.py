"""WAN controls preserve HTML fallback and report asynchronous apply failures."""
import unittest
from unittest.mock import patch
import app as netem


class QuickControlTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        self.link = {"id": "wan1", "name": "WAN1"}
        self.patches = [patch.object(netem, "load_config", return_value={}),
                        patch.object(netem, "get_presets", return_value={}),
                        patch.object(netem, "get_link", return_value=self.link),
                        patch.object(netem, "scenario_snapshot", return_value={"active": False}),
                        patch.object(netem, "save_config"), patch.object(netem, "log_event")]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def test_async_quality_reports_apply_result_without_redirect(self):
        for success in (True, False):
            with patch.object(netem, "apply_selected_profile", return_value=(success, "test failure", {})):
                result = self.client.post("/wan/quick", data={"link_id": "wan1", "action": "quality", "quality": "60"},
                                          headers={"Accept": "application/json"})
                self.assertEqual(result.status_code, 200 if success else 400)
                self.assertEqual(result.json["ok"], success)
                self.assertTrue(result.json["messages"])
                self.assertNotIn("Location", result.headers)

    def test_html_fallback_and_scenario_guard(self):
        result = self.client.post("/wan/quick", data={"link_id": "wan1", "action": "invalid"})
        self.assertEqual(result.status_code, 302)
        with patch.object(netem, "scenario_snapshot", return_value={"active": True}), \
             patch.object(netem, "apply_selected_profile") as apply:
            result = self.client.post("/wan/quick", data={"link_id": "wan1", "action": "quality"},
                                      headers={"Accept": "application/json"})
            self.assertEqual(result.status_code, 400)
            apply.assert_not_called()
