"""A generic test started without a lab session asks the operator first: start a session, or acknowledge the warning
and run the test. Unsaved tests are shown live but never archived."""
import copy
import unittest
from unittest.mock import patch

import app as netem

CONFIG = {
    "mgmt_interface": "eth0",
    "wan_links": [{"id": "wan1", "name": "WAN1 DIA", "inner": "eth1", "outer": "eth2", "bridge": "br-wan1", "preset": "dia"}],
}


class NoSessionWarningTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(CONFIG)
        patches = [
            patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.config)),
            patch.object(netem, "ACTIVE_SESSION", {"active": False, "id": None, "name": None, "started_at": None,
                                                   "site": None, "deadline": None, "appliance": None, "wan": None, "tests": {}}),
            patch.object(netem, "log_event"),
            patch.dict(netem.SCENARIO_STATE),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        netem.SCENARIO_STOP.clear()
        self.addCleanup(netem.SCENARIO_STOP.clear)
        self.addCleanup(netem.end_unsaved_telemetry)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def start(self, **extra):
        data = {"link_id": "wan1", "scenario_id": "sla_failover", "length_min": "", "return_to": "overview",
                "integration_csrf": "token"}
        data.update(extra)
        return self.client.post("/lab/scenario/start", data=data)

    def test_without_a_session_the_operator_is_asked_before_the_test_starts(self):
        with patch.object(netem.threading, "Thread") as thread:
            response = self.start()
        self.assertEqual(response.status_code, 409)
        html = response.get_data(as_text=True)
        self.assertIn('id="no-session-warning"', html)
        self.assertIn('href="/sessions"', html)
        self.assertEqual(html.count('name="no_session_ack"'), 1)
        self.assertIn('name="scenario_id" value="sla_failover"', html)
        self.assertIn('name="link_id" value="wan1"', html)
        self.assertIn('name="return_to" value="overview"', html)
        thread.assert_not_called()
        self.assertFalse(netem.SCENARIO_STATE.get("active"))

    def test_acknowledging_the_warning_runs_the_test_without_a_session(self):
        with patch.object(netem.threading, "Thread") as thread:
            response = self.start(no_session_ack="1")
        self.assertEqual(response.status_code, 302)
        thread.assert_called_once()
        self.assertTrue(netem.SCENARIO_STATE.get("active"))

    def test_an_unsaved_test_is_visible_on_the_showcase(self):
        with patch.object(netem.threading, "Thread"):
            self.start(no_session_ack="1")
        self.assertTrue(netem.SCENARIO_STATE.get("active"))
        with patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.config)):
            snapshot = netem.showroom_snapshot()
        self.assertEqual(snapshot["links"][0]["id"], "wan1")
        self.assertTrue(snapshot["scenario"]["active"])
        self.assertTrue(snapshot["scenario"]["unsaved"])
        self.assertEqual(snapshot["appliance"], {})
        self.assertFalse(snapshot["session"]["active"])

    def test_a_running_session_starts_the_test_without_asking(self):
        with patch.object(netem, "session_snapshot", return_value={"active": True}), \
             patch.object(netem.threading, "Thread") as thread:
            response = self.start()
        self.assertEqual(response.status_code, 302)
        thread.assert_called_once()

    def test_the_old_testing_override_is_gone(self):
        self.assertEqual(self.client.post("/tests/without-session", data={"allow": "1"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
