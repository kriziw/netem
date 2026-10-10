"""Open pages follow the lab: a cheap state version changes with what a page's own live
widgets do not cover, and every page carries the version it was rendered with."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem


class VersionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "config.json"
        self.config.write_text("{}")
        for name, value in (("CONFIG_PATH", self.config), ("SCENARIO_STATE", dict(netem.SCENARIO_STATE, active=False)),
                            ("SITE_PLAN_STATE", dict(netem.SITE_PLAN_STATE, active=False)),
                            ("ACTIVE_SESSION", dict(netem.ACTIVE_SESSION, active=False, id=None)), ("ACTIVE_FAULTS", {}),
                            ("CAPTURE_STATE", dict(netem.CAPTURE_STATE, active=False)),
                            ("DIAGNOSIS_CACHE", {"timestamp": 0.0, "checked": None, "payload": None})):
            mock = patch.object(netem, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def changes(self, change):
        before = netem.ui_state_version()
        self.assertEqual(before, netem.ui_state_version())
        change()
        return netem.ui_state_version() != before

    def test_changes_with_what_live_widgets_do_not_cover(self):
        cases = {
            "session starts": lambda: netem.ACTIVE_SESSION.update(active=True, id="s1"),
            "test starts": lambda: netem.SCENARIO_STATE.update(active=True, scenario_id="brownout", intro=True),
            "announcement ends": lambda: netem.SCENARIO_STATE.update(intro=False),
            "test pauses": lambda: netem.SCENARIO_STATE.update(paused=True),
            "plan moves on": lambda: netem.SITE_PLAN_STATE.update(active=True, current="primary_outage"),
            "impairment": lambda: netem.ACTIVE_FAULTS.update(wan1="blackhole"),
            "capture": lambda: netem.CAPTURE_STATE.update(active=True),
            "saved setting": lambda: os.utime(self.config, ns=(time.time_ns(), time.time_ns() + 10**9)),
            "simulator run": lambda: netem.DIAGNOSIS_CACHE.update(payload={"traffic_generator": {
                "connected": True, "status": {"status": "running", "run": {"run_id": "r1"}}}}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertTrue(self.changes(change))

    def test_progress_within_a_test_does_not_refresh_pages(self):
        netem.SCENARIO_STATE.update(active=True, scenario_id="brownout", phase_index=0, step=1)

        def progress():
            netem.SCENARIO_STATE.update(phase_index=2, phase="Recovery", step=6, step_label="Recovered",
                                        condition={"description": "experience"})
            netem.DIAGNOSIS_CACHE.update(payload={"links": [{"link_id": "wan1"}], "findings": [{"title": "x"}]})
        self.assertFalse(self.changes(progress))

    def test_api_returns_the_version_without_contacting_the_simulator(self):
        with patch.object(netem, "traffic_generator_request", side_effect=AssertionError("no simulator call")):
            response = netem.app.test_client().get("/api/v1/ui-version")
        self.assertEqual(response.get_json(), {"version": netem.ui_state_version()})


class PageTests(unittest.TestCase):
    def test_pages_carry_their_version_and_refreshable_status(self):
        client = netem.app.test_client()
        with patch.object(netem, "ACTIVE_FAULTS", {"wan1": "blackhole"}), \
             patch.object(netem, "ACTIVE_SESSION", dict(netem.ACTIVE_SESSION, active=True, name="PoC", id="s1")):
            page = client.get("/settings").get_data(as_text=True)
            version = netem.ui_state_version()
        self.assertIn(f'<main class="page" data-ui-version="{version}">', page)
        status = page.split('id="top-status"', 1)[1].split('id="activity-toggle"', 1)[0]
        self.assertIn("Session · PoC", status)
        self.assertIn("1 active impairment", status)
        alert = page.split('id="global-alert-slot"', 1)[1].split('<main', 1)[0]
        self.assertIn("Active runtime impairment", alert)
        self.assertIn("wan1: blackhole", alert)


if __name__ == "__main__":
    unittest.main()
