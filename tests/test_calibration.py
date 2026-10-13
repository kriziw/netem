"""Calibration judges each load step on the throughput the WAN delivered and the host's state; drops are a warning."""
import unittest

import calibration as cal


def step(target, shaped, achieved, drop_pct=0.0, host="ok", late=0.0):
    return cal.judge_step(target, shaped, achieved, drop_pct, host, late, drops_ps=0.0)


class StepVerdictTests(unittest.TestCase):
    def test_a_step_that_carries_its_offered_rate_passes(self):
        verdict = step(50, 300, 49.0)
        self.assertEqual((verdict["status"], verdict["expected_mbps"], verdict["reasons"]), ("pass", 50.0, []))

    def test_a_step_above_the_wan_rate_is_expected_to_reach_the_wan_rate(self):
        self.assertEqual(step(400, 300, 295.0)["status"], "pass")
        failing = step(400, 300, 200.0)
        self.assertEqual(failing["status"], "fail")
        self.assertIn("achieved 200 of 300 Mbit/s", failing["reasons"])

    def test_shaper_drops_are_a_warning_and_do_not_fail_a_step_that_delivers_its_rate(self):
        verdict = step(50, 300, 49.0, drop_pct=8.2)
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(verdict["warnings"], ["8.2% of packets dropped at the shaper"])

    def test_small_drops_produce_no_warning(self):
        self.assertEqual(step(200, 300, 196.0, drop_pct=0.2)["warnings"], [])

    def test_a_host_at_its_limit_or_a_late_sampler_fails_the_step(self):
        self.assertIn("NetEm host at its limit", step(100, 300, 98.0, host="at_limit")["reasons"])
        self.assertIn("sampler late for 4 s", step(100, 300, 98.0, late=4.0)["reasons"])

    def test_a_missing_measurement_fails_rather_than_passing(self):
        verdict = cal.judge_step(100, 300, None, None, "ok", 0.0)
        self.assertEqual(verdict["status"], "fail")
        self.assertIn("no WAN measurement in the window", verdict["reasons"])


class CapacitySummaryTests(unittest.TestCase):
    def test_the_highest_passing_rate_is_the_capacity_and_the_first_failure_names_the_cause(self):
        steps = [step(50, 300, 49.0), step(100, 300, 98.0), step(200, 300, 150.0, drop_pct=3.0)]
        summary = cal.summarise(steps, 300)
        self.assertEqual(summary["capacity_mbps"], 98.0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["limited_by"], "path or traffic generator: the WAN delivered less than offered; "
                                                "the shaper also dropped 3.0% of packets")

    def test_reaching_the_wan_rate_says_the_shaping_is_the_limit(self):
        steps = [step(50, 300, 49.0), step(400, 300, 295.0)]
        summary = cal.summarise(steps, 300)
        self.assertEqual(summary["capacity_mbps"], 295.0)
        self.assertIn("configured WAN rate (300 Mbit/s)", summary["limited_by"])

    def test_a_host_failure_is_named_as_the_limit(self):
        steps = [step(100, 300, 98.0), step(200, 300, 196.0, host="at_limit")]
        self.assertEqual(cal.summarise(steps, 300)["limited_by"], "NetEm host (CPU, scheduling or NIC)")

    def test_no_passing_step_means_no_capacity(self):
        summary = cal.summarise([step(50, 300, 10.0)], 300)
        self.assertIsNone(summary["capacity_mbps"])
        self.assertEqual(summary["limited_by"], "path or traffic generator: the WAN delivered less than offered")

    def test_the_ladder_is_ascending_and_reaches_the_gigabit_level(self):
        self.assertEqual(list(cal.LADDER_MBPS), sorted(cal.LADDER_MBPS))
        self.assertEqual(cal.LADDER_MBPS[-1], 1000)


if __name__ == "__main__":
    unittest.main()
