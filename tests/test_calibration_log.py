"""The calibration log: qdisc counter deltas, the step summary line and the downloadable text report."""
import unittest

import calibration_log as log


def qdisc(kind, handle, packets, drops, overlimits=0, backlog=0, root=False):
    return {"kind": kind, "handle": handle, "root": root, "packets": packets, "drops": drops,
            "overlimits": overlimits, "backlog_bytes": backlog}


class QdiscDeltaTests(unittest.TestCase):
    def test_the_window_sees_only_what_each_qdisc_did_during_it(self):
        before = [qdisc("netem", "1:", 1000, 10, root=True), qdisc("fq_codel", "20:", 900, 0, backlog=0)]
        after = [qdisc("netem", "1:", 2000, 10, root=True), qdisc("fq_codel", "20:", 1900, 50, overlimits=3, backlog=36864)]
        deltas = {item["qdisc"]: item for item in log.qdisc_deltas(before, after)}
        self.assertEqual(deltas["netem 1:"]["dropped_packets"], 0)
        self.assertEqual(deltas["netem 1:"]["sent_packets"], 1000)
        fq = deltas["fq_codel 20:"]
        self.assertEqual((fq["sent_packets"], fq["dropped_packets"], fq["overlimits"]), (1000, 50, 3))
        self.assertAlmostEqual(fq["drop_pct"], 100 * 50 / 1050, places=2)
        self.assertEqual(fq["backlog_bytes_end"], 36864)

    def test_a_qdisc_missing_at_one_end_or_reset_is_reported_not_guessed(self):
        before = [qdisc("tbf", "10:", 500, 0)]
        after = [qdisc("tbf", "10:", 100, 0), qdisc("fq_codel", "20:", 10, 0)]
        notes = {item["qdisc"]: item.get("note") for item in log.qdisc_deltas(before, after)}
        self.assertEqual(notes["tbf 10:"], "counters reset during the window")
        self.assertEqual(notes["fq_codel 20:"], "not read at both ends of the window")

    def test_no_readings_give_no_deltas(self):
        self.assertEqual(log.qdisc_deltas(None, None), [])


class StepLineTests(unittest.TestCase):
    def test_the_line_names_the_throughput_each_qdisc_drops_and_the_failure(self):
        verdict = {"target_mbps": 50, "status": "pass", "achieved_mbps": 49.0, "expected_mbps": 50.0,
                   "host_status": "ok", "reasons": [], "warnings": ["8.2% of packets dropped at the shaper"],
                   "diagnostics": {"qdisc_delta": {"down": [{"qdisc": "fq_codel 20:", "sent_packets": 1000,
                                                             "dropped_packets": 80, "drop_pct": 7.4}]}}}
        line = log.step_line(verdict)
        self.assertIn("50 Mbit/s PASS", line)
        self.assertIn("WAN achieved 49.0 of expected 50.0 Mbit/s", line)
        self.assertIn("down fq_codel 20: sent 1000 dropped 80 (7.4%)", line)
        self.assertIn("warnings: 8.2% of packets dropped at the shaper", line)

    def test_a_step_without_diagnostics_still_has_a_line(self):
        verdict = {"target_mbps": 100, "status": "fail", "achieved_mbps": None, "expected_mbps": 100.0,
                   "host_status": "unknown", "reasons": ["no WAN measurement in the window"], "warnings": []}
        self.assertIn("100 Mbit/s FAIL", log.step_line(verdict))


class TextReportTests(unittest.TestCase):
    def sample_result(self):
        diagnostics = {
            "window": {"start": 1000.0, "end": 1030.0},
            "qdisc_delta": {"down": [{"qdisc": "fq_codel 20:", "root": False, "sent_packets": 1000,
                                      "dropped_packets": 80, "overlimits": 0, "drop_pct": 7.4,
                                      "backlog_bytes_end": 36864}]},
            "simulator_before": {"status": "running", "users": 10, "dropped_transactions": 0,
                                 "requests_per_second": 4.2, "p95_ms": 300, "availability_pct": 100.0},
            "simulator_after": {"error": "timed out"},
            "probe": {"skipped": "No ICMP probe is configured for this WAN."},
            "wan_timeline": [{"timestamp": 1001.0, "down_mbps": 48.0, "up_mbps": 2.0, "down_queue_drops_ps": 3.0,
                              "down_drop_pct": 6.0, "down_backlog_bytes": 40960}],
            "host_timeline": [{"timestamp": 1001.2, "status": "ok", "cpu_busy_pct": 12.0, "cpu_steal_pct": 0.0,
                               "sampler_lag_ms": 1.5}],
            "tc_end": {"down": "qdisc fq_codel 20: root"},
        }
        step = {"target_mbps": 50, "status": "pass", "achieved_mbps": 49.0, "expected_mbps": 50.0,
                "host_status": "ok", "reasons": [], "warnings": ["8.2% of packets dropped at the shaper"],
                "diagnostics": diagnostics}
        return {"link_id": "wan1", "wan_rate_mbps": 300.0, "settle_s": 10.0, "measure_s": 30.0,
                "capacity_mbps": 49.0, "limited_by": "x", "steps": [step]}

    def test_the_report_has_the_verdict_counters_simulator_state_and_per_second_readings(self):
        text = log.text_report(self.sample_result())
        self.assertIn("Capacity: 49.0 Mbit/s", text)
        self.assertIn("down fq_codel 20: sent 1000 packets, dropped 80 (7.4%)", text)
        self.assertIn("Simulator at start: status running, users 10", text)
        self.assertIn("Simulator at end: unavailable (timed out)", text)
        self.assertIn("Probe: No ICMP probe is configured for this WAN.", text)
        self.assertIn("48.0", text)
        self.assertIn("tc -s qdisc show (down), end of window:", text)
        self.assertIn("qdisc fq_codel 20: root", text)

    def test_an_error_result_is_reported_as_an_error(self):
        text = log.text_report({"link_id": "wan1", "error": "Calibration needs an idle lab: a scenario is running."})
        self.assertIn("Error: Calibration needs an idle lab", text)
        self.assertNotIn("Capacity", text)


class SessionEventTests(unittest.TestCase):
    def test_the_session_events_are_listed_with_their_kind(self):
        text = log.text_report({"link_id": "wan1", "steps": [], "events": [
            {"timestamp": 1000.0, "kind": "calibration", "message": "50 Mbit/s PASS"}]})
        self.assertIn("NetEm events during the session:", text)
        self.assertIn("[calibration] 50 Mbit/s PASS", text)

class ShaperDropTests(unittest.TestCase):
    def test_the_fair_queue_counts_the_drops_once_and_gives_the_window_rate(self):
        delta = {"down": [
            {"qdisc": "netem 1:", "sent_packets": 950, "dropped_packets": 50, "drop_pct": 5.0, "sent_bytes": 1500000},
            {"qdisc": "tbf 10:", "sent_packets": 950, "dropped_packets": 50, "drop_pct": 5.0, "sent_bytes": 1500000},
            {"qdisc": "fq_codel 20:", "sent_packets": 950, "dropped_packets": 50, "drop_pct": 5.0, "sent_bytes": 1500000}]}
        shaper = log.shaper_drops(delta, 10.0)
        self.assertEqual((shaper["drop_pct"], shaper["drops_ps"], shaper["qdisc_mbps"]), (5.0, 5.0, 1.2))

    def test_without_a_fair_queue_the_rate_limiter_is_used(self):
        delta = {"down": [{"qdisc": "tbf 10:", "sent_packets": 90, "dropped_packets": 10, "drop_pct": 10.0,
                           "sent_bytes": None}]}
        shaper = log.shaper_drops(delta, 5.0)
        self.assertEqual((shaper["drop_pct"], shaper["drops_ps"], shaper["qdisc_mbps"]), (10.0, 2.0, None))

    def test_no_counters_give_no_figures(self):
        self.assertEqual(log.shaper_drops({}, 30.0)["drop_pct"], None)

    def test_the_qdisc_bytes_give_the_window_throughput(self):
        before = [{"kind": "fq_codel", "handle": "20:", "root": False, "bytes": 1000, "packets": 10, "drops": 0,
                   "overlimits": 0, "backlog_bytes": 0}]
        after = [dict(before[0], bytes=3000001000, packets=20)]
        self.assertEqual(log.qdisc_deltas(before, after)[0]["sent_bytes"], 3000000000)

if __name__ == "__main__":
    unittest.main()
