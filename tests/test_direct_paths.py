"""Direct traffic must not be gated or scored as appliance traffic."""
import tempfile
import unittest
from unittest.mock import patch

import app as netem
from appliance_inventory import Store


class DirectPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.platform = netem.APPLIANCE_PLATFORM
        mocked = patch.object(self.platform, "store", Store(temp.name)); mocked.start(); self.addCleanup(mocked.stop)
        self.platform.store.state("traffic_paths", {"paths": [{"id": "direct"}]})
        mocked = patch.object(netem, "log_event"); mocked.start(); self.addCleanup(mocked.stop)

    def run_preflight(self, mode, endpoint, form, ready=True):
        platform = {"network": {"selected": {"mode": mode, "wan_id": "wan1", "name": "Direct router"}}, "path": {"ready": ready}}
        with netem.app.test_request_context(endpoint, method="POST"), \
             patch.object(self.platform, "check_platform", return_value=platform), \
             patch.object(self.platform, "check_appliance") as check:
            self.platform.preflight(form)
            check.assert_not_called()

    def test_full_baseline_workload_is_allowed_without_appliance_license_checks(self):
        self.run_preflight("direct_baseline", "/traffic-generator/start", {})

    def test_clean_baseline_cannot_produce_a_netem_scenario_pass(self):
        with self.assertRaisesRegex(ValueError, "bypasses NetEm"):
            self.run_preflight("direct_baseline", "/lab/scenario/start", {"link_id": "wan1"})

    def test_scenarios_must_use_the_wired_wan_and_cannot_assert_sdwan_steering(self):
        with self.assertRaisesRegex(ValueError, "WAN assigned"):
            self.run_preflight("direct_netem", "/lab/scenario/start", {"link_id": "wan2"})
        scenario = {"id": "test", "steps": [{"condition": {"type": "steering"}}]}
        with patch.object(netem, "get_scenarios", return_value=[scenario]), self.assertRaisesRegex(ValueError, "not applicable"):
            self.run_preflight("direct_netem", "/lab/scenario/start", {"link_id": "wan1", "scenario_id": "test"})
        with patch.object(netem, "get_scenarios", return_value=[]):
            self.run_preflight("direct_netem", "/lab/scenario/start", {"link_id": "wan1"})

    def test_unverified_direct_path_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, "not verified"):
            self.run_preflight("direct_baseline", "/traffic-generator/start", {"appliance_override": "1"}, ready=False)

    def test_intentional_baseline_bypass_is_not_reported_as_a_fault(self):
        status = {"status": "running", "run": {"traffic_path": {"mode": "direct_baseline"}},
                  "dem": {"requests": 120}}
        signals = [{"link_id": "wan1", "directions": {"down": {"rate_mbps": 0}, "up": {"rate_mbps": 0}}}]
        self.assertIsNone(netem.workload_finding(status, signals))
        self.assertIsNotNone(netem.workload_finding(dict(status, run={}), signals))

    def test_direct_mode_does_not_match_inventory_or_display_a_firewall_brand(self):
        row = {"gateway": "10.0.0.1", "target": "198.18.0.1", "interface": "eth1"}
        self.assertFalse(self.platform.route_matches(row, dict(row, mode="direct_baseline")))
        self.platform.store.state("selection", {"mode": "direct_baseline", "name": "Router baseline"})
        self.assertEqual(netem.appliance_under_test()["vendor_name"], "Clean router baseline")


if __name__ == "__main__":
    unittest.main()
