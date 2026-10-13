"""Applied shaping, path latency, bridges, credentials in reports, and the diagnostics output."""
import json
import unittest

import platform_test as pt
from test_platform_test import by_id, healthy_facts


class ShapingAndPathTests(unittest.TestCase):
    def test_applied_shaping_that_differs_from_the_configured_profile_fails(self):
        facts = healthy_facts()
        facts["links"][0]["shaping"]["down"]["applied"] = {"rate_mbit": 150, "delay_ms": 5.0, "loss_pct": 0.0}
        check = by_id(pt.run_self_test(facts)["checks"])["shaping.wan1"]
        self.assertEqual(check["status"], pt.FAIL)
        self.assertIn("down rate: configured 300 Mbit/s, applied 150 Mbit/s", check["detail"])

    def test_a_missing_rate_limiter_is_a_mismatch_not_a_pass(self):
        facts = healthy_facts()
        facts["links"][0]["shaping"]["up"]["applied"] = {"rate_mbit": None, "delay_ms": None, "loss_pct": None}
        check = by_id(pt.run_self_test(facts)["checks"])["shaping.wan1"]
        self.assertEqual(check["status"], pt.FAIL)
        self.assertIn("applied none", check["detail"])

    def test_rounding_inside_tolerance_is_not_a_mismatch(self):
        facts = healthy_facts()
        facts["links"][0]["shaping"]["down"]["applied"] = {"rate_mbit": 299.0, "delay_ms": 5.2, "loss_pct": 0.004}
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["shaping.wan1"]["status"], pt.PASS)

    def test_no_applied_profile_is_skipped(self):
        facts = healthy_facts()
        facts["links"][0]["shaping"] = {"skipped": "No profile has been applied to this WAN yet."}
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["shaping.wan1"]["status"], pt.SKIP)

    def test_path_latency_beyond_the_configured_delay_is_judged_by_its_excess(self):
        facts = healthy_facts()
        probe = facts["links"][0]["probe"]
        probe["latency_ms"] = 13.0          # 8 ms beyond 5 ms configured
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["paths.wan1"]["status"], pt.WARN)
        probe["latency_ms"] = 40.0          # 35 ms beyond
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["paths.wan1"]["status"], pt.FAIL)

    def test_a_failed_probe_fails_and_a_busy_lab_skips_the_path(self):
        facts = healthy_facts()
        facts["links"][0]["probe"] = {"success": False, "target": "198.51.100.10", "detail": "timeout"}
        self.assertEqual(by_id(pt.run_self_test(facts)["checks"])["paths.wan1"]["status"], pt.FAIL)
        facts["links"][0]["probe"] = {"skipped": "a test is running"}
        check = by_id(pt.run_self_test(facts)["checks"])["paths.wan1"]
        self.assertEqual((check["status"], check["detail"]), (pt.SKIP, "a test is running"))


class SoftwareTests(unittest.TestCase):
    def test_bridge_problems_fail_while_forwarding_off_is_not_judged(self):
        facts = healthy_facts()
        facts["forwarding"] = "0"
        facts["bridge_problems"] = [{"link_id": "wan2", "summary": "wan2: outer port missing from br-wan2"}]
        checks = by_id(pt.run_self_test(facts)["checks"])
        self.assertEqual(checks["software.forwarding"]["status"], pt.SKIP)
        self.assertIn("layer 2", checks["software.forwarding"]["detail"])
        self.assertEqual(checks["software.bridges"]["status"], pt.FAIL)
        self.assertIn("outer port missing", checks["software.bridges"]["detail"])

    def test_a_database_that_fails_its_check_or_runs_low_on_space_is_judged(self):
        facts = healthy_facts()
        facts["database"] = {"quick_check": "*** in database main ***", "disk_free_pct": 3.0}
        checks = by_id(pt.run_self_test(facts)["checks"])
        self.assertEqual(checks["database.integrity"]["status"], pt.FAIL)
        self.assertEqual(checks["database.space"]["status"], pt.FAIL)


class ReportTests(unittest.TestCase):
    def test_redaction_removes_credentials_but_keeps_the_rest(self):
        config = {"traffic_generator": {"host": "10.0.0.5", "api_key": "abc123"}, "token": "t", "empty_key": "",
                  "wan_links": [{"id": "wan1", "inner": "eth1"}]}
        cleaned = pt.redact(config)
        self.assertEqual(cleaned["traffic_generator"], {"host": "10.0.0.5", "api_key": "[redacted]"})
        self.assertEqual(cleaned["token"], "[redacted]")
        self.assertEqual(cleaned["empty_key"], "")
        self.assertEqual(cleaned["wan_links"], [{"id": "wan1", "inner": "eth1"}])
        self.assertNotIn("abc123", json.dumps(cleaned))

    def test_the_text_report_carries_each_verdict_its_reading_and_the_evidence(self):
        facts = healthy_facts()
        facts["links"][0]["shaping"]["down"]["applied"]["rate_mbit"] = 150
        report = pt.run_self_test(facts, now=0.0)
        text = pt.diagnostics_text(report, facts, {"app version": "0.24.0", "kernel": "6.8.0"})
        self.assertIn("Overall: FAIL", text)
        self.assertIn("[FAIL] WAN1 DIA applied shaping: down rate: configured 300 Mbit/s, applied 150 Mbit/s", text)
        self.assertIn("measured:", text)
        self.assertIn("qdisc netem 1: root delay 5ms", text)
        self.assertIn("app version: 0.24.0", text)

    def test_the_json_report_is_complete_and_redacted(self):
        facts = healthy_facts()
        report = pt.run_self_test(facts, now=0.0)
        bundle = json.loads(pt.diagnostics_json(report, facts, {"kernel": "6.8.0"},
                                                {"traffic_generator": {"api_key": "secret-value"}}))
        self.assertEqual(bundle["report"]["status"], pt.PASS)
        self.assertEqual(bundle["facts"]["links"][0]["id"], "wan1")
        self.assertEqual(bundle["configuration"]["traffic_generator"]["api_key"], "[redacted]")
        self.assertNotIn("secret-value", json.dumps(bundle))


if __name__ == "__main__":
    unittest.main()
