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

    def test_busy_paced_users_mean_the_path_set_the_rate(self):
        trace = {"sim_mbps_mean": 40.0, "busy_fraction": 0.94, "paced_users": 8, "in_flight_mean": 7.5}
        findings = bn.step_findings(step(target=50, achieved=40.0, efficiency=0.8, trace=trace), 300)
        path = titled(findings, "paced users were busy 94%")
        self.assertEqual(path[0]["category"], "path")
        self.assertIn("never offered", path[0]["evidence"])
        self.assertFalse(any(item["category"] == "generator" for item in findings))

    def test_resting_paced_users_mean_the_generator_set_the_rate(self):
        trace = {"sim_mbps_mean": 30.0, "busy_fraction": 0.5, "paced_users": 8, "in_flight_mean": 4.0}
        findings = bn.step_findings(step(target=50, achieved=30.0, efficiency=0.6, trace=trace), 300)
        self.assertEqual(titled(findings, "paced users rested 50%")[0]["category"], "generator")

    def test_without_busy_figures_the_shortfall_is_unattributed_not_blamed_on_the_generator(self):
        findings = bn.step_findings(step(target=50, achieved=30.0, efficiency=0.6, trace={"sim_mbps_mean": 30.0}), 300)
        self.assertEqual(titled(findings, "The simulator completed 30.0 of 50")[0]["category"], "unattributed")
        self.assertFalse(any(item["category"] == "generator" for item in findings))

    def test_a_saturated_simulator_cpu_is_a_bad_simulator_finding(self):
        findings = bn.step_findings(step(trace={"sim_cpu_pct_max": 95.0, "sim_cpu_pct_mean": 80.0}), 300)
        cpu = titled(findings, "process CPU up to 95%")
        self.assertEqual(cpu[0]["category"], "simulator")
        self.assertEqual(cpu[0]["severity"], "bad")

    def test_slow_first_bytes_with_busy_users_are_a_target_finding(self):
        trace = {"busy_fraction": 0.95, "wait_ms_mean": 400.0, "transfer_ms_mean": 300.0}
        findings = bn.step_findings(step(trace=trace), 300)
        self.assertEqual(titled(findings, "waited 400 ms for their first byte")[0]["category"], "target")

    def test_slow_transfers_after_a_quick_first_byte_are_a_path_finding(self):
        trace = {"busy_fraction": 0.95, "wait_ms_mean": 20.0, "transfer_ms_mean": 900.0}
        findings = bn.step_findings(step(trace=trace), 300)
        self.assertEqual(titled(findings, "took 900 ms to transfer")[0]["category"], "path")

    def test_slow_transfers_at_the_configured_rate_are_the_shapers_doing(self):
        findings = bn.step_findings(step(target=50, achieved=49.0, trace={"busy_fraction": 0.95, "wait_ms_mean": 10.0,
                                                                          "transfer_ms_mean": 900.0}), 50)
        transfer = titled(findings, "took 900 ms to transfer")
        self.assertIn("the shaper set the transfer time", transfer[0]["action"])

    def test_slow_transfers_below_the_configured_rate_point_at_the_receive_side(self):
        findings = bn.step_findings(step(target=50, achieved=20.0, trace={"busy_fraction": 0.95, "wait_ms_mean": 10.0,
                                                                          "transfer_ms_mean": 900.0}), 300)
        transfer = titled(findings, "took 900 ms to transfer")
        self.assertIn("receive side", transfer[0]["action"])

    def test_a_step_that_ran_no_sparse_users_says_so(self):
        s = step()
        s["diagnostics"]["sparse"] = {"ot_loss_pct": None, "requests_per_second": 2.4}
        findings = bn.step_findings(s, 300)
        self.assertEqual(titled(findings, "No sparse users ran in this step")[0]["severity"], "info")

    def test_a_step_without_sparse_figures_is_not_reported_as_having_none(self):
        findings = bn.step_findings(step(), 300)
        self.assertEqual(titled(findings, "No sparse users ran in this step"), [])

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
