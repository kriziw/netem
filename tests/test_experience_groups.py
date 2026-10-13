"""Each traffic group shows its own sub-score and one short sentence, from the simulator's per-application scores."""
import unittest

from experience_groups import attach_experience_groups, experience_groups


def application(score, cls="interactive", requests=20, components=None, **extra):
    item = {"class": cls, "requests": requests, "experience_score": score,
            "score_components": components if components is not None else {"deadline": 100.0, "reliability": 100.0,
                                                                          "response_time": 100.0},
            "quality": {}, "experience_targets": {}, "availability_pct": 100.0, "p95_ms": 120.0}
    item.update(extra)
    return item


def label(name):
    return {"dns": "DNS", "updates": "Software updates"}.get(name, name)


class GroupScoreTests(unittest.TestCase):
    def test_each_group_takes_its_worst_application_and_names_what_limits_it(self):
        dem = {"applications": {
            "voice": application(96, "realtime"),
            "dns": application(10, components={"deadline": 10.0, "reliability": 100.0, "response_time": 91.0},
                               quality={"on_time_pct": 66.7}, experience_targets={"deadline_ms": 120.0}),
            "web_saas": application(92),
        }}
        realtime, web, bulk = experience_groups(dem, label)
        self.assertEqual((realtime["key"], web["key"], bulk["key"]), ("realtime", "interactive", "bulk"))
        self.assertEqual((realtime["score"], realtime["tone"]), (96, "good"))
        self.assertEqual((web["score"], web["tone"]), (10, "bad"))
        self.assertTrue(web["text"].startswith("Limited by DNS (10/100): 67% of transactions met the 120 ms target."))
        self.assertIn("1 of 2 scored applications are performing well.", web["text"])

    def test_a_group_with_too_few_transactions_has_no_score(self):
        dem = {"applications": {"voice": application(None, "realtime", requests=3)}}
        realtime = experience_groups(dem, label)[0]
        self.assertIsNone(realtime["score"])
        self.assertEqual(realtime["tone"], "unknown")
        self.assertEqual(realtime["text"], "Too few transactions to score yet: 3 in the window.")

    def test_a_group_without_traffic_says_so(self):
        self.assertEqual(experience_groups({"applications": {}}, label)[2]["text"],
                         "No traffic in this group in the window.")

    def test_a_healthy_group_says_so_and_names_unscored_applications(self):
        dem = {"applications": {"web_saas": application(95), "collaboration": application(None, requests=4)}}
        web = experience_groups(dem, label)[1]
        self.assertEqual(web["tone"], "good")
        self.assertEqual(web["text"], "Performing well. 1 with too few transactions to score yet.")

    def test_failures_are_named_in_plain_words(self):
        dem = {"applications": {"erp": application(40, components={"reliability": 40.0, "deadline": 100.0},
                                                    availability_pct=88.0)}}
        web = experience_groups(dem, label)[1]
        self.assertIn("Limited by erp (40/100): 12.0% of transactions failed.", web["text"])

    def test_transfer_speed_is_compared_with_its_target(self):
        dem = {"applications": {"updates": application(
            55, "bulk", components={"sustained_down": 55.0, "reliability": 100.0},
            quality={"sustained_down_mbps": 27.5}, experience_targets={"min_sustained_down_mbps": 50.0})}}
        bulk = experience_groups(dem, label)[2]
        self.assertEqual((bulk["score"], bulk["tone"]), (55, "warn"))
        self.assertIn("download transfers reached 27.5 Mbit/s against a 50 Mbit/s target", bulk["text"])

    def test_attach_puts_the_groups_on_the_summary_for_the_dashboard_and_its_refresh(self):
        dem = attach_experience_groups({"applications": {"voice": application(96, "realtime")}}, label)
        self.assertEqual([group["key"] for group in dem["experience_groups"]], ["realtime", "interactive", "bulk"])
        self.assertIsNone(attach_experience_groups(None))


if __name__ == "__main__":
    unittest.main()
