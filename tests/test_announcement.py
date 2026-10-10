"""Every test is announced for a few seconds before it changes anything, so the showroom
shows what is coming. Stopping during the announcement changes nothing."""
import threading
import time
import unittest
from unittest.mock import patch

import app as netem
import site_catalog

SCENARIO = {
    "id": "demo", "name": "Demo outage", "description": "WAN1 fails and recovers.",
    "steps": [{"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
              {"after": 0, "action": "fault", "value": "blackhole", "label": "Blackholed", "phase": "Outage"},
              {"after": 0, "action": "assert", "condition": {"type": "sla", "state": "fail"}, "timeout": 5,
               "label": "SLA detects failure", "phase": "Outage"},
              {"after": 0, "action": "fault", "value": "normal", "phase": "Recovery"}],
}
CONFIG = {"wan_links": [{"id": "wan1", "name": "WAN1", "inner": "ens19", "outer": "ens20", "preset": "dia", "quality": 100}]}


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class PreviewTests(unittest.TestCase):
    def test_preview_names_what_each_phase_does_and_the_checks(self):
        phases, planned_s, checks = netem.scenario_preview(SCENARIO["steps"])
        self.assertEqual([(phase["name"], phase["what"]) for phase in phases],
                         [("Baseline", "Nominal"), ("Outage", "Blackholed"), ("Recovery", "Normal")])
        self.assertEqual((planned_s, checks), (0, ["SLA detects failure"]))
        generic = next(item for item in netem.DEFAULT_SCENARIOS if item["id"] == "progressive_brownout")
        phases, planned_s, _checks = netem.scenario_preview(generic["steps"])
        self.assertEqual(phases[1]["what"], "Minor degradation → Noticeable degradation")
        # A label that only repeats the phase name adds nothing.
        self.assertEqual(phases[2]["what"], "")
        self.assertEqual(planned_s, 260)

    def test_every_site_test_says_what_it_does_and_checks(self):
        plan = site_catalog.build_site_plan({"industry": "manufacturing", "sub_industry": "automotive", "function": "plant",
                                             "size": "large", "criticality": "mission_critical"})
        for test in plan["tests"]:
            with self.subTest(test=test["id"]):
                phases, _planned, checks = netem.scenario_preview(test["steps"])
                self.assertTrue(checks)
                self.assertTrue(all(phase["what"] for phase in phases), phases)


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.mocks = {}
        for name, value in (("load_config", CONFIG), ("apply_selected_profile", (True, "OK", {})),
                            ("apply_runtime_fault", (True, "OK")), ("apply_mtu_limit", (True, "OK")),
                            ("wait_for_scenario_condition", (True, "fail", "", 0.0)), ("log_event", None),
                            ("recorder_start", None), ("recorder_note", None), ("recorder_finish", None)):
            mock = patch.object(netem, name, return_value=value)
            self.mocks[name] = mock.start()
            self.addCleanup(mock.stop)
        netem.SCENARIO_STOP.clear()
        self.addCleanup(netem.reset_scenario_state, None, None)
        with netem.RUNTIME_LOCK:
            netem.SCENARIO_STATE.update(active=True, scenario_name=SCENARIO["name"], link_id="wan1",
                                        started_at=time.time(), clock_start=time.monotonic(), step_label="Starting")

    def start(self, intro_s):
        thread = threading.Thread(target=netem.run_scenario, args=("wan1", SCENARIO, intro_s), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        wait_until(lambda: netem.SCENARIO_STATE.get("intro"))
        return thread

    def test_announcement_comes_first_and_the_test_clock_starts_after_it(self):
        thread = self.start(0.6)
        snapshot = netem.scenario_snapshot()
        self.assertTrue(snapshot["intro"])
        self.assertTrue(0 < snapshot["starts_in_s"] <= 0.6)
        self.assertEqual((snapshot["elapsed_s"], snapshot["phase_index"], snapshot["description"], snapshot["checks"]),
                         (0.0, None, "WAN1 fails and recovers.", ["SLA detects failure"]))
        self.assertEqual([phase["name"] for phase in snapshot["phases"]], ["Baseline", "Outage", "Recovery"])
        self.mocks["apply_selected_profile"].assert_not_called()
        self.mocks["recorder_start"].assert_not_called()
        wait_until(lambda: not netem.SCENARIO_STATE["intro"])
        self.assertLess(netem.scenario_snapshot().get("elapsed_s", 0), 0.5)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(netem.SCENARIO_STATE["result"], "passed")
        self.mocks["recorder_start"].assert_called_once()
        self.mocks["apply_runtime_fault"].assert_called()
        self.assertEqual((netem.SCENARIO_STATE["intro"], netem.SCENARIO_STATE["checks"]), (False, []))

    def test_stopping_during_the_announcement_changes_nothing(self):
        thread = self.start(30)
        netem.SCENARIO_STOP.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual((netem.SCENARIO_STATE["active"], netem.SCENARIO_STATE["result"]), (False, "stopped"))
        for name in ("apply_selected_profile", "apply_runtime_fault", "recorder_start", "recorder_finish"):
            self.mocks[name].assert_not_called()
        self.assertFalse(netem.SCENARIO_STOP.is_set())

    def test_pause_waits_until_the_test_runs(self):
        with netem.RUNTIME_LOCK:
            netem.SCENARIO_STATE.update(intro=True, intro_clock=time.monotonic() + 5)
        netem.app.test_client().post("/lab/scenario/pause")
        self.assertFalse(netem.SCENARIO_PAUSE.is_set())
        self.assertFalse(netem.SCENARIO_STATE["paused"])

    def test_every_start_path_announces_its_test(self):
        tests = [{"id": "outage", "role": "primary", "name": "Primary WAN outage", "description": "The primary fails.",
                  "steps": [{"after": 0, "action": "phase", "label": "Measuring", "phase": "Baseline"}]}]
        with netem.RUNTIME_LOCK:
            netem.SITE_PLAN_STATE.update(active=True, tests=[{"id": "outage", "name": "Primary WAN outage", "role": "primary",
                                                              "status": "pending", "link_id": None, "error": None}])
        with patch.object(netem, "run_scenario") as run:
            netem.run_site_plan({"label": "Plant"}, tests, {"primary": "wan1"})
        link_id, scenario, intro_s = run.call_args.args
        self.assertEqual((link_id, scenario["description"], intro_s), ("wan1", "The primary fails.", netem.TEST_INTRO_S))
        netem.reset_scenario_state(None, None)
        with patch.object(netem, "get_scenarios", return_value=[SCENARIO]), \
             patch.object(netem.threading, "Thread") as thread:
            netem.app.test_client().post("/lab/scenario/start", data={"link_id": "wan1", "scenario_id": "demo"})
        self.assertEqual(thread.call_args.kwargs["args"][2], netem.TEST_INTRO_S)
        self.assertEqual(netem.TEST_INTRO_S, 5)


if __name__ == "__main__":
    unittest.main()
