"""Site catalog, simulator compatibility, runner lifecycle and HTTP actions."""
import copy
import unittest
from unittest.mock import patch

import app as netem
import site_catalog as sites

SELECTION = dict(industry="manufacturing", sub_industry="automotive", function="plant",
                 size="large", criticality="mission_critical")


class CatalogTests(unittest.TestCase):
    def test_every_supported_selection_builds_valid_steps_and_known_wan_lines(self):
        count = 0
        presets = netem.get_presets({})
        for industry, info in sites.catalog()["industries"].items():
            for sub in info["sub_industries"]:
                for function in info["functions"]:
                    for size in sites.SIZES:
                        for criticality in sites.CRITICALITY:
                            plan = sites.build_site_plan(dict(industry=industry, sub_industry=sub,
                                function=function, size=size, criticality=criticality))
                            self.assertLessEqual(plan["start"]["users"], 5000)
                            self.assertEqual(plan["start"]["users"], min(5000, sum(plan["start"]["personas"].values())))
                            self.assertEqual(set(plan["start"]["applications"]), set(sites.SIMULATOR_APPS))
                            for role in ("primary", "backup"):
                                self.assertIn(plan["wan_lines"][role]["preset"], presets)
                            for test in plan["tests"]:
                                steps = netem.validate_scenario_steps(test["steps"])
                                self.assertEqual(steps[0]["value"]["users"], plan["start"]["users"])
                                self.assertEqual(steps[0]["value"]["label"], plan["label"])
                            count += 1
        self.assertEqual(count, 1728)

    def test_automotive_targets_and_matched_dual_dia(self):
        plan = sites.build_site_plan(SELECTION)
        self.assertEqual(plan["start"]["users"], 960)
        self.assertEqual(plan["workload"]["employees"], 1200)
        self.assertEqual(plan["targets"]["steering_max_s"], 10)
        lines = plan["wan_lines"]
        self.assertEqual([lines[role]["preset"] for role in ("primary", "backup")], ["dia", "dia"])
        self.assertEqual((lines["backup"]["download_mbit"], lines["backup"]["upload_mbit"]), (1000, 1000))
        self.assertEqual(len(plan["tests"]), 6)

    def test_wan_lines_follow_site_category_size_and_criticality(self):
        def lines(**override):
            item = sites.wan_lines(dict(SELECTION, **override))
            return tuple((item[role]["preset"], item[role]["download_mbit"], item[role]["upload_mbit"]) for role in ("primary", "backup"))
        self.assertEqual(lines(criticality="business_critical"), (("dia", 1000, 1000), ("dia", 1000, 1000)))
        self.assertEqual(lines(criticality="standard"), (("dia", 1000, 1000), ("broadband", 500, 50)))
        self.assertEqual(lines(industry="retail", sub_industry="grocery", function="retail_store", size="small", criticality="standard"),
                         (("broadband", 100, 20), ("4g", 80, 20)))
        self.assertEqual(lines(industry="energy", sub_industry="power", function="field_site", size="small", criticality="standard"),
                         (("satellite", 100, 20), ("4g", 80, 20)))
        # Either line of a dual-DIA site carries the whole site.
        pairs = 0
        for category in sites.WAN_LINES.values():
            for by_criticality in category.values():
                for primary, backup in by_criticality.values():
                    if primary[0] == backup[0] == "dia":
                        pairs += 1
                        self.assertEqual(primary, backup)
        self.assertGreater(pairs, 10)
        note = " ".join(sites.wan_lines(SELECTION)["notes"])
        self.assertIn("matching bandwidth", note)

    def test_every_site_test_runs_three_to_five_minutes_in_phases(self):
        for criticality in sites.CRITICALITY:
            for test in sites.build_site_plan(dict(SELECTION, criticality=criticality))["tests"]:
                with self.subTest(criticality=criticality, test=test["id"]):
                    phases, total = netem.scenario_phases(netem.validate_scenario_steps(test["steps"]))
                    self.assertTrue(180 <= total <= 300, total)
                    self.assertEqual(phases[0]["name"], "Warm-up")
                    self.assertTrue(all(phase["planned_s"] > 0 for phase in phases))

    def test_invalid_cascade_is_rejected(self):
        for override in (dict(industry="unknown"), dict(sub_industry="grocery"), dict(function="clinic"), dict(size="huge"), dict(criticality="urgent")):
            with self.assertRaises(ValueError):
                sites.build_site_plan(dict(SELECTION, **override))

    def test_old_simulator_drops_unknown_apps_and_personas_with_warning(self):
        old = dict(applications={"web_saas": {}, "voice": {}}, personas={"knowledge_worker": {}})
        plan = sites.build_site_plan(SELECTION, old)
        self.assertEqual(set(plan["start"]["applications"]), {"web_saas", "voice"})
        self.assertEqual(set(plan["start"]["personas"]), {"knowledge_worker"})
        self.assertNotIn("media_mode", plan["start"])
        self.assertNotIn("label", plan["start"])
        self.assertTrue(plan["warnings"])
        for test in plan["tests"]:
            netem.validate_scenario_steps(test["steps"])

    def test_steering_deadline_rejects_late_success_and_idle(self):
        condition = netem.validate_condition(dict(type="steering", **{"class": "realtime"}, within=10), 1)
        item = dict(**{"class": "realtime"}, verdict="steered", text="moved", reactions=[dict(was_used=True, steered_after_seconds=11)])
        with patch.object(netem, "current_diagnosis", return_value={"steering": {"classes": [item]}}):
            self.assertFalse(netem.evaluate_scenario_condition(condition, "wan1")[0])
            item["reactions"][0]["steered_after_seconds"] = 10
            self.assertTrue(netem.evaluate_scenario_condition(condition, "wan1")[0])
            for verdict in ("idle", "unattributed", "partial"):
                item["verdict"] = verdict
                self.assertFalse(netem.evaluate_scenario_condition(condition, "wan1")[0])
        for within in (0, float("nan"), "wrong"):
            with self.assertRaises(ValueError):
                netem.validate_condition(dict(condition, within=within), 1)


class SiteRoutesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(site_profile=copy.deepcopy(SELECTION), wan_links=[
            dict(id="wan1", name="Primary", inner="lo", outer="lo", bridge="lo", preset="dia"),
            dict(id="wan2", name="Backup", inner="lo", outer="lo", bridge="lo", preset="broadband")])
        self.patches = [patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)),
                       patch.object(netem, "save_config", side_effect=lambda cfg: self.cfg.update(cfg)),
                       patch.object(netem, "log_event"), patch.object(netem, "simulator_catalog", return_value=None),
                       patch.object(netem, "SCENARIO_STATE", dict(active=False)),
                       patch.object(netem, "SITE_PLAN_STATE", dict(active=False, tests=[])),
                       patch.object(netem, "traffic_generator_snapshot", return_value=dict(connected=True))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as session:
            session["integration_csrf"] = "site-test-token"
        netem.SITE_PLAN_STOP.clear()
        netem.SCENARIO_STOP.clear()
        self.addCleanup(netem.SITE_PLAN_STOP.clear)
        self.addCleanup(netem.SCENARIO_STOP.clear)

    def post(self, path, **data):
        return self.client.post(path, data=dict(integration_csrf="site-test-token", **data))

    def test_preview_rejects_bad_selection_and_does_not_save(self):
        self.assertEqual(self.client.get("/api/v1/site-plan", query_string=SELECTION).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/site-plan", query_string=dict(SELECTION, size="huge")).status_code, 400)

    def test_save_only_changes_sla_with_explicit_choice(self):
        self.cfg["sla_profile"] = dict(name="Keep", latency_ms=1)
        self.post("/site/save", **SELECTION)
        self.assertEqual(self.cfg["sla_profile"]["name"], "Keep")
        self.post("/site/save", **SELECTION, apply_sla="1")
        self.assertEqual(self.cfg["sla_profile"]["latency_ms"], 50)
        self.assertEqual(self.client.post("/site/save", data=SELECTION).status_code, 400)

    def test_wan_apply_validates_both_roles_before_changing_config(self):
        before = copy.deepcopy(self.cfg)
        self.post("/site/wan", primary_link="wan1", backup_link="wan1")
        self.assertEqual(self.cfg, before)
        with patch.object(netem, "apply_selected_profile", return_value=(True, "ok", {})) as apply:
            self.post("/site/wan", primary_link="wan1", backup_link="wan2")
            self.assertEqual(apply.call_count, 2)
        self.assertEqual(self.cfg["wan_links"][1]["bandwidth_download_mbit"], 1000)

    def test_run_scales_tests_to_the_requested_length(self):
        with patch.object(netem.threading, "Thread") as worker:
            self.post("/site/run", primary_link="wan1", backup_link="wan2", test_id="baseline", length_min="8")
            steps = worker.call_args.kwargs["args"][1][0]["steps"]
            self.assertAlmostEqual(netem.scenario_phases(steps)[1], 480, delta=5)
            self.post("/site/stop")
            netem.SITE_PLAN_STATE.update(active=False)
            netem.SITE_PLAN_STOP.clear()
            self.post("/site/run", primary_link="wan1", backup_link="wan2", test_id="baseline", length_min="90")
            self.assertEqual(worker.call_count, 1)

    def test_run_overrides_users_and_blocks_overlapping_tests(self):
        with patch.object(netem.threading, "Thread") as worker:
            self.post("/site/run", primary_link="wan1", backup_link="wan2", users="123", test_id="baseline")
            self.assertEqual(worker.call_args.kwargs["args"][1][0]["steps"][0]["value"]["users"], 123)
            worker.return_value.start.assert_called_once()
            self.post("/site/run", primary_link="wan1", backup_link="wan2")
            self.assertEqual(worker.call_count, 1)
        self.post("/site/stop")
        self.assertTrue(netem.SITE_PLAN_STOP.is_set())
        self.assertTrue(netem.SCENARIO_STOP.is_set())

    def test_invalid_users_or_roles_never_start_worker(self):
        with patch.object(netem.threading, "Thread") as worker:
            for users in ("0", "5001", "1.5"):
                self.post("/site/run", primary_link="wan1", backup_link="wan2", users=users)
            self.post("/site/run", primary_link="wan1", backup_link="wan1")
            self.post("/site/run", primary_link="wan1", backup_link="missing")
            worker.assert_not_called()

    def test_runner_orders_results_and_stop_skips_remaining_tests(self):
        plan = sites.build_site_plan(SELECTION)
        tests = plan["tests"][:3]
        def reset():
            netem.SITE_PLAN_STATE.update(active=True, tests=[dict(id=t["id"], status="pending") for t in tests])
        calls = []
        def run(link, scenario, intro_s):
            # Each test of the plan is announced on the showroom before it starts.
            self.assertEqual(intro_s, netem.TEST_INTRO_S)
            calls.append(scenario["id"])
            netem.SCENARIO_STATE.update(active=False, result="passed", error=None)
        reset()
        with patch.object(netem, "run_scenario", side_effect=run):
            netem.run_site_plan(plan, tests, dict(primary="wan1", backup="wan2"))
        self.assertEqual(calls, ["site_"+t["id"] for t in tests])
        self.assertEqual(netem.SITE_PLAN_STATE["result"], "passed")
        calls.clear()
        reset()
        def stop(link, scenario, intro_s):
            run(link, scenario, intro_s)
            netem.SITE_PLAN_STOP.set()
        with patch.object(netem, "run_scenario", side_effect=stop):
            netem.run_site_plan(plan, tests, dict(primary="wan1", backup="wan2"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(netem.SITE_PLAN_STATE["result"], "stopped")
        self.assertEqual(netem.SITE_PLAN_STATE["tests"][1]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
