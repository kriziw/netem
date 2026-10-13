"""The self-test's glue: a run is saved, shown on Platform Health and downloadable; tc output is read per WAN side."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem
from test_platform_test import healthy_facts


class SelfTestRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        patches = [
            patch.object(netem, "PLATFORM_TEST_DIR", Path(directory.name)),
            patch.object(netem, "gather_platform_facts", side_effect=lambda: copy.deepcopy(healthy_facts())),
            patch.object(netem, "platform_environment", return_value={"app version": "test"}),
            patch.object(netem, "load_config", return_value={"wan_links": []}),
            patch.object(netem, "log_event"),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def test_a_run_saves_a_report_and_the_downloads_serve_it(self):
        response = self.client.post("/platform-health/self-test", data={"integration_csrf": "token"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue((netem.PLATFORM_TEST_DIR / "platform-self-test.json").exists())
        text = self.client.get("/platform-health/self-test/text")
        self.assertEqual(text.status_code, 200)
        self.assertIn("attachment", text.headers["Content-Disposition"])
        self.assertIn("NetEm platform self-test", text.get_data(as_text=True))
        bundle = json.loads(self.client.get("/platform-health/self-test/json").get_data(as_text=True))
        self.assertEqual(bundle["report"]["status"], "pass")
        self.assertEqual(bundle["environment"], {"app version": "test"})

    def test_the_page_shows_the_last_result_and_its_downloads(self):
        self.client.post("/platform-health/self-test", data={"integration_csrf": "token"})
        html = self.client.get("/platform-health").get_data(as_text=True)
        self.assertIn("data-self-test-status", html)
        self.assertIn("Download diagnostics (JSON)", html)

    def test_downloads_before_any_run_are_not_found(self):
        self.assertEqual(self.client.get("/platform-health/self-test/text").status_code, 404)
        self.assertEqual(self.client.get("/platform-health/self-test/pdf").status_code, 404)

    def test_a_second_run_while_one_is_in_progress_is_refused(self):
        netem.PLATFORM_TEST_LOCK.acquire()
        try:
            self.assertIsNone(netem.run_platform_self_test())
        finally:
            netem.PLATFORM_TEST_LOCK.release()


class GatheredShapingTests(unittest.TestCase):
    def test_tc_output_is_read_for_each_side_of_the_wan(self):
        outputs = {
            "eth1": "qdisc netem 1: root refcnt 2 limit 1000 delay 5ms\nqdisc tbf 10: parent 1:1 rate 300Mbit burst 375000b\n",
            "eth2": "qdisc netem 1: root refcnt 2 limit 1000\nqdisc tbf 10: parent 1:1 rate 50Mbit burst 3200b\n",
        }
        with patch.object(netem, "run_cmd", side_effect=lambda cmd: (0, outputs[cmd.split()[-1]], "")):
            shaping = netem.applied_shaping({"id": "wan1", "inner": "eth1", "outer": "eth2"},
                                            {"download_mbit": 300, "upload_mbit": 50, "delay_ms": 5.0, "loss_pct": 0.0})
        self.assertEqual(shaping["down"]["applied"]["rate_mbit"], 300)
        self.assertEqual(shaping["down"]["applied"]["delay_ms"], 5.0)
        self.assertEqual(shaping["up"]["applied"]["rate_mbit"], 50)
        self.assertEqual(shaping["up"]["expected"], {"rate_mbit": 50, "delay_ms": 0.0, "loss_pct": 0.0})
        self.assertIn("tbf", shaping["down"]["raw"])

    def test_a_bridge_problem_is_summarised_in_words(self):
        problem = {"link_id": "wan2", "bridge_missing": False, "bridge_down": True,
                   "missing": ["eth5"], "outside": [], "down": [], "no_link": []}
        self.assertEqual(netem.bridge_problem_summary(problem), "wan2: bridge down, port missing eth5")


if __name__ == "__main__":
    unittest.main()
