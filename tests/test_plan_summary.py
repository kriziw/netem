"""After a run sequence, one summary of the whole site test plan: the verdict, a row per test
and insights, kept for the showroom, the Tests page and the event log."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem

TARGETS = {"experience_min": 80, "success_min_pct": 99, "interactive_p95_max_ms": 400, "steering_max_s": 30}
VOICE, WEB, FILES = "Voice & video", "Web, collaboration & DNS", "File transfers"


def phase(name, experience=None, success=None, worst=None, p95=None, steering=None):
    return {"name": name, "experience_score": experience, "success_pct": success,
            "worst_success_pct": success if worst is None else worst, "interactive_p95_ms": p95, "steering": steering or {}}


def checks(passed, failed=()):
    items = [{"label": label, "passed": True} for label in passed] + [{"label": label, "passed": False} for label in failed]
    return {"passed": len(passed), "total": len(items), "items": items}


SUMMARIES = {
    "baseline": {"link": "WAN1", "phases": [phase("Warm-up"), phase("Steady state", 92, 99.98, None, 140)],
                 "remediation": [], "assertions": checks(["Experience ≥ 80"])},
    "primary_outage": {"link": "WAN1", "phases": [phase("Baseline", 92, 99.98, None, 140),
                                                  phase("Outage", 71, 98.1, 97.4, 620, {VOICE: "steered", WEB: "steered"})],
                       "remediation": [{"traffic_class": VOICE, "wan": "WAN1", "seconds": 9, "within_target": True},
                                       {"traffic_class": WEB, "wan": "WAN1", "seconds": 41, "within_target": False}],
                       "assertions": checks(["Voice & video steered within 30 s"], ["Request success ≥ 99% at 192.0.2.7"])},
    "backup_outage": {"link": "WAN2", "phases": [phase("Baseline", 92, 99.98), phase("Backup outage", 91, 99.95)],
                      "remediation": [], "assertions": checks(["Experience ≥ 80"])},
    "primary_saturation": {"link": "WAN1", "phases": [phase("Baseline", 92, 99.98), phase("Saturation", 83, 99.4, 99.1, 380, {FILES: "stuck"})],
                           "remediation": [], "assertions": checks(["Interactive P95 ≤ 400 ms"])},
}
TESTS = [{"id": "baseline", "name": "Baseline experience", "role": "primary", "status": "passed"},
         {"id": "primary_outage", "name": "Primary WAN outage", "role": "primary", "status": "failed"},
         {"id": "backup_outage", "name": "Backup WAN outage", "role": "backup", "status": "passed"},
         {"id": "primary_saturation", "name": "Primary WAN saturation", "role": "primary", "status": "passed"},
         {"id": "flaky_primary", "name": "Flaky primary WAN", "role": "primary", "status": "skipped"}]


class SummaryTests(unittest.TestCase):
    def test_rows_say_what_users_got_in_each_test(self):
        summary = netem.summarize_plan("Automotive plant", TARGETS, TESTS, SUMMARIES, "failed", 1000, 2295)
        self.assertEqual((summary["title"], summary["duration_s"], summary["checks"]), ("3 of 5 tests passed", 1295, {"passed": 4, "total": 5}))
        outage = summary["tests"][1]
        self.assertEqual((outage["experience"], outage["experience_low"], outage["low_phase"], outage["success_low"], outage["p95_max"]),
                         (92, 71, "Outage", 97.4, 620))
        self.assertEqual([item["seconds"] for item in outage["moved"]], [9, 41])
        self.assertEqual(summary["tests"][3]["stuck"], [{"traffic_class": FILES, "phase": "Saturation"}])
        skipped = summary["tests"][4]
        self.assertEqual((skipped["result"], skipped["measured"], skipped["checks"]), ("skipped", False, {"passed": 0, "total": 0}))

    def test_insights_lead_with_what_needs_attention(self):
        insights = netem.summarize_plan("Plant", TARGETS, TESTS, SUMMARIES, "failed", 1000, 2295)["insights"]
        texts = [item["text"] for item in insights]
        self.assertEqual([item["tone"] for item in insights], sorted((item["tone"] for item in insights), key=["fail", "warn", "pass"].index))
        self.assertIn("The appliance moved web, collaboration & DNS off an impaired WAN in 41 s, slower than the 30 s target.", texts)
        self.assertIn("Primary WAN outage missed: Request success ≥ 99% at 192.0.2.7.", texts)
        self.assertIn("File transfers stayed on an impaired WAN during Saturation (Primary WAN saturation).", texts)
        self.assertIn("Lowest experience: 71 during Outage in Primary WAN outage, below the 80 target.", texts)
        self.assertIn("With both WANs healthy, experience was 92 and 99.98% of requests succeeded.", texts)
        self.assertIn("The appliance moved voice & video off an impaired WAN in 9 s, within the 30 s target.", texts)
        self.assertIn("Users did not notice: Backup WAN outage, Primary WAN saturation.", texts)
        self.assertLessEqual(len(insights), netem.PLAN_INSIGHTS_MAX)
        # A clean run says so, and a run without measurements says that instead of guessing.
        clean = netem.summarize_plan("Plant", TARGETS, TESTS[2:3], SUMMARIES, "passed", 0, 10)
        self.assertIn("Experience stayed at or above the 80 target in every test.", [item["text"] for item in clean["insights"]])
        unmeasured = netem.summarize_plan("Plant", TARGETS, TESTS[:1], {}, "failed", 0, 10)
        self.assertEqual([item["text"] for item in unmeasured["insights"]], ["No simulated user traffic was measured in Baseline experience."])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, value in (("RUNTIME_DIR", root), ("LAST_PLAN_SUMMARY_PATH", root / "last-plan-summary.json"),
                            ("SITE_PLAN_SUMMARY", None), ("LAST_TEST_SUMMARY", None)):
            mock = patch.object(netem, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(netem, "log_event")
        self.log = mock.start()
        self.addCleanup(mock.stop)
        self.addCleanup(netem.SITE_PLAN_STATE.update, active=False, tests=[], result=None)
        # The plan runner stamps each test's start; the stand-in runner never resets it.
        self.addCleanup(netem.reset_scenario_state, None, None)
        self.root = root

    def run_plan(self, tests, stop_after=None):
        with netem.RUNTIME_LOCK:
            netem.SITE_PLAN_STATE.update(active=True, label="Plant", started_at=1000, finished_at=None, result=None,
                                         tests=[dict(id=test["id"], name=test["name"], role=test["role"], status="pending",
                                                     link_id=None, error=None) for test in tests])

        def run(link_id, scenario, intro_s):
            test_id = scenario["id"].removeprefix("site_")
            if test_id in SUMMARIES:
                netem.LAST_TEST_SUMMARY = dict(SUMMARIES[test_id], scenario_id=scenario["id"])
            netem.SCENARIO_STATE.update(active=False, result="passed", error=None)
            if test_id == stop_after:
                netem.SITE_PLAN_STOP.set()

        plan = {"label": "Plant", "targets": TARGETS}
        steps = [{"after": 0, "action": "phase", "label": "Measuring", "phase": "Baseline"}]
        with patch.object(netem, "run_scenario", side_effect=run):
            netem.run_site_plan(plan, [dict(test, steps=steps) for test in tests], {"primary": "wan1", "backup": "wan2"})
        return netem.SITE_PLAN_SUMMARY

    def test_finished_plan_keeps_its_summary_for_the_showroom_and_tests_page(self):
        summary = self.run_plan([TESTS[0], TESTS[2]])
        self.assertEqual((summary["title"], summary["result"]), ("2 of 2 tests passed", "passed"))
        self.assertEqual([row["link"] for row in summary["tests"]], ["WAN1", "WAN2"])
        self.assertEqual(json.loads((self.root / "last-plan-summary.json").read_text())["title"], "2 of 2 tests passed")
        self.assertEqual(netem.site_plan_snapshot()["summary"]["title"], "2 of 2 tests passed")
        self.assertTrue(any(call.args[1].startswith("Site test plan summary") for call in self.log.call_args_list))
        # While a plan runs, its state carries no summary.
        with netem.RUNTIME_LOCK:
            netem.SITE_PLAN_STATE["active"] = True
        self.assertIsNone(netem.site_plan_snapshot()["summary"])

    def test_stopped_plan_names_what_ran_and_what_did_not(self):
        summary = self.run_plan([TESTS[0], TESTS[2], TESTS[3]], stop_after="baseline")
        self.assertEqual(summary["title"], "1 of 3 tests passed · stopped after 1")
        self.assertEqual([row["result"] for row in summary["tests"]], ["passed", "skipped", "skipped"])

    def test_a_test_without_its_own_summary_is_not_given_another_tests_numbers(self):
        netem.LAST_TEST_SUMMARY = dict(SUMMARIES["baseline"], scenario_id="site_baseline")
        summary = self.run_plan([{"id": "unrecorded", "name": "Unrecorded", "role": "primary"}])
        self.assertFalse(summary["tests"][0]["measured"])


class ShowroomTests(unittest.TestCase):
    def setUp(self):
        self.summary = netem.summarize_plan("Plant at 198.51.100.4", TARGETS, TESTS, SUMMARIES, "failed", 1000, 2295)

    def test_published_while_current_without_internal_fields_or_addresses(self):
        published = netem.showroom_plan_summary(self.summary, 2295 + 60, {"active": True, "started_at": 900})
        self.assertEqual((published["title"], published["label"]), ("3 of 5 tests passed", "Plant at [address]"))
        self.assertNotIn("192.0.2.7", str(published))
        for hidden in ("id", "missed", "stuck"):
            self.assertNotIn(hidden, published["tests"][1])
        self.assertEqual(published["tests"][1]["moved"][1], {"traffic_class": WEB, "wan": "WAN1", "seconds": 41, "within_target": False})

    def test_hidden_when_old_or_once_a_new_session_starts(self):
        self.assertIsNone(netem.showroom_plan_summary(self.summary, 2295 + netem.SHOWROOM_PLAN_SUMMARY_S, {"active": False}))
        self.assertIsNone(netem.showroom_plan_summary(self.summary, 2400, {"active": True, "started_at": 2350}))
        self.assertIsNone(netem.showroom_plan_summary(None, 2400, {"active": False}))
        self.assertIsNotNone(netem.showroom_plan_summary(self.summary, 2400, {"active": False}))


if __name__ == "__main__":
    unittest.main()
