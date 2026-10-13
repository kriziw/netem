"""An information-level experience finding is not shown as a user impact; a warning or a failure is."""
import unittest

import app as netem


def finding(severity, id="media_loss"):
    return {"source": "experience", "id": id, "severity": severity, "title": "Voice: 1 of 50 bursts lost packets",
            "applications": ["voice"], "wans": []}


class ImpactSeverityTests(unittest.TestCase):
    def test_an_information_finding_is_not_a_user_impact(self):
        impacts = netem.current_impacts({"findings": [finding("info")]}, {}, {})
        self.assertEqual(impacts, {})

    def test_a_warning_is_still_a_user_impact(self):
        impacts = netem.current_impacts({"findings": [finding("warn")]}, {}, {})
        self.assertEqual(len(impacts), 1)
        self.assertIn("breaking up", next(iter(impacts.values()))["impact"])


if __name__ == "__main__":
    unittest.main()
