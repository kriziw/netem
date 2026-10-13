"""The ladder's stopping reason says what the steps showed, and does not claim the infrastructure was tested to its limit."""
import unittest

import calibration as cal


class StoppingReasonTests(unittest.TestCase):
    def test_a_top_step_short_of_the_configured_rate_is_not_called_exhaustion(self):
        steps = [{"status": "pass", "achieved_mbps": 41.0, "target_mbps": 50, "reasons": []}]
        cause = cal.summarise(steps, 300)["limited_by"]
        self.assertIn("no step failed", cause)
        self.assertIn("14% of the configured 300 Mbit/s", cause)

    def test_an_unshaped_ladder_that_never_failed_names_the_rung_it_stopped_at(self):
        steps = [{"status": "pass", "achieved_mbps": 50.0, "target_mbps": 50, "reasons": []}]
        self.assertEqual(cal.summarise(steps, None)["limited_by"], "no step failed: the ladder stopped at 50 Mbit/s")

    def test_no_steps_is_reported_as_no_step(self):
        self.assertEqual(cal.summarise([], 300)["limited_by"], "no step ran")


if __name__ == "__main__":
    unittest.main()
