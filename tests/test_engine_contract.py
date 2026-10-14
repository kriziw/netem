import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app as netem
import site_catalog
import calibration
from experience_groups import experience_groups


class EvidenceTests(unittest.TestCase):
    def test_incomplete_truncated_stale_and_wrong_run_never_pass_assertions(self):
        base = {"timestamp": time.time(), "scoring_complete": True, "run_id": "run-a",
                "endpoint_experience": {"score": 100}}
        condition = {"type": "dem", "field": "experience_score", "op": ">=", "value": 80}
        for change in ({"scoring_complete": False}, {"truncated": True}, {"timestamp": time.time() - 30},
                       {"timestamp": time.time() + 30}, {"run_id": "run-b"}, {"evidence_issues": ["dropped_transactions"]}):
            with self.subTest(change=change), patch.dict(netem.SCENARIO_STATE, {"workload_run_id": "run-a"}), \
                 patch.object(netem, "traffic_generator_request", return_value=dict(base, **change)):
                self.assertFalse(netem.evaluate_scenario_condition(condition, "wan1")[0])

    def test_showroom_keeps_nonzero_utility_and_critical_alert(self):
        dem = {"requests": 100, "experience_score": 91, "scoring_complete": True, "availability_pct": 100,
               "interactive_p95_ms": 100, "critical_alerts": [{"application": "erp", "violations": ["deadline"]}]}
        diagnosis = {"traffic_generator": {"connected": True, "status": {"dem": dem}}}
        result = netem.showroom_outcome(diagnosis, None)["experience"]
        self.assertEqual(result["experience_score"], 91)
        self.assertEqual(result["verdicts"]["experience"], "fail")
        self.assertEqual(result["critical_alerts"], dem["critical_alerts"])
        for invalid in ({"truncated": True}, {"evidence_issues": ["dropped_transactions"]}, {"evidence_issues": ["persistence_unhealthy"]}):
            with self.subTest(invalid=invalid):
                damaged = dict(dem, **invalid)
                state = {"traffic_generator": {"connected": True, "status": {"dem": damaged}}}
                self.assertEqual(netem.showroom_outcome(state, None)["experience"]["verdicts"]["experience"], "unknown")

    def test_partial_coverage_does_not_mask_confirmed_critical_failure(self):
        dem = {"requests": 5, "experience_score": 0, "coverage_pct": 10, "score_status": "provisional",
               "score_range": {"minimum": 0, "maximum": 90}, "scoring_complete": False,
               "business_acceptance": "fail", "critical_alerts": [{"application": "erp", "severity": "outage"}]}
        diagnosis = {"traffic_generator": {"connected": True, "status": {"dem": dem}}}
        experience = netem.showroom_outcome(diagnosis, None)["experience"]
        self.assertEqual(experience["verdicts"]["experience"], "fail")
        self.assertEqual(experience["coverage_pct"], 10)
        dem.update(business_acceptance="unknown", critical_alerts=[{"application": "erp", "severity": "observation"}])
        self.assertEqual(netem.showroom_outcome(diagnosis, None)["experience"]["verdicts"]["experience"], "unknown")

    def test_complete_low_utility_fails_even_without_a_critical_application(self):
        dem = {"requests": 100, "experience_score": 0, "scoring_complete": True,
               "business_acceptance": "unknown", "critical_alerts": []}
        state = {"traffic_generator": {"connected": True, "status": {"dem": dem}}}
        self.assertEqual(netem.showroom_outcome(state, None)["experience"]["verdicts"]["experience"], "fail")
        self.assertEqual(netem.showroom_outcome(state, None)["experience"]["business_acceptance"], "fail")
        dem["scoring_complete"] = False
        self.assertEqual(netem.showroom_outcome(state, None)["experience"]["verdicts"]["experience"], "unknown")

    def test_short_phase_has_exact_boundaries_and_run_identity(self):
        with patch.object(netem, "traffic_generator_request", return_value={"scoring_complete": False}) as request:
            result = netem.phase_window_dem(3.8, since=100, until=103.8, run_id="run-a")
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query, {"since": ["100"], "until": ["103.8"], "run_id": ["run-a"]})
        self.assertIs(result["scoring_complete"], False)

    def test_group_uses_configured_weights_and_preserves_missing_evidence(self):
        dem = {"applications": {"erp": {"class": "interactive", "requests": 20, "weight": .2,
                                        "experience_score": 0, "scoring_complete": True},
                                "web_saas": {"class": "interactive", "requests": 20, "weight": .8,
                                             "experience_score": 100, "scoring_complete": True}}}
        self.assertEqual(experience_groups(dem)[1]["score"], 80)
        dem["applications"]["erp"]["scoring_complete"] = False
        self.assertEqual(experience_groups(dem)[1]["tone"], "unknown")




class SiteTests(unittest.TestCase):
    def test_industry_apps_receive_a_population_and_site_targets_reach_scenarios(self):
        catalog = {"engine_capabilities": ["workload-utility-v2"], "media_modes": {},
                   "personas": {"knowledge_worker": {"applications": {"web_saas": 1}},
                                "warehouse_operator": {"applications": {"wms_scan": 55, "erp": 20}},
                                "camera": {"applications": {"cctv_backhaul": 100}}},
                   "applications": {key: {"class": "interactive"} for key in ("web_saas", "wms_scan", "erp", "cctv_backhaul")}}
        load = {"personas": {"knowledge_worker": 12, "camera": 4}, "users": 16,
                "devices": {"camera": 4}, "application_weights": {"wms_scan": 1.4, "erp": 1.1},
                "activity": "normal", "media_mode": "realistic", "label": "Parcel branch"}
        start, warnings = site_catalog.fit_to_simulator(load, catalog)
        self.assertEqual(start["personas"]["camera"], 4)
        self.assertEqual(sum(start["personas"].values()), 16)
        self.assertGreater(start["personas"]["warehouse_operator"], 0)
        self.assertEqual(warnings, [])
        start["experience_policy"] = {"erp": {"deadline_ms": 400, "criticality": "critical"}}
        step = netem.validate_scenario_steps([{"action": "traffic_generator", "after": 0, "value": start}])[0]
        self.assertEqual(step["value"]["experience_policy"], start["experience_policy"])
