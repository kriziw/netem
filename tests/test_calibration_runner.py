"""The calibration drives paced steps up the ladder, stops after each one, and stops at the first failure."""
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
            patch.object(netem.time, "sleep"),
            patch.object(netem, "log_event"),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, path, method="GET", payload=None, timeout=3.0):
        run_id = f"run-{len(self.started) + 1}"
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
                             {"achieved_mbps": 196.0, "drop_pct": 3.0, "drops_ps": 25.0}]
        result = netem.run_bandwidth_calibration("wan1")
        self.assertEqual(self.started, [50, 100, 200])
        self.assertEqual(len(self.stopped), 3)
        self.assertEqual(result["capacity_mbps"], 98.0)
        self.assertEqual(result["limited_by"], "rate limiter queue: drops below the WAN rate")

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


if __name__ == "__main__":
    unittest.main()
