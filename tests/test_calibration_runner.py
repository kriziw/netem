"""The calibration drives paced steps up the ladder, stops after each one, stops at the first failure, and keeps logs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem


class CalibrationRunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.started, self.stopped = [], []
        self.measurements = []
        patches = [
            patch.object(netem, "CALIBRATION_FILE", Path(directory.name) / "calibration.json"),
            patch.object(netem, "platform_test_busy", return_value=None),
            patch.object(netem, "ensure_traffic_path", return_value={"ready": True}),
            patch.object(netem, "wan_shaped_download", return_value=300.0),
            patch.object(netem, "traffic_generator_request", side_effect=self.request),
            patch.object(netem, "stop_test_owned_workload", side_effect=lambda run_id: self.stopped.append(run_id)),
            patch.object(netem, "measure_wan", side_effect=lambda link, start, end: self.measurements.pop(0)),
            patch.object(netem, "host_headroom", return_value={"status": "ok", "late_seconds": 0}),
            patch.object(netem, "calibration_device", return_value={"down": None, "up": None}),
            patch.object(netem, "qdisc_readings", return_value={}),
            patch.object(netem, "simulator_state", return_value={"status": "running", "users": 10}),
            patch.object(netem, "wan_timeline", return_value=[]),
            patch.object(netem, "host_window", return_value=[]),
            patch.object(netem, "calibration_probe", return_value={"skipped": "No ICMP probe is configured for this WAN."}),
            patch.object(netem, "events_between", return_value=[]),
            patch.object(netem.time, "sleep"),
            patch.object(netem, "log_event"),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, path, method="GET", payload=None, timeout=3.0):
        run_id = f"run-{len(self.started) + 1}"
        if path != "/api/v1/workloads/start":
            return {}
        self.started.append((payload or {}).get("paced_mbps"))
        return {"run": {"run_id": run_id}}

    def test_a_clean_climb_ends_at_the_wan_rate_and_stops_every_workload(self):
        self.measurements = [{"achieved_mbps": 49.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 98.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 196.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 290.0, "drop_pct": 9.0, "drops_ps": 40.0}]
        result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(self.started, [50, 100, 200, 400])
        self.assertEqual(len(self.stopped), 4)
        self.assertEqual(result["capacity_mbps"], 290.0)
        self.assertIn("configured WAN rate (300 Mbit/s)", result["limited_by"])
        self.assertTrue((netem.CALIBRATION_FILE).exists())

    def test_the_first_failure_stops_the_ladder_and_names_the_limit(self):
        self.measurements = [{"achieved_mbps": 49.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 98.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 150.0, "drop_pct": 3.0, "drops_ps": 25.0}]
        result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(self.started, [50, 100, 200])
        self.assertEqual(len(self.stopped), 3)
        self.assertEqual(result["capacity_mbps"], 98.0)
        self.assertEqual(result["limited_by"], "path or traffic generator: the WAN carried less than the step offered")

    def test_each_step_keeps_its_diagnostics_and_logs_one_line(self):
        self.measurements = [{"achieved_mbps": 49.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 150.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        with patch.object(netem, "log_event") as logged:
            result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(len(result["steps"]), 2)
        for verdict in result["steps"]:
            self.assertIn("window", verdict["diagnostics"])
            self.assertIn("simulator_before", verdict["diagnostics"])
            self.assertIn("probe", verdict["diagnostics"])
        step_lines = [call.args[1] for call in logged.call_args_list
                      if call.args[0] == "calibration" and " Mbit/s " in call.args[1]]
        self.assertEqual(len(step_lines), 2)
        self.assertIn("50 Mbit/s PASS", step_lines[0])

    def test_the_text_and_json_logs_are_saved_for_download(self):
        self.measurements = [{"achieved_mbps": 49.0, "drop_pct": 0.0, "drops_ps": 0.0},
                             {"achieved_mbps": 150.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        with patch.object(netem, "events_between",
                          return_value=[{"timestamp": 1.0, "kind": "calibration", "message": "50 Mbit/s PASS"}]):
            netem.run_bandwidth_calibration("wan1")
        body, media, name = netem.calibration_file("text")
        self.assertEqual(name, "netem-bandwidth-calibration.txt")
        self.assertIn("NetEm bandwidth calibration", body)
        self.assertIn("[calibration] 50 Mbit/s PASS", body)
        saved = json.loads(netem.calibration_file("json")[0])
        self.assertEqual(saved["link_id"], "wan1")
        self.assertIsNone(netem.calibration_file("pdf"))

    def test_the_drop_warning_comes_from_the_window_counters(self):
        before = [{"kind": "fq_codel", "handle": "20:", "root": False, "bytes": 1000, "packets": 1000,
                   "drops": 0, "overlimits": 0, "backlog_bytes": 0}]
        after = [dict(before[0], bytes=2000, packets=1950, drops=50)]
        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.5, "drops_ps": 0.2}]
        readings = [{"down": {"device": "eth1", "raw": "", "parsed": before}},
                    {"down": {"device": "eth1", "raw": "", "parsed": after}}]
        with patch.object(netem, "calibration_device", return_value={"down": "eth1", "up": None}):
            with patch.object(netem, "qdisc_readings", side_effect=readings):
                result = netem.run_bandwidth_calibration("wan1")
        verdict = result["steps"][0]
        self.assertEqual(verdict["warnings"], ["5.0% of packets dropped at the shaper"])
        self.assertEqual(verdict["diagnostics"]["shaper"]["drop_pct"], 5.0)

    def test_a_busy_lab_is_not_loaded_and_the_refusal_is_saved(self):
        with patch.object(netem, "platform_test_busy", return_value="a scenario is running"):
            result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(self.started, [])
        self.assertIn("idle lab", result["error"])
        self.assertFalse(netem.CALIBRATION_STATE["running"])

    def test_a_second_calibration_is_refused_while_one_runs(self):
        netem.CALIBRATION_LOCK.acquire()
        try:
            self.assertIsNone(netem.run_bandwidth_calibration("wan1"))
        finally:
            netem.CALIBRATION_LOCK.release()


class SessionEventTests(unittest.TestCase):
    def test_only_the_events_inside_the_window_are_kept(self):
        lines = [json.dumps({"timestamp": 5, "kind": "a", "message": "before"}),
                 json.dumps({"timestamp": 15, "kind": "b", "message": "inside"}),
                 json.dumps({"timestamp": 25, "kind": "c", "message": "after"})]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(netem, "EVENT_LOG_PATH", path):
                events = netem.events_between(10, 20)
        self.assertEqual([event["message"] for event in events], ["inside"])


class CalibrationDownloadRouteTests(unittest.TestCase):
    def test_a_saved_log_downloads_and_a_missing_one_is_not_found(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp) / "bandwidth-calibration.json"
            with patch.object(netem, "CALIBRATION_FILE", base):
                client = netem.app.test_client()
                self.assertEqual(client.get("/platform-health/calibration/text").status_code, 404)
                base.write_text(json.dumps({"link_id": "wan1"}), encoding="utf-8")
                base.with_suffix(".txt").write_text("NetEm bandwidth calibration" + chr(10), encoding="utf-8")
                response = client.get("/platform-health/calibration/text")
                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn("NetEm bandwidth calibration", response.get_data(as_text=True))
                self.assertEqual(client.get("/platform-health/calibration/json").status_code, 200)
                self.assertEqual(client.get("/platform-health/calibration/pdf").status_code, 404)

class UnshapedAndSparseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.payloads, self.measurements = [], []
        self.cfg = {"wan_links": [{"id": "wan1", "inner": "eth1", "outer": "eth2", "bridge": "br-wan1"}]}
        patches = [
            patch.object(netem, "CALIBRATION_FILE", Path(directory.name) / "calibration.json"),
            patch.object(netem, "platform_test_busy", return_value=None),
            patch.object(netem, "ensure_traffic_path", return_value={"ready": True}),
            patch.object(netem, "traffic_generator_request", side_effect=self.request),
            patch.object(netem, "stop_test_owned_workload", return_value=None),
            patch.object(netem, "measure_wan", side_effect=lambda link, start, end: self.measurements.pop(0)),
            patch.object(netem, "host_headroom", return_value={"status": "ok", "late_seconds": 0}),
            patch.object(netem, "calibration_device", return_value={"down": None, "up": None}),
            patch.object(netem, "qdisc_readings", return_value={}),
            patch.object(netem, "simulator_state", return_value={"status": "running"}),
            patch.object(netem, "simulator_dem_window", return_value={"ot_loss_pct": 0.0, "ot_jitter_p95_ms": 2.0}),
            patch.object(netem, "wan_timeline", return_value=[]),
            patch.object(netem, "host_window", return_value=[]),
            patch.object(netem, "calibration_probe", return_value={"skipped": "none"}),
            patch.object(netem, "events_between", return_value=[]),
            patch.object(netem, "load_config", return_value=self.cfg),
            patch.object(netem, "get_presets", return_value={}),
            patch.object(netem, "apply_netem", return_value=(True, "OK")),
            patch.object(netem, "apply_selected_profile", return_value=(True, "OK", {})),
            patch.object(netem.time, "sleep"),
            patch.object(netem, "log_event"),
            patch.dict(netem.RUNTIME_EFFECTIVE, {"wan1": {"effective": {"delay_ms": 5.0, "jitter_ms": 1.0,
                                                                        "loss_pct": 0.0, "download_mbit": 300}}}),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, path, method="GET", payload=None, timeout=3.0):
        if path.startswith("/api/v1/workloads"):
            self.payloads.append(dict(payload or {}))
        return {"run": {"run_id": f"run-{len(self.payloads)}"}}

    def test_each_paced_step_asks_for_a_share_of_sparse_users_and_keeps_its_udp_window(self):
        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(self.payloads[0]["sparse_share"], 0.2)
        self.assertEqual(result["steps"][0]["diagnostics"]["sparse"]["ot_loss_pct"], 0.0)

    def test_an_older_simulator_runs_without_sparse_users_and_says_so(self):
        original = netem.traffic_generator_request

        def older(path, method="GET", payload=None, timeout=3.0):
            if payload and "sparse_share" in payload:
                raise RuntimeError("Unknown workload configuration fields.")
            return original(path, method=method, payload=payload, timeout=timeout)

        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        with patch.object(netem, "traffic_generator_request", side_effect=older):
            result = netem.run_bandwidth_calibration("wan1")
        self.assertNotIn("sparse_share", self.payloads[-1])
        self.assertIn("does not support sparse", result["steps"][0]["diagnostics"]["sparse"]["skipped"])

    def test_an_unshaped_run_lifts_the_download_rate_and_restores_the_profile(self):
        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        result = netem.run_bandwidth_calibration("wan1", unshaped=True)
        self.assertEqual(result["mode"], "unshaped")
        self.assertIsNone(result["wan_rate_mbps"])
        netem.apply_netem.assert_called_once_with("eth1", 5.0, 1.0, 0.0, 0.0)
        netem.apply_selected_profile.assert_called_once()

    def test_the_profile_is_restored_even_when_the_run_fails_part_way(self):
        def explode(link, start, end):
            raise RuntimeError("the simulator stopped answering")

        with patch.object(netem, "measure_wan", side_effect=explode):
            result = netem.run_bandwidth_calibration("wan1", unshaped=True)
        self.assertIn("simulator stopped answering", result["error"])
        netem.apply_selected_profile.assert_called_once()

    def test_an_unshaped_run_never_starts_during_a_lab_session(self):
        with patch.object(netem, "session_snapshot", return_value={"active": True}):
            result = netem.run_bandwidth_calibration("wan1", unshaped=True)
        self.assertIn("cannot run during a lab session", result["error"])
        netem.apply_netem.assert_not_called()
        netem.apply_selected_profile.assert_not_called()

    def test_each_step_keeps_the_simulator_trace_and_its_timings(self):
        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        result = netem.run_bandwidth_calibration("wan1")
        diagnostics = result["steps"][0]["diagnostics"]
        self.assertIn("summary", diagnostics["trace"])
        self.assertIn("seconds", diagnostics["trace"])
        self.assertIn("status_before_ms", diagnostics["timings"])
        self.assertIn("measure_wan_ms", diagnostics["timings"])

    def test_a_failed_restore_is_reported_in_the_result(self):
        self.measurements = [{"achieved_mbps": 10.0, "drop_pct": 0.0, "drops_ps": 0.0}]
        with patch.object(netem, "apply_selected_profile", return_value=(False, "tc failed", {})):
            result = netem.run_bandwidth_calibration("wan1", unshaped=True)
        self.assertEqual(result["restore_error"], "tc failed")


if __name__ == "__main__":
    unittest.main()
