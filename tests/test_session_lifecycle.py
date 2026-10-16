"""A lab session captures its details when it starts and ends by itself after six hours. Until one starts, the
showcase publishes nothing about the client, the appliance or the lab, and each site test is started separately."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem
import branding

SITE = dict(industry="manufacturing", sub_industry="automotive", function="plant", size="large", criticality="business_critical")
CONFIG = {"wan_links": [{"id": "wan1", "name": "WAN1 DIA"}, {"id": "wan2", "name": "WAN2 broadband"}], "site_profile": SITE}
APPLIANCE = {"vendor": "fortinet", "vendor_name": "Fortinet", "product": "FortiGate", "model": "FortiGate-VM04",
             "name": "Lab FortiGate", "source": "simulator"}


def blank_session():
    return {"active": False, "id": None, "name": None, "started_at": None, "site": None, "deadline": None,
            "appliance": None, "wan": None, "tests": {}}


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(CONFIG)
        self.session = blank_session()
        self.history = []
        patches = [
            patch.object(netem, "ACTIVE_SESSION", self.session),
            patch.object(netem, "LAB_SESSIONS", self.history),
            patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)),
            patch.object(netem, "save_config", side_effect=lambda cfg: self.cfg.update(cfg)),
            patch.object(netem, "save_session_history"),
            patch.object(netem, "log_event"),
            patch.object(netem, "build_session_report", return_value={"result": "passed"}),
            patch.object(netem, "appliance_under_test", return_value=dict(APPLIANCE)),
            patch.object(netem, "SITE_PLAN_STATE", dict(active=False, tests=[])),
            patch.object(netem, "SCENARIO_STATE", dict(netem.SCENARIO_STATE, active=False)),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        netem.SITE_PLAN_STOP.clear()
        netem.SCENARIO_STOP.clear()
        self.addCleanup(netem.SITE_PLAN_STOP.clear)
        self.addCleanup(netem.SCENARIO_STOP.clear)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def start(self, **extra):
        data = dict(SITE, name="Contoso PoC", primary_link="wan1", backup_link="wan2", integration_csrf="token")
        data.update(extra)
        return self.client.post("/sessions/start", data=data)


class StartTests(SessionCase):
    def test_start_captures_the_client_appliance_wans_and_the_six_hour_limit(self):
        before = time.time()
        self.start()
        self.assertTrue(self.session["active"])
        self.assertEqual(self.session["name"], "Contoso PoC")
        self.assertEqual(self.session["appliance"]["vendor_name"], "Fortinet")
        self.assertEqual(self.session["appliance"]["model"], "FortiGate-VM04")
        self.assertEqual(self.session["wan"]["primary"], {"link_id": "wan1", "label": "WAN1 DIA"})
        self.assertEqual(self.session["wan"]["backup"]["link_id"], "wan2")
        self.assertGreaterEqual(self.session["started_at"], before)
        self.assertEqual(self.session["deadline"] - self.session["started_at"], 6 * 3600)
        self.assertEqual(self.history[0]["status"], "active")
        self.assertEqual(self.history[0]["appliance"]["model"], "FortiGate-VM04")

    def test_session_admission_rechecks_if_another_start_wins_during_preparation(self):
        def load_after_competing_start():
            self.session.update(active=True, id='competing-session', name='Other session')
            return copy.deepcopy(self.cfg)
        with patch.object(netem, 'load_config', side_effect=load_after_competing_start), \
                patch.object(netem, 'save_session_history') as persist:
            self.assertEqual(self.start().status_code, 302)
            self.assertEqual(self.session['id'], 'competing-session')
            self.assertEqual(self.session['name'], 'Other session')
            self.assertEqual(self.history, [])
            persist.assert_not_called()

    def test_the_captured_appliance_does_not_follow_later_selection_changes(self):
        self.start()
        with patch.object(netem, "appliance_under_test", return_value={"vendor_name": "Cisco", "model": "Other"}):
            snapshot = netem.session_snapshot()
        self.assertEqual(snapshot["appliance"]["vendor_name"], "Fortinet")

    def test_wan_roles_default_to_the_first_two_wans_and_must_be_different_and_known(self):
        self.assertEqual(netem.session_wan_roles(self.cfg)["backup"]["link_id"], "wan2")
        with self.assertRaises(ValueError):
            netem.session_wan_roles(self.cfg, "wan1", "wan1")
        with self.assertRaises(ValueError):
            netem.session_wan_roles(self.cfg, "missing", "wan2")

    def test_a_second_session_is_refused_while_one_runs(self):
        self.start()
        first = self.session["id"]
        self.start(name="Another client")
        self.assertEqual(self.session["id"], first)

    def test_start_without_a_site_is_refused(self):
        self.cfg.pop("site_profile")
        self.client.post("/sessions/start", data={"name": "PoC", "integration_csrf": "token"})
        self.assertFalse(self.session["active"])

    def test_a_lab_with_one_wan_starts_a_session_without_site_test_roles(self):
        # The session still records the client and the appliance; its site tests say why they cannot run.
        self.cfg["wan_links"] = self.cfg["wan_links"][:1]
        self.start()
        self.assertTrue(self.session["active"])
        self.assertIsNone(self.session["wan"])
        self.client.post("/site/run", data={"integration_csrf": "token", "test_id": "baseline",
                                            "primary_link": "wan1", "backup_link": "wan2"})
        self.assertFalse(netem.SITE_PLAN_STATE.get("active"))


class EndingTests(SessionCase):
    def test_the_operator_completes_the_session_and_a_running_plan_stops(self):
        self.start()
        netem.SITE_PLAN_STATE.update(active=True)
        self.client.post("/sessions/stop", data={"integration_csrf": "token", "return_to": "overview"})
        self.assertFalse(self.session["active"])
        self.assertTrue(netem.SITE_PLAN_STOP.is_set())
        self.assertEqual(self.history[0]["status"], "completed")
        self.assertEqual(self.history[0]["report"], {"result": "passed"})
        self.assertIsNotNone(self.history[0]["ended_at"])

    def test_six_hours_end_the_session_by_itself(self):
        self.start()
        deadline = self.session["deadline"]
        self.assertFalse(netem.expire_session_if_due(deadline - 1))
        self.assertTrue(self.session["active"])
        self.assertTrue(netem.expire_session_if_due(deadline))
        self.assertFalse(self.session["active"])
        self.assertEqual(self.history[0]["status"], "timed_out")
        self.assertFalse(netem.expire_session_if_due(deadline + 10))

    def test_the_time_limit_stops_a_running_test(self):
        self.start()
        netem.SCENARIO_STATE.update(active=True)
        netem.expire_session_if_due(self.session["deadline"])
        self.assertTrue(netem.SCENARIO_STOP.is_set())


class ShowcaseGatingTests(SessionCase):
    def test_before_a_session_the_showcase_publishes_nothing_about_the_lab(self):
        netem.SCENARIO_STATE.update(active=True, scenario_name="Brownout", link_id="wan1")
        with patch.object(netem, "build_link_states", side_effect=AssertionError("links read")), \
             patch.object(netem, "appliance_under_test", side_effect=AssertionError("appliance read")):
            snapshot = netem.showroom_snapshot()
        self.assertEqual(snapshot["links"], [])
        self.assertEqual(snapshot["appliance"], {})
        self.assertIsNone(snapshot["site"])
        self.assertFalse(snapshot["session"]["active"])
        self.assertIsNone(snapshot["session"]["name"])
        self.assertFalse(snapshot["scenario"]["active"])
        self.assertEqual(snapshot["plan"]["tests"], [])
        self.assertIsNone(snapshot["plan_summary"])
        self.assertIsNone(snapshot["last_test"])
        text = str(snapshot)
        for hidden in ("Automotive", "Fortinet", "WAN1", "Brownout", "Contoso", "Stuttgart"):
            self.assertNotIn(hidden, text)

    def test_during_a_session_the_showcase_shows_what_the_session_captured(self):
        self.start()
        with patch.object(netem, "build_link_states", return_value=[]), \
             patch.object(netem, "appliance_under_test", return_value={"vendor_name": "Cisco", "model": "Other"}), \
             patch.object(netem, "current_diagnosis", return_value={}):
            snapshot = netem.showroom_snapshot()
        self.assertTrue(snapshot["session"]["active"])
        self.assertEqual(snapshot["session"]["name"], "Contoso PoC")
        self.assertEqual(snapshot["session"]["wan"]["primary"], "WAN1 DIA")
        self.assertEqual(snapshot["appliance"]["vendor_name"], "Fortinet")
        self.assertEqual(snapshot["site"]["sub_industry"], "Automotive")

    def test_a_finished_session_leaves_the_showcase_waiting_again(self):
        self.start()
        self.client.post("/sessions/stop", data={"integration_csrf": "token"})
        with patch.object(netem, "build_link_states", side_effect=AssertionError("links read")):
            snapshot = netem.showroom_snapshot()
        self.assertFalse(snapshot["session"]["active"])
        self.assertEqual(snapshot["appliance"], {})


class SiteTestControlTests(SessionCase):
    def test_each_site_test_result_is_recorded_for_the_session(self):
        self.start()
        test = {"id": "baseline", "name": "Baseline experience", "role": "primary"}
        netem.set_session_test(test, "running")
        self.assertEqual(self.session["tests"]["baseline"]["status"], "running")
        netem.set_session_test(test, "passed", {"result": "passed", "phases": []})
        self.assertEqual(self.session["tests"]["baseline"]["summary"], {"result": "passed", "phases": []})
        self.assertEqual(self.history[0]["tests"]["baseline"]["status"], "passed")
        self.assertNotIn("summary", self.history[0]["tests"]["baseline"])
        statuses = {row["id"]: row["status"] for row in netem.session_site_tests()}
        self.assertEqual(statuses["baseline"], "passed")
        self.assertEqual(statuses["primary_outage"], "pending")

    def test_site_tests_cannot_start_outside_a_session(self):
        self.client.post("/site/run", data={"integration_csrf": "token", "test_id": "baseline",
                                            "primary_link": "wan1", "backup_link": "wan2"})
        self.assertFalse(netem.SITE_PLAN_STATE.get("active"))

    def test_command_centre_offers_a_start_button_per_test_only_when_a_session_runs(self):
        template = netem.app.jinja_env.get_template("_session_console.html")
        with netem.app.test_request_context():
            idle = template.render(access_can=lambda *args: True, session_state=blank_session(), session_tests=[], session_wan={},
                                   traffic_generator={"connected": True}, scenario_state={"active": False},
                                   site_state={"active": False}, integration_csrf="token")
            tests = [{"id": "baseline", "name": "Baseline", "role": "primary", "description": "", "status": "pending"},
                     {"id": "primary_outage", "name": "Outage", "role": "primary", "description": "", "status": "passed"}]
            session = dict(blank_session(), active=True, name="Contoso PoC", site={"label": "Plant"},
                           deadline=time.time() + 3600)
            wan = {"primary": {"link_id": "wan1", "label": "WAN1"}, "backup": {"link_id": "wan2", "label": "WAN2"}}
            running = template.render(access_can=lambda *args: True, session_state=session, session_tests=tests, session_wan=wan,
                                      traffic_generator={"connected": True}, scenario_state={"active": False},
                                      site_state={"active": False}, integration_csrf="token")
            busy = template.render(access_can=lambda *args: True, session_state=session, session_tests=tests, session_wan=wan,
                                   traffic_generator={"connected": True}, scenario_state={"active": True},
                                   site_state={"active": True}, integration_csrf="token")
        self.assertIn("No session", idle)
        self.assertNotIn("Start test", idle)
        self.assertEqual(running.count(">Start test</button>"), 2)
        self.assertEqual(running.count("disabled>Start test"), 0)
        self.assertIn('name="primary_link" value="wan1"', running)
        self.assertIn("Contoso PoC", running)
        # While a test runs (or the simulator is offline) every start button is disabled.
        self.assertEqual(busy.count("disabled>Start test"), 2)

    def test_the_sessions_page_asks_for_the_wan_roles_and_shows_the_appliance(self):
        before = self.client.get("/sessions").get_data(as_text=True)
        self.assertIn('name="primary_link"', before)
        self.assertIn('name="backup_link"', before)
        self.start()
        during = self.client.get("/sessions").get_data(as_text=True)
        self.assertIn("captured at start", during)
        self.assertIn("six hours after it started", during)


class BrandWaitingAnimationTests(unittest.TestCase):
    def test_a_brand_can_supply_its_own_waiting_animation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            pack = Path(tmp) / "pack"
            (pack / "assets").mkdir(parents=True)
            (pack / "assets" / "waiting.webm").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 16)
            (pack / "branding.json").write_text(json.dumps({"name": "MSP", "waiting_video": "waiting.webm"}))
            store = branding.BrandStore(Path(tmp) / "brands", pack)
            _root, brand = store.current()
        self.assertEqual(brand.get("waiting_video"), "waiting.webm")


if __name__ == "__main__":
    unittest.main()
