"""The showcase's experience tiles carry each traffic group's sub-score and sentence from the simulator's summary."""
import unittest

import app as netem

GROUPS = [{"key": "interactive", "label": "Web and business apps", "score": 10, "tone": "bad",
           "text": "Limited by DNS (10/100): 67% of transactions met the 120 ms target."}]


def diagnosis(connected=True, dem=None):
    return {"traffic_generator": {"configured": True, "connected": connected,
                                  "status": {"status": "running", "users": 6, "dem": dem or {}}},
            "findings": [], "steering": {}}


class ShowcaseGroupTests(unittest.TestCase):
    def test_the_showcase_outcome_carries_the_group_sub_scores(self):
        outcome = netem.showroom_outcome(diagnosis(dem={"requests": 36, "experience_score": 10,
                                                        "experience_groups": GROUPS}), None)
        self.assertEqual(outcome["experience"]["groups"], GROUPS)

    def test_no_groups_when_the_simulator_reports_none(self):
        outcome = netem.showroom_outcome(diagnosis(dem={"requests": 0}), None)
        self.assertEqual(outcome["experience"]["groups"], [])

    def test_no_groups_when_the_simulator_is_not_connected(self):
        outcome = netem.showroom_outcome(diagnosis(connected=False), None)
        self.assertEqual(outcome["experience"]["groups"], [])


if __name__ == "__main__":
    unittest.main()
