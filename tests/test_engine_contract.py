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
        dem["truncated"] = True
        self.assertEqual(netem.showroom_outcome(diagnosis, None)["experience"]["verdicts"]["experience"], "unknown")

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


class ShapingTests(unittest.TestCase):
    def test_unsupported_jitter_is_rejected_before_mutation(self):
        with patch.object(netem, "run_cmd") as run:
            self.assertFalse(netem.apply_netem("eth1", 0, 30, 0, 100)[0])
        run.assert_not_called()

    def test_identical_verified_profile_preserves_queues(self):
        values = (5, 1, 0, 100, 0, 0, 0, 0)
        with patch.dict(netem.APPLIED_QDISCS, {"eth1": {"values": values, "observed": "qdisc netem"}}, clear=True), \
             patch.object(netem, "run_cmd", return_value=(0, "qdisc netem", "")) as run:
            self.assertTrue(netem.apply_netem("eth1", *values)[0])
        self.assertEqual(run.call_count, 1)
        self.assertIn("qdisc show", run.call_args.args[0])

    def test_partial_failure_restores_old_profile_or_exposes_unknown(self):
        old = {"delay_ms": 5, "jitter_ms": 1, "loss_pct": 0, "download_mbit": 100, "upload_mbit": 50}
        link = {"id": "wan1", "inner": "in", "outer": "out"}
        for restored in (True, False):
            with self.subTest(restored=restored), patch.dict(netem.RUNTIME_EFFECTIVE, {"wan1": {"effective": old}}, clear=True), \
                 patch.object(netem, "get_effective_profile", return_value=dict(old, delay_ms=40)), \
                 patch.object(netem, "apply_netem", side_effect=[(True, "OK"), (False, "upload failed"), (restored, "rollback"), (restored, "rollback")]):
                self.assertFalse(netem.apply_selected_profile(link, {})[0])
                state = netem.RUNTIME_EFFECTIVE["wan1"]
                self.assertEqual(state["apply_status"], "rolled_back" if restored else "unknown")
                self.assertEqual(state["effective"], old if restored else {})


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

    def test_missing_host_evidence_cannot_pass_calibration(self):
        self.assertEqual(calibration.judge_step(100, 100, 100, 0, "unknown", 0)["status"], "fail")
