"""Each rule turns a step's readings into a finding that names its evidence and the next check."""
import unittest

import bottlenecks as bn


def host(**values):
    row = {"timestamp": 1.0, "cpu_busy_pct": 5.0, "cpu_steal_pct": 0.0, "psi_cpu": 0.0, "psi_memory": 0.0,
           "psi_io": 0.0, "sampler_lag_ms": 0.5, "nic_drops_ps": 0.0}
    row.update(values)
    return row


def step(target=100, achieved=99.0, efficiency=0.99, drop_pct=None, host_rows=None, sparse=None, probe=None,
         status="pass", expected=None):
    return {"target_mbps": target, "expected_mbps": expected or target, "achieved_mbps": achieved,
            "efficiency": efficiency, "drop_pct": drop_pct, "status": status,
            "diagnostics": {"host_timeline": host_rows or [host()], "sparse": sparse or {},
                            "probe": probe or {"skipped": "none"}}}


def categories(findings):
    return {item["category"] for item in findings}


class HypervisorAndHostTests(unittest.TestCase):
    def test_steal_is_a_warning_then_bad_and_points_at_the_hypervisor(self):
        warn = bn.step_findings(step(host_rows=[host(cpu_steal_pct=3.0)]), 300)
        self.assertEqual([(item["category"], item["severity"]) for item in warn if item["category"] == "hypervisor"],
                         [("hypervisor", "warn")])
        bad = [item for item in bn.step_findings(step(host_rows=[host(cpu_steal_pct=6.4)]), 300)
               if item["category"] == "hypervisor"]
        self.assertEqual(bad[0]["severity"], "bad")
        self.assertIn("Proxmox host", bad[0]["action"])

    def test_host_cpu_pressure_and_nic_drops_are_findings_on_the_netem_host(self):
        findings = bn.step_findings(step(host_rows=[host(cpu_busy_pct=92.0, psi_io=25.0, nic_drops_ps=3.0)]), 300)
        titles = [item["title"] for item in findings if item["category"] == "netem_host"]
        self.assertTrue(any("CPU busy up to 92%" in title for title in titles))
        self.assertTrue(any("disk I/O" in title for title in titles))
        self.assertTrue(any("NIC drops" in title for title in titles))

    def test_a_late_sampler_makes_the_measurement_a_finding(self):
        findings = bn.step_findings(step(host_rows=[host(sampler_lag_ms=1500.0)]), 300)
        self.assertIn("measurement", categories(findings))


class ShaperAndSparseTests(unittest.TestCase):
    def test_drops_while_the_wan_delivers_are_a_shaper_finding(self):
        findings = bn.step_findings(step(achieved=294.0, efficiency=0.98, drop_pct=17.0), 300)
        overflow = [item for item in findings if item["category"] == "shaper" and item["severity"] == "warn"]
        self.assertEqual(len(overflow), 1)
        self.assertIn("17.0% of packets", overflow[0]["title"])

    def test_sparse_loss_is_bad_from_one_percent_and_names_the_traffic(self):
        findings = bn.step_findings(step(sparse={"ot_loss_pct": 1.5, "ot_jitter_p95_ms": 9.0, "ot_p95_ms": 60.0}), 300)
        loss = [item for item in findings if "OT telemetry lost" in item["title"]]
        self.assertEqual(loss[0]["severity"], "bad")
        self.assertIn("jitter p95 9.0 ms", loss[0]["evidence"])

    def test_a_small_sparse_loss_is_a_warning(self):
        findings = bn.step_findings(step(sparse={"ot_loss_pct": 0.2}), 300)
        self.assertEqual([item["severity"] for item in findings if "OT telemetry lost" in item["title"]], ["warn"])


class LatencyAndGeneratorTests(unittest.TestCase):
    def test_loaded_latency_is_compared_with_the_unloaded_baseline(self):
        probe = {"success": True, "latency_ms": 30.0, "configured_delay_ms": 5.0,
                 "additional_latency_ms": 25.0, "unloaded_baseline": {"latency_ms": 5.0}}
        findings = bn.step_findings(step(probe=probe), 300)
        latency = [item for item in findings if item["category"] == "latency"]
        self.assertEqual(latency[0]["severity"], "bad")

    def test_configured_delay_alone_does_not_establish_added_latency(self):
        findings = bn.step_findings(step(probe={"success": True, "latency_ms": 30, "configured_delay_ms": 5}), 300)
        self.assertNotIn("latency", categories(findings))

    def test_a_shortfall_with_no_host_limit_points_at_the_generator_or_path(self):
        findings = bn.step_findings(step(target=400, achieved=200.0, efficiency=0.6, expected=300), 300)
        self.assertIn("generator", categories(findings))

    def test_a_shortfall_with_a_host_limit_is_not_blamed_on_the_generator(self):
        findings = bn.step_findings(step(target=400, achieved=200.0, efficiency=0.6, expected=300,
                                         host_rows=[host(cpu_steal_pct=6.0)]), 300)
        self.assertNotIn("generator", categories(findings))

    def test_the_wan_rate_being_reached_is_information(self):
        findings = bn.step_findings(step(target=400, achieved=294.0, efficiency=0.98, expected=300), 300)
        reached = [item for item in findings if item["severity"] == "info"]
        self.assertIn("configured 300 Mbit/s", reached[0]["title"])


class RunSummaryTests(unittest.TestCase):
    def test_the_cause_is_the_most_severe_finding_of_the_failing_step(self):
        failing = step(target=400, achieved=120.0, efficiency=0.4, expected=400, status="fail",
                       host_rows=[host(cpu_steal_pct=6.0)])
        summary = bn.analyse({"steps": [step(50, 49.0), failing], "wan_rate_mbps": None})
        self.assertEqual(summary["bottleneck"], "hypervisor")
        self.assertEqual(summary["bottleneck_title"], "Hypervisor CPU steal on the NetEm VM")

    def test_findings_are_sorted_most_severe_first_and_missing_probes_are_noted(self):
        summary = bn.analyse({"steps": [step(sparse={"ot_loss_pct": 2.0}, probe={"skipped": "none"})],
                              "wan_rate_mbps": 300})
        severities = [item["severity"] for item in summary["findings"]]
        self.assertEqual(severities, sorted(severities, key=lambda s: -bn.SEVERITY_RANK[s]))
        self.assertTrue(any(item["title"] == "No ICMP probe is configured for this WAN" for item in summary["findings"]))

    def test_a_clean_run_has_no_cause(self):
        summary = bn.analyse({"steps": [step(50, 49.0), step(100, 98.0)], "wan_rate_mbps": 300,
                              "steps_probe": None})
        self.assertIsNone(summary["bottleneck"])


if __name__ == "__main__":
    unittest.main()
