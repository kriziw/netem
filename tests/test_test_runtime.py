"""Test phases, length, pause, recorded summaries, session sites and the showroom address."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem

SELECTION = dict(industry="manufacturing", sub_industry="automotive", function="plant",
                 size="large", criticality="business_critical")
STEPS = [
    {"after": 0, "action": "quality", "value": 100, "label": "Nominal", "phase": "Baseline"},
    {"after": 60, "action": "quality", "value": 40, "label": "Degrade", "phase": "Brownout"},
    {"after": 0, "action": "assert", "label": "Experience", "timeout": 30, "phase": "Brownout",
     "condition": {"type": "dem", "field": "experience_score", "op": ">=", "value": 80, "window": 60}},
    {"after": 90, "action": "quality", "value": 100, "label": "Restore", "phase": "Recovery"},
    {"after": 30, "action": "phase", "label": "Settled", "phase": "Recovery"},
]


class PhaseTests(unittest.TestCase):
    def test_phases_follow_steps_and_every_built_in_test_runs_three_to_five_minutes(self):
        phases, total = netem.scenario_phases(STEPS)
        self.assertEqual([(p["name"], p["start_s"], p["planned_s"]) for p in phases],
                         [("Baseline", 0, 60), ("Brownout", 60, 90), ("Recovery", 150, 30)])
        self.assertEqual(total, 180)
        self.assertEqual(netem.step_phase_indexes(STEPS), [0, 1, 1, 2, 2])
        for scenario in netem.DEFAULT_SCENARIOS:
            with self.subTest(scenario=scenario["id"]):
                phases, total = netem.scenario_phases(netem.validate_scenario_steps(scenario["steps"]))
                self.assertTrue(180 <= total <= 300, total)
                self.assertTrue(all(phase["planned_s"] > 0 for phase in phases))

    def test_length_scales_delays_and_windows_but_not_timeouts(self):
        scaled = netem.scale_scenario_steps(STEPS, 360)
        self.assertEqual([step["after"] for step in scaled], [0, 120, 0, 180, 60])
        self.assertEqual(scaled[2]["condition"]["window"], 120)
        self.assertEqual(scaled[2]["timeout"], 30)
        self.assertEqual(STEPS[1]["after"], 60)
        self.assertEqual(netem.scenario_phases(netem.scale_scenario_steps(STEPS, 60 * 60))[1], 720)
        self.assertEqual(netem.requested_length_s(""), None)
        self.assertEqual(netem.requested_length_s("4.5"), 270)
        for raw in ("0", "61", "nan", "x"):
            with self.assertRaises(ValueError):
                netem.requested_length_s(raw)

    def test_pause_holds_the_sleep_and_stop_still_ends_it(self):
        netem.SCENARIO_PAUSE.set()
        self.addCleanup(netem.SCENARIO_PAUSE.clear)
        self.addCleanup(netem.SCENARIO_STOP.clear)
        timer = threading.Timer(0.4, netem.SCENARIO_PAUSE.clear)
        timer.start()
        started = time.monotonic()
        self.assertFalse(netem.scenario_sleep(0.1))
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        netem.SCENARIO_PAUSE.set()
        threading.Timer(0.2, netem.SCENARIO_STOP.set).start()
        self.assertTrue(netem.scenario_sleep(5))

    def test_snapshot_counts_down_to_the_next_phase_from_its_real_start(self):
        phases = [{"name": "Baseline", "planned_s": 60}, {"name": "Brownout", "planned_s": 90}, {"name": "Recovery", "planned_s": 30}]
        state = dict(netem.SCENARIO_STATE, active=True, started_at=1000.0, phases=phases, phase_index=1,
                     phase="Brownout", phase_started_s=70.0, paused=True, paused_at=1095.0, paused_total_s=5.0)
        with patch.object(netem, "SCENARIO_STATE", state), patch.object(netem.time, "time", return_value=1100.0):
            snapshot = netem.scenario_snapshot()
        # 100 s since the start, 5 s paused earlier and 5 s in the current pause.
        self.assertEqual((snapshot["elapsed_s"], snapshot["phase_elapsed_s"], snapshot["phase_remaining_s"]), (90.0, 20.0, 70.0))
        self.assertEqual(snapshot["next_phase"], "Recovery")


class PauseRouteTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(netem.SCENARIO_STATE, active=True, scenario_name="Brownout", phase="Brownout")
        for target, value in (("SCENARIO_STATE", self.state), ("log_event", None)):
            mock = patch.object(netem, target, value) if value is not None else patch.object(netem, target)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(netem.SCENARIO_PAUSE.clear)
        self.client = netem.app.test_client()

    def test_pause_and_resume_account_for_held_time(self):
        with patch.object(netem.time, "time", return_value=500.0):
            self.client.post("/lab/scenario/pause", data={"return_to": "tests"})
        self.assertTrue(self.state["paused"])
        self.assertTrue(netem.SCENARIO_PAUSE.is_set())
        with patch.object(netem.time, "time", return_value=530.0):
            self.client.post("/lab/scenario/resume", data={"return_to": "overview"})
        self.assertFalse(self.state["paused"])
        self.assertFalse(netem.SCENARIO_PAUSE.is_set())
        self.assertEqual(self.state["paused_total_s"], 30.0)
        self.state["active"] = False
        self.client.post("/lab/scenario/pause")
        self.assertFalse(netem.SCENARIO_PAUSE.is_set())


class SummaryTests(unittest.TestCase):
    def record(self):
        def sample(phase, experience, success, down, steering):
            return {"phase": phase, "experience": experience, "success": success, "interactive_p95_ms": 100,
                    "links": [{"id": "wan1", "label": "WAN1", "health": "healthy" if phase == "Baseline" else "degraded",
                               "down_mbps": down, "up_mbps": 10}],
                    "steering": {"Voice & video": {"verdict": steering, "impaired": []}}}
        return {"name": "Brownout", "scenario_id": "b", "link_id": "wan1", "started_at": 1000.0,
                "phases": ["Baseline", "Brownout", "Recovery"],
                "phase_marks": [{"name": "Baseline", "at": 1000.0}, {"name": "Brownout", "at": 1060.0}],
                "samples": [sample("Baseline", 95, 100.0, 50, "balanced"), sample("Brownout", 70, 99.0, 30, "stuck"),
                            sample("Brownout", 60, 97.5, 10, "steered")],
                "assertions": [{"label": "Experience ≥ 80", "passed": False, "observed": 60, "phase": "Brownout"}],
                "reactions": [{"traffic_class": "Voice & video", "wan": "WAN1", "health": "degraded", "seconds": 12}]}

    def test_summary_reports_each_phase_remediation_and_checks(self):
        summary = netem.summarize_test(self.record(), "failed", None, {"steering_max_s": 10}, "Automotive plant", ended_at=1150.0)
        baseline, brownout, recovery = summary["phases"]
        self.assertEqual((baseline["duration_s"], brownout["duration_s"]), (60, 90))
        self.assertEqual((brownout["experience_score"], brownout["success_pct"], brownout["worst_success_pct"]), (60, 97.5, 97.5))
        self.assertEqual(brownout["wans"], [{"label": "WAN1", "down_mbps": 20.0, "up_mbps": 10.0, "health": "degraded"}])
        self.assertEqual(brownout["steering"], {"Voice & video": "steered"})
        self.assertFalse(recovery["reached"])
        self.assertEqual(summary["remediation"][0]["within_target"], False)
        self.assertEqual(summary["assertions"]["passed"], 0)
        self.assertIn("fell to 60 during Brownout", " ".join(summary["conclusion"]))
        moved = [line for line in summary["conclusion"] if "moved voice & video off WAN1 in 12 s" in line]
        self.assertEqual(len(moved), 1)
        self.assertIn("target ≤ 10 s missed", moved[0])
        self.assertNotIn(moved[0], summary["narrative"])
        self.assertTrue(summary["narrative"][-1].startswith("Missed: Experience ≥ 80"))

    def test_without_measurements_the_summary_says_so(self):
        record = dict(self.record(), samples=[], reactions=[], assertions=[])
        summary = netem.summarize_test(record, "passed", None, ended_at=1100.0)
        self.assertIn("No simulated user traffic was measured", summary["conclusion"][1])
        self.assertIsNone(summary["steering_target_s"])

    def test_recorder_keeps_the_last_summary(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        payload = {"traffic_generator": {"status": {"dem": {"requests": 10, "experience_score": 88, "availability_pct": 99.5,
                                                            "interactive_p95_ms": 120}}},
                   "links": [{"link_id": "wan1", "label": "WAN1", "health": "healthy",
                              "directions": {"down": {"rate_mbps": 12.5}, "up": {"rate_mbps": 2.0}}}],
                   "steering": {"classes": [{"label": "Voice & video", "verdict": "balanced", "shares": []}]}}
        with patch.object(netem, "LAST_TEST_SUMMARY_PATH", Path(temp.name) / "summary.json"), \
             patch.object(netem, "RUNTIME_DIR", Path(temp.name)), \
             patch.object(netem, "LAST_TEST_SUMMARY", None), \
             patch.object(netem, "scenario_snapshot", return_value={"active": True, "elapsed_s": 5, "phase": "Baseline"}), \
             patch.object(netem, "load_config", return_value={"site_profile": SELECTION}), \
             patch.object(netem, "get_link", return_value={"name": "WAN1"}), \
             patch.object(netem, "log_event") as log:
            netem.recorder_start({"id": "b", "name": "Brownout"}, "wan1", [{"name": "Baseline"}])
            netem.recorder_note("phase_marks", {"name": "Baseline", "at": time.time()})
            netem.record_test_sample(payload)
            summary = netem.recorder_finish("passed", None)
            self.assertIs(netem.LAST_TEST_SUMMARY, summary)
            self.assertEqual(json.loads((Path(temp.name) / "summary.json").read_text())["name"], "Brownout")
        self.assertEqual(summary["link"], "WAN1")
        self.assertEqual(summary["site"], "Automotive · Manufacturing plant · Large · Business-critical")
        self.assertEqual(summary["phases"][0]["experience_score"], 88)
        self.assertEqual(summary["phases"][0]["wans"][0]["down_mbps"], 12.5)
        self.assertEqual(log.call_args.args[0], "test-summary")
        self.assertIsNone(netem.recorder_finish("passed", None))


class SessionSiteTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"wan_links": []}
        self.session = dict(active=False, id=None, name=None, started_at=None, site=None)
        for target, kwargs in (("load_config", {"side_effect": lambda: copy.deepcopy(self.cfg)}),
                               ("save_config", {"side_effect": lambda cfg: self.cfg.update(cfg)}),
                               ("save_session_history", {}), ("log_event", {}),
                               ("ACTIVE_SESSION", {"new": self.session}), ("LAB_SESSIONS", {"new": []})):
            mock = patch.object(netem, target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)
        self.client = netem.app.test_client()

    def test_session_requires_and_records_the_client_site(self):
        self.client.post("/sessions/start", data={"name": "PoC"})
        self.assertFalse(self.session["active"])
        self.client.post("/sessions/start", data=dict(SELECTION, name="PoC", size="huge"))
        self.assertFalse(self.session["active"])
        self.client.post("/sessions/start", data=dict(SELECTION, name="PoC"))
        self.assertTrue(self.session["active"])
        self.assertEqual(self.session["site"]["label"], "Automotive · Manufacturing plant · Large · Business-critical")
        self.assertEqual(self.cfg["site_profile"], SELECTION)
        self.assertEqual(netem.LAB_SESSIONS[0]["site"]["industry"], "manufacturing")

    def test_session_without_fields_uses_the_active_site(self):
        self.cfg["site_profile"] = dict(SELECTION, criticality="standard")
        self.client.post("/sessions/start", data={"name": "PoC"})
        self.assertEqual(self.session["site"]["criticality"], "standard")


class ShowroomAddressTests(unittest.TestCase):
    def url(self, base, **env):
        with patch.dict(os.environ):
            for name in ("NETEM_SHOWROOM_HOST", "NETEM_SHOWROOM_PORT"):
                os.environ.pop(name, None)
            os.environ.update(env)
            with netem.app.test_request_context(base_url=base):
                return netem.showroom_url()

    def test_address_follows_the_operator_host_unless_bound_to_one_address(self):
        self.assertEqual(self.url("http://10.1.2.3:8081"), "http://10.1.2.3:8082/")
        self.assertEqual(self.url("http://[2001:db8::1]:8081"), "http://[2001:db8::1]:8082/")
        self.assertEqual(self.url("http://netem.lab:8081", NETEM_SHOWROOM_HOST="192.0.2.5", NETEM_SHOWROOM_PORT="9000"),
                         "http://192.0.2.5:9000/")
        self.assertIsNone(self.url("http://10.1.2.3:8081", NETEM_SHOWROOM_PORT="0"))
        self.assertIsNone(self.url("http://10.1.2.3:8081", NETEM_SHOWROOM_PORT="8081"))


if __name__ == "__main__":
    unittest.main()
