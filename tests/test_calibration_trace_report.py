"""The calibration log shows the simulator trace and the step timings."""
import unittest

import calibration_log


def result_with(trace, timings):
    verdict = {"target_mbps": 50, "status": "fail", "achieved_mbps": 12.5, "expected_mbps": 50, "host_status": "ok",
               "reasons": ["achieved 12.5 of 50 Mbit/s"], "warnings": [],
               "diagnostics": {"trace": trace, "timings": timings}}
    return {"link_id": "wan1", "steps": [verdict], "events": [], "findings": [], "mode": "unshaped", "error": None,
            "capacity_mbps": None, "limited_by": "generator", "settle_s": 10.0, "measure_s": 30.0,
            "wan_rate_mbps": None}


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.trace = {
            "summary": {"polls": 5, "poll_errors": 1, "poll_max_ms": 2100.0, "sim_mbps_mean": 12.5,
                        "sim_requests_ps_mean": 3.0, "in_flight_max": 4, "loop_max_ms": 300.0,
                        "loop_stall_seconds": 1, "status_ms_max": 40.0, "dem_ms_max": 25.0, "write_ms_max": 12.0,
                        "target_max_ms": 5.0, "target_no_answer_seconds": 0, "restarts": 0, "quiet_seconds": 3,
                        "quiet_loop_seconds": 1, "quiet_target_seconds": 0, "quiet_waiting_seconds": 1,
                        "quiet_idle_seconds": 1},
            "seconds": [{"offset_s": 2.0, "wan_down_mbps": 0.0, "sim_mbps": None, "sim_requests_ps": None,
                         "in_flight": 0, "loop_max_ms": 300.0, "poll_ms": None, "poll_error": "timed out",
                         "target_ms": None, "quiet": True}],
        }
        self.timings = {"status_before_ms": 40.0, "status_after_ms": 35.0, "dem_window_ms": 22.0,
                        "stop_workload_ms": 5.0, "measure_wan_ms": 3.0}

    def test_the_report_shows_the_trace_summary_and_its_per_second_rows(self):
        text = calibration_log.text_report(result_with(self.trace, self.timings))
        self.assertIn("Simulator trace: polls 5 (1 unanswered", text)
        self.assertIn("Quiet WAN seconds 3: loop held 1, target slow 0, requests waiting on the WAN 1, generator idle 1", text)
        self.assertIn("no answer", text)

    def test_the_report_shows_the_step_timings(self):
        text = calibration_log.text_report(result_with(self.trace, self.timings))
        self.assertIn("Timings (ms): status before 40 · status after 35 · DEM window 22 · stop workload 5 · measure WAN 3", text)

    def test_the_event_line_names_what_the_simulator_sent_and_its_loop_stall(self):
        text = calibration_log.text_report(result_with(self.trace, self.timings))
        self.assertIn("simulator sent 12.5 Mbit/s · loop max 300 ms · quiet WAN seconds 3", text)

    def test_an_older_simulator_is_reported_as_not_recording_the_trace(self):
        text = calibration_log.text_report(result_with(None, None))
        self.assertIn("Simulator trace: not recorded", text)
        self.assertIn("Timings: not recorded", text)


if __name__ == "__main__":
    unittest.main()
