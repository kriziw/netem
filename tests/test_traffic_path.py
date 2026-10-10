"""Traffic path readiness: NetEm only starts simulated users whose traffic crosses the appliance,
repairs the path when the simulator can, and notices when the simulator's traffic and its own
measurements cannot both be right."""
import unittest
from unittest.mock import patch

import app as netem

PATH = {"interface": "eth1", "gateway": "10.250.10.1", "target": "198.18.0.1", "source": "10.250.10.10"}
READY = {"ready": True, "repairable": False, "saved_route": True, "message": "Traffic to 198.18.0.1 goes through the appliance.",
         "path": PATH, "target": {"ok": True, "message": "answers"}, "busy": False, "job": {}}
BROKEN = {"ready": False, "repairable": True, "saved_route": True, "message": "eth1 is down. The simulator can restore it.",
          "path": PATH, "busy": False, "job": {}}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("TRAFFIC_PATH_CACHE", {"checked": None, "payload": None}),):
            mock = patch.object(netem, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        for target, kwargs in (("traffic_generator_config", {"return_value": {"host": "192.168.0.135"}}),
                               ("traffic_generator_api_key", {"return_value": "key"}),
                               ("load_config", {"return_value": {}}), ("log_event", {})):
            mock = patch.object(netem, target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(netem.SCENARIO_STOP, "wait", return_value=False)
        mock.start()
        self.addCleanup(mock.stop)

    def test_readiness_comes_from_the_simulator_and_is_summarized(self):
        with patch.object(netem, "traffic_generator_request", return_value=dict(READY)) as api:
            path = netem.traffic_path_readiness()
            netem.traffic_path_readiness()
        api.assert_called_once_with("/api/v1/network/readiness", timeout=8.0)
        self.assertEqual(path["summary"], "Traffic path ready: via 10.250.10.1 on eth1 · target answers.")
        with patch.object(netem, "traffic_generator_request", return_value=dict(BROKEN)):
            self.assertEqual(netem.traffic_path_readiness(max_age=0)["summary"],
                             "Traffic path not ready: eth1 is down. The simulator can restore it.")
        with patch.object(netem, "traffic_generator_request", side_effect=RuntimeError("Traffic Simulator connection failed: timed out")):
            unknown = netem.traffic_path_readiness(max_age=0)
        self.assertFalse(unknown["available"])
        self.assertTrue(unknown["summary"].startswith("Traffic path unknown"))

    def test_older_simulators_are_judged_from_the_selected_route(self):
        network = {"selected": PATH, "route_health": {"active": False, "message": "eth1 has no IPv4 address."},
                   "appliances": [dict(PATH, id="fgt", name="FortiGate")], "job": {}}
        with patch.object(netem, "traffic_generator_request",
                          side_effect=[RuntimeError("Traffic Simulator returned HTTP 404: not found"), network]):
            path = netem.traffic_path_readiness(max_age=0)
        self.assertEqual((path["ready"], path["repairable"], path["appliance_id"], path["legacy"]), (False, True, "fgt", True))
        with patch.object(netem, "traffic_generator_request",
                          side_effect=[RuntimeError("Traffic Simulator returned HTTP 404: not found"), dict(network, selected=None)]):
            unselected = netem.traffic_path_readiness(max_age=0)
        self.assertIsNone(unselected["ready"])
        self.assertIn("update it", unselected["summary"])

    def test_repair_waits_for_the_simulator_job(self):
        calls = []

        def simulator(path, method="GET", payload=None, timeout=3.0):
            calls.append((method, path))
            if path == "/api/v1/network/repair":
                return {"id": "job1", "action": "repair"}
            done = len([call for call in calls if call[1].endswith("readiness")]) > 2
            return dict(READY, job={"id": "job1", "state": "completed"}) if done else dict(BROKEN, job={"id": "job1", "state": "running"})

        with patch.object(netem, "traffic_generator_request", side_effect=simulator):
            path = netem.repair_traffic_path()
        self.assertTrue(path["ready"])
        self.assertIn(("POST", "/api/v1/network/repair"), calls)
        legacy = dict(BROKEN, legacy=True, appliance_id="fgt", available=True)
        with patch.object(netem, "traffic_path_readiness", side_effect=[legacy, dict(READY, job={"id": "job2", "state": "completed"})]), \
             patch.object(netem, "traffic_generator_request", return_value={"id": "job2"}) as api:
            self.assertTrue(netem.repair_traffic_path()["ready"])
        api.assert_called_once_with("/api/v1/network/select", method="POST", payload={"appliance_id": "fgt"}, timeout=8.0)

    def test_starting_traffic_repairs_or_refuses(self):
        with patch.object(netem, "traffic_path_readiness", return_value=dict(READY, available=True)), \
             patch.object(netem, "repair_traffic_path") as repair:
            netem.ensure_traffic_path("Baseline")
            repair.assert_not_called()
        with patch.object(netem, "traffic_path_readiness", return_value=dict(BROKEN, available=True)), \
             patch.object(netem, "repair_traffic_path", return_value=dict(READY, available=True)):
            self.assertTrue(netem.ensure_traffic_path("Baseline")["ready"])
        bypass = {"ready": False, "repairable": False, "available": True,
                  "message": "No appliance route is selected, so traffic to 198.18.0.1 would leave through the management interface (eth0)."}
        with patch.object(netem, "traffic_path_readiness", return_value=bypass):
            with self.assertRaisesRegex(RuntimeError, "Traffic path not ready: No appliance route"):
                netem.ensure_traffic_path("Baseline")
        # Unknown readiness does not block; the start request itself reports connection problems.
        with patch.object(netem, "traffic_path_readiness", return_value={"available": False, "ready": None}):
            netem.ensure_traffic_path("Baseline")

    def test_ui_start_is_refused_when_the_path_cannot_be_fixed(self):
        client = netem.app.test_client()
        with client.session_transaction() as state:
            state["integration_csrf"] = "token"

        def flashes():
            with client.session_transaction() as state:
                return [message for _category, message in state.pop("_flashes", [])]

        with patch.object(netem, "ensure_traffic_path", side_effect=RuntimeError("Traffic path not ready: no appliance route")),              patch.object(netem, "traffic_generator_request") as api:
            client.post("/traffic-generator/start", data={"integration_csrf": "token", "users": "5"})
        api.assert_not_called()
        self.assertEqual(flashes(), ["Traffic path not ready: no appliance route"])
        with patch.object(netem, "repair_traffic_path", return_value=dict(READY, summary="Traffic path ready: via 10.250.10.1 on eth1.")):
            self.assertEqual(client.post("/integrations/traffic-generator/repair").status_code, 400)
            client.post("/integrations/traffic-generator/repair", data={"integration_csrf": "token", "return_to": "settings"})
        self.assertEqual(flashes(), ["Traffic path ready: via 10.250.10.1 on eth1."])


class WorkloadFindingTests(unittest.TestCase):
    def signal(self, down, up):
        return {"link_id": "wan1", "directions": {"down": {"rate_mbps": down}, "up": {"rate_mbps": up}}}

    def test_a_missing_test_workload_is_reported(self):
        finding = netem.workload_finding({"status": "idle", "users": 0, "dem": {}}, [self.signal(30.0, 20.0)], expected_run="run-7")
        self.assertEqual((finding["id"], finding["severity"], finding["source"]), ("workload_missing", "bad", "platform"))
        self.assertIn("NetEm started workload run-7", finding["detail"])
        self.assertIn("different simulator", finding["detail"])
        other = netem.workload_finding({"status": "running", "run": {"run_id": "run-9"}, "dem": {}}, [], expected_run="run-7")
        self.assertIn("with run run-9", other["detail"])
        self.assertIsNone(netem.workload_finding({"status": "running", "run": {"run_id": "run-7"}, "dem": {}}, [self.signal(1.0, 1.0)], "run-7"))

    def test_traffic_that_never_reaches_netem_is_reported(self):
        status = {"status": "running", "run": {"run_id": "run-7"}, "dem": {"requests": 120, "window_seconds": 60}}
        finding = netem.workload_finding(status, [self.signal(0.0, 0.01), self.signal(0.0, 0.0)])
        self.assertEqual(finding["id"], "workload_bypass")
        self.assertIn("120 transactions", finding["detail"])
        self.assertIsNone(netem.workload_finding(status, [self.signal(12.0, 3.0)]))
        self.assertIsNone(netem.workload_finding(status, [self.signal(None, None)]))
        self.assertIsNone(netem.workload_finding(dict(status, dem={"requests": 5}), [self.signal(0.0, 0.0)]))

    def test_showroom_leaves_platform_findings_to_operators(self):
        finding = netem.workload_finding({"status": "idle", "dem": {}}, [], expected_run="run-7")
        self.assertEqual(netem.showroom_outcome({"findings": [finding]}, None)["findings"], [])


if __name__ == "__main__":
    unittest.main()
