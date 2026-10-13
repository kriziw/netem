"""The platform self-test judges gathered facts; each check is pinned against a healthy set with one fault."""
import copy
import json
import unittest

import platform_test as pt


def healthy_facts():
    link = {"id": "wan1", "label": "WAN1 DIA", "fault": "normal",
            "interfaces": [{"role": "inner", "name": "eth1", "present": True, "carrier": True, "errors_delta": 0},
                           {"role": "outer", "name": "eth2", "present": True, "carrier": True, "errors_delta": 0}],
            "shaping": {"down": {"expected": {"rate_mbit": 300, "delay_ms": 5.0, "loss_pct": 0.0},
                                 "applied": {"rate_mbit": 300, "delay_ms": 5.0, "loss_pct": 0.0}},
                        "up": {"expected": {"rate_mbit": 300, "delay_ms": 0.0, "loss_pct": 0.0},
                               "applied": {"rate_mbit": 300, "delay_ms": None, "loss_pct": None}}},
            "probe": {"success": True, "target": "198.51.100.10", "latency_ms": 5.6, "configured_delay_ms": 5.0}}
    return {"links": [link],
            "host": {"status": "ok", "reasons": [], "samples": 900, "window_seconds": 1800, "late_seconds": 0},
            "clock": {"ntp": True, "synchronized": True},
            "telemetry": {"age_s": 1.2, "interval_s": 2.0},
            "database": {"quick_check": "ok", "disk_free_pct": 60.0},
            "simulator": {"connected": True},
            "forwarding": "1",
            "bridge_problems": [],
            "evidence": [{"name": "wan1 inner qdisc", "text": "qdisc netem 1: root delay 5ms"}],
            "raw": {}}


def by_id(results):
    return {item["id"]: item for item in results}


class HealthyLabTests(unittest.TestCase):
    def test_a_healthy_lab_passes_every_judged_check(self):
        report = pt.run_self_test(healthy_facts(), now=1000.0)
        self.assertEqual(report["status"], pt.PASS)
        self.assertEqual(report["counts"][pt.FAIL], 0)
        self.assertEqual(report["counts"][pt.WARN], 0)

    def test_with_no_data_every_check_is_skipped_and_nothing_crashes(self):
        report = pt.run_self_test({}, now=1000.0)
        self.assertEqual(report["status"], pt.SKIP)
        self.assertEqual(report["counts"][pt.FAIL], 0)

    def test_the_worst_judged_check_sets_the_overall_status(self):
        facts = healthy_facts()
        facts["simulator"] = {"connected": False}
        self.assertEqual(pt.run_self_test(facts)["status"], pt.WARN)
        facts["bridge_problems"] = [{"link_id": "wan2", "summary": "wan2: port missing"}]
        self.assertEqual(pt.run_self_test(facts)["status"], pt.FAIL)


class InterfaceAndHostTests(unittest.TestCase):
    def test_a_missing_or_dead_wan_interface_fails_and_drops_are_a_warning(self):
        facts = healthy_facts()
        facts["links"][0]["interfaces"][0].update(present=False)
        facts["links"][0]["interfaces"][1].update(carrier=False, errors_delta=4)
        checks = by_id(pt.run_self_test(facts)["checks"])
        self.assertEqual(checks["interfaces.wan1.inner"]["status"], pt.FAIL)
        self.assertIn("renamed", checks["interfaces.wan1.inner"]["detail"])
        self.assertEqual(checks["interfaces.wan1.outer"]["status"], pt.FAIL)
        self.assertEqual(checks["interfaces.wan1.outer.errors"]["status"], pt.WARN)

    def test_a_host_at_its_limit_fails_and_names_the_reading(self):
        facts = healthy_facts()
        facts["host"] = {"status": "at_limit", "samples": 5, "window_seconds": 1800, "late_seconds": 0,
                         "reasons": [{"label": "CPU steal (hypervisor)", "value": 6.0, "unit": "%"}]}
        check = by_id(pt.run_self_test(facts)["checks"])["host.headroom"]
        self.assertEqual(check["status"], pt.FAIL)
        self.assertIn("CPU steal (hypervisor) 6.0%", check["detail"])

    def test_a_late_sampler_warns_then_fails(self):
        facts = healthy_facts()
        facts["host"]["late_seconds"] = 4
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["sampling.lateness"]["status"], pt.WARN)
        facts["host"]["late_seconds"] = 30
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["sampling.lateness"]["status"], pt.FAIL)

    def test_a_sample_dated_in_the_future_warns_about_the_clock(self):
        facts = healthy_facts()
        facts["telemetry"]["age_s"] = -6.0
        check = by_id(pt.run_self_test(facts)["checks"])["telemetry.freshness"]
        self.assertEqual(check["status"], pt.WARN)
        self.assertIn("in the future", check["detail"])

    def test_stale_or_missing_telemetry_fails(self):
        facts = healthy_facts()
        facts["telemetry"]["age_s"] = 9.0
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["telemetry.freshness"]["status"], pt.FAIL)
        facts["telemetry"]["age_s"] = None
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["telemetry.freshness"]["status"], pt.FAIL)


if __name__ == "__main__":
    unittest.main()
