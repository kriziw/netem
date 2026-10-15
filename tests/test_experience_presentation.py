"""Confirmed misses affect the headline without rewriting measured utility."""
import copy
import unittest

import app as netem
from experience_groups import experience_groups
from experience_presentation import display_score
from tests.test_phase_scoring import record, window


def failing_dem(app_score=97):
    return {"requests": 120, "experience_score": 97, "availability_pct": 100,
            "interactive_p95_ms": 100, "scoring_complete": True, "business_acceptance": "fail",
            "critical_alerts": [{"application": "erp", "severity": "violation", "violations": ["deadline"]}],
            "applications": {"erp": {"experience_score": app_score, "class": "interactive", "requests": 20,
                "scoring_complete": True, "critical": True, "target_violations": ["deadline"],
                "p95_ms": 413, "experience_targets": {"critical": True, "deadline_ms": 400}}}}


class HeadlineTests(unittest.TestCase):
    def test_near_perfect_critical_miss_is_below_passing_band_and_preserves_average(self):
        dem = failing_dem()
        original = copy.deepcopy(dem)
        diagnosis = {"traffic_generator": {"connected": True, "status": {"dem": dem}}}
        result = netem.showroom_outcome(diagnosis, None)["experience"]
        self.assertEqual(result["experience_score"], 74)
        self.assertEqual(result["average_experience_score"], 97)
        self.assertEqual(result["verdicts"]["experience"], "fail")
        self.assertIn("413 ms P95 (target ≤ 400 ms)", result["target_failures"][0])
        self.assertEqual(dem, original)
        self.assertEqual(display_score(dem, 97, minimum=70)["score"], 69)

    def test_worst_confirmed_application_cannot_be_averaged_away(self):
        self.assertEqual(display_score(failing_dem(20), 97)["score"], 20)
        group = experience_groups(failing_dem())[1]
        self.assertEqual(group["score"], 74)
        self.assertNotEqual(group["tone"], "good")
        self.assertIn("Critical targets missed", group["text"])

    def test_observations_overdue_evidence_and_corrupt_windows_do_not_invent_failure(self):
        for issue in ("observation", "row_limit", "dropped_transactions"):
            dem = failing_dem()
            dem["business_acceptance"] = "unknown"
            if issue == "observation":
                dem["critical_alerts"][0]["severity"] = "observation"
                dem["evidence_issues"] = ["outstanding_overdue_operations"]
            else:
                dem["evidence_issues"] = [issue]
            self.assertEqual(display_score(dem, 97)["score"], 97)
        healthy = {"business_acceptance": "pass", "scoring_complete": True}
        self.assertEqual(display_score(healthy, 100)["score"], 100)

    def test_complete_site_target_miss_is_explained_but_unknown_is_not_graded(self):
        dem = {"scoring_complete": True, "business_acceptance": "pass"}
        self.assertEqual(display_score(dem, 100, site_misses=["Completion P95 above 400 ms"])["score"], 74)
        dem["business_acceptance"] = "unknown"
        self.assertEqual(display_score(dem, 100, site_misses=["Completion P95 above 400 ms"])["score"], 100)

    def test_phase_summary_explains_failure_and_retains_baseline_and_recovery(self):
        dem = failing_dem()
        dem["experience"], dem["success"] = dem.pop("experience_score"), dem.pop("availability_pct")
        dem["applications"]["erp"]["experience"] = dem["applications"]["erp"].pop("experience_score")
        ends = [{"name": "Baseline", "dem": window(100, 100, 120)},
                {"name": "Severe brownout", "dem": dem},
                {"name": "Recovery", "dem": window(100, 100, 120)}]
        result = netem.summarize_test(record(ends), "passed", None,
                                     targets={"experience_min": 80, "success_min_pct": 99, "interactive_p95_max_ms": 400})
        self.assertEqual([item["experience_score"] for item in result["phases"]], [100, 74, 100])
        self.assertEqual(result["experience_result"], "fail")
        text = " ".join(result["conclusion"])
        self.assertIn("fell to 74 during Severe brownout", text)
        self.assertIn("Severe brownout missed targets: Erp: 413 ms P95 (target ≤ 400 ms)", text)
        self.assertIn("measured application average was 97/100", text)
