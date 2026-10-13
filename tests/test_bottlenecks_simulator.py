"""The simulator's own readings become findings: its event loop, its status polls, the target, the quiet seconds, and
what the generator sent against what the WAN carried."""
import unittest

import bottlenecks as bn


def step(target=50, achieved=49.0, efficiency=0.98, trace=None):
    diagnostics = {"host_timeline": [{"timestamp": 1.0, "status": "ok", "cpu_busy_pct": 5.0, "cpu_steal_pct": 0.0,
                                      "psi_cpu": 0.0, "psi_memory": 0.0, "psi_io": 0.0, "sampler_lag_ms": 0.5,
                                      "nic_drops_ps": 0.0}],
                   "sparse": {}, "probe": {"skipped": "none"}}
    if trace is not None:
        diagnostics["trace"] = {"summary": trace}
    return {"target_mbps": target, "expected_mbps": target, "achieved_mbps": achieved, "efficiency": efficiency,
            "drop_pct": None, "status": "fail", "diagnostics": diagnostics}


def titled(findings, text):
    return [item for item in findings if text in item["title"]]


class SimulatorFindingTests(unittest.TestCase):
    def test_a_stalled_event_loop_is_a_bad_simulator_finding(self):
        findings = bn.step_findings(step(trace={"loop_max_ms": 800.0, "loop_stall_seconds": 2, "status_ms_max": 40.0,
                                                "dem_ms_max": 300.0, "write_ms_max": 12.0}), 300)
        loop = titled(findings, "event loop held")
        self.assertEqual(loop[0]["category"], "simulator")
        self.assertEqual(loop[0]["severity"], "bad")
        self.assertIn("2 seconds", loop[0]["evidence"])
        self.assertIn("300.0 ms", loop[0]["evidence"])

    def test_each_quiet_second_is_blamed_on_its_own_cause(self):
        trace = {"quiet_seconds": 6, "quiet_loop_seconds": 1, "quiet_target_seconds": 0, "quiet_waiting_seconds": 2,
                 "quiet_idle_seconds": 3, "quiet_offsets": [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}
        findings = bn.step_findings(step(trace=trace), 300)
        self.assertEqual(titled(findings, "1 of 6 quiet seconds")[0]["category"], "simulator")
        self.assertEqual(titled(findings, "2 of 6 quiet seconds")[0]["category"], "path")
        self.assertEqual(titled(findings, "3 of 6 quiet seconds")[0]["category"], "generator")

    def test_the_generator_is_the_limit_when_the_wan_carried_what_was_sent(self):
        findings = bn.step_findings(step(target=50, achieved=30.0, efficiency=0.6, trace={"sim_mbps_mean": 30.0}), 300)
        generator = titled(findings, "The simulator sent 30.0 of 50 Mbit/s")
        self.assertEqual(generator[0]["category"], "generator")
        self.assertEqual(sum(1 for item in findings if item["category"] == "generator"), 1)

    def test_the_path_is_the_limit_when_the_wan_carried_less_than_was_sent(self):
        findings = bn.step_findings(step(target=50, achieved=20.0, efficiency=0.4, trace={"sim_mbps_mean": 40.0}), 300)
        self.assertEqual(titled(findings, "The simulator sent 40.0 of 50 Mbit/s")[0]["category"], "path")
        self.assertFalse(any(item["category"] == "generator" for item in findings))

    def test_a_wan_shortfall_is_reported_when_the_simulator_kept_up(self):
        findings = bn.step_findings(step(target=50, achieved=40.0, efficiency=0.8, trace={"sim_mbps_mean": 48.0}), 300)
        shortfall = titled(findings, "The WAN carried 40.0 of the 48.0 Mbit/s")
        self.assertEqual(shortfall[0]["category"], "path")

    def test_a_slow_target_is_a_target_finding(self):
        findings = bn.step_findings(step(trace={"target_max_ms": 250.0, "target_no_answer_seconds": 0}), 300)
        slow = titled(findings, "Controlled target answered")
        self.assertEqual(slow[0]["category"], "target")
        self.assertIn("250.0", slow[0]["title"])

    def test_without_a_trace_the_generator_fallback_still_applies(self):
        findings = bn.step_findings(step(target=400, achieved=200.0, efficiency=0.5), 300)
        self.assertEqual(titled(findings, "The WAN carried 50% of the target rate")[0]["category"], "generator")


if __name__ == "__main__":
    unittest.main()
