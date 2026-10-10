"""The customer listener shares live data without exposing operator capabilities."""
import os
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import app as netem
from showroom import create_showroom_app


STATE = {
    "id": "wan1", "label": "Customer WAN", "preset_name": "DIA",
    "runtime_quality": 90, "fault": "normal", "sla": {"pass": True},
    "effective": {"delay_ms": 10, "jitter_ms": 2, "loss_pct": 0,
                  "download_mbit": 100, "upload_mbit": 20},
    "appliance_addresses": ["192.0.2.1"], "inner": "private-interface",
}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("load_config", {"integration_token": "secret-token"}),
            ("build_link_states", [STATE]),
            ("scenario_snapshot", {"active": True, "scenario_name": "Brownout",
                                   "step": 1, "step_count": 2, "step_label": "Degrade",
                                   "error": "sensitive diagnostic"}),
            ("session_snapshot", {"active": True, "name": "Showcase", "id": "internal"}),
        ):
            mock = patch.object(netem, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(netem.time, "time", return_value=100)
        mock.start()
        self.addCleanup(mock.stop)

    def snapshot(self, sample):
        with patch.object(netem, "latest_telemetry_sample", return_value=sample):
            return netem.showroom_snapshot()

    def test_live_and_idle_rates_and_allowlisted_data(self):
        result = self.snapshot({"timestamp": 99, "rate_valid": 1,
                                "down_mbps": 0, "up_mbps": 1.5})
        link = result["links"][0]
        self.assertTrue(link["traffic_available"])
        self.assertEqual((link["down_mbps"], link["up_mbps"]), (0, 1.5))
        self.assertEqual(result["scenario"]["scenario_name"], "Brownout")
        self.assertEqual(result["session"], {"active": True, "name": "Showcase", "site": None})
        self.assertIsNone(result["last_test"])
        for secret in ("secret-token", "private-interface", "192.0.2.1", "sensitive diagnostic", "internal"):
            self.assertNotIn(secret, str(result))

    def test_site_session_and_diagnosis_texts_never_publish_addresses(self):
        site = dict(industry="manufacturing", sub_industry="automotive", function="plant", size="large",
                    criticality="business_critical", label="Automotive plant")
        diagnosis = {
            "findings": [{"severity": "bad", "source": "experience", "title": "Voice fails via 198.51.100.7",
                          "detail": "Replies from 2001:db8::7 instead", "wans": [{"label": "WAN1", "affected": 3,
                                                                                   "causes": ["Queue full on 203.0.113.9"]}]},
                         {"severity": "warn", "source": "mapping", "title": "Map 192.0.2.44 to a WAN"}],
            "steering": {"classes": [{"label": "Voice & video", "verdict": "steered", "severity": "good",
                                      "text": "Moved from 198.51.100.7", "shares": [], "reactions": []}]},
        }
        with patch.object(netem, "session_snapshot", return_value={"active": True, "name": "PoC", "site": site}), \
             patch.object(netem, "current_diagnosis", return_value=diagnosis):
            result = self.snapshot(None)
        self.assertEqual(result["session"]["site"], "Automotive plant")
        self.assertEqual(result["site"]["sub_industry"], "Automotive")
        self.assertEqual(result["experience"]["targets"]["steering_max_s"], 30)
        self.assertEqual([item["title"] for item in result["findings"]], ["Voice fails via [address]"])
        for address in ("198.51.100.7", "2001:db8::7", "203.0.113.9", "192.0.2.44"):
            self.assertNotIn(address, str(result))

    def test_finished_test_report_is_published_only_between_tests(self):
        summary = {"name": "Brownout", "link": "WAN1", "result": "failed", "ended_at": 90, "duration_s": 240,
                   "link_id": "internal-link", "scenario_id": "internal-id",
                   "conclusion": ["Brownout failed.", "Moved off 192.0.2.1 in 4 s."],
                   "narrative": ["Brownout failed.", "Experience fell during Brownout."],
                   "phases": [{"name": "Brownout", "reached": True, "experience_score": 60, "secret": "x",
                               "wans": [{"label": "WAN1", "down_mbps": 4, "id": "internal-link"}]}],
                   "remediation": [{"traffic_class": "Voice & video", "wan": "WAN1", "seconds": 4, "within_target": True, "health": "x"}],
                   "assertions": {"passed": 1, "total": 2, "items": [{"label": "secret check"}]}}
        with patch.object(netem, "LAST_TEST_SUMMARY", summary):
            self.assertIsNone(self.snapshot(None)["last_test"])
            with patch.object(netem, "scenario_snapshot", return_value={"active": False}):
                report = self.snapshot(None)["last_test"]
        self.assertEqual(report["conclusion"], ["Brownout failed.", "Experience fell during Brownout."])
        self.assertEqual(report["assertions"], {"passed": 1, "total": 2})
        for hidden in ("internal-link", "internal-id", "secret"):
            self.assertNotIn(hidden, str(report))

    def test_missing_invalid_stale_and_future_measurements_are_unknown(self):
        for sample in (None, {"timestamp": 99, "rate_valid": 0},
                       {"timestamp": 93, "rate_valid": 1, "down_mbps": 5, "up_mbps": 2},
                       {"timestamp": 101, "rate_valid": 1, "down_mbps": 5, "up_mbps": 2},
                       {"timestamp": 99, "rate_valid": 1, "down_mbps": None, "up_mbps": 2},
                       {"timestamp": 99, "rate_valid": 1, "down_mbps": float("nan"), "up_mbps": 2}):
            with self.subTest(sample=sample):
                link = self.snapshot(sample)["links"][0]
                self.assertFalse(link["traffic_available"])
                self.assertIsNone(link["down_mbps"])
                self.assertIsNone(link["up_mbps"])


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"links": [], "timestamp": 1}
        self.viewer = create_showroom_app(lambda: self.state)
        self.client = self.viewer.test_client()

    def test_surface_is_only_presentation_routes(self):
        allowed = {"/", "/api/snapshot", "/assets/showroom.css", "/assets/showroom.js"}
        # Brand and vendor assets are the only other routes; without a brand they return 404.
        assets = {"/branding/theme.css", "/branding/assets/<path:filename>", "/assets/vendors/<vendor>.svg"}
        self.assertEqual({rule.rule for rule in self.viewer.url_map.iter_rules()}, allowed | assets)
        self.assertEqual(self.client.get("/branding/theme.css").status_code, 404)
        self.assertEqual(self.client.get("/assets/vendors/fortinet.svg").status_code, 200)
        for path in ("/assets/vendors/..%2Fapp.svg", "/assets/vendors/unknown.svg", "/assets/vendors/FORTINET.svg"):
            self.assertEqual(self.client.get(path).status_code, 404)
        for rule in netem.app.url_map.iter_rules():
            path = rule.rule.replace("<", "").replace(">", "")
            if path not in allowed and rule.rule not in assets:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
        for path in allowed:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
            response.close()
            with self.client.head(path) as response:
                self.assertEqual(response.status_code, 200)
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
                self.assertEqual(self.client.open(path, method=method).status_code, 405)
        self.assertEqual(self.client.get("/assets/../app.py").status_code, 404)
        self.assertEqual(self.client.get("/static/actions.js").status_code, 404)

    def test_same_provider_reflects_operator_changes_without_cookies(self):
        self.assertEqual(self.client.get("/api/snapshot").json["timestamp"], 1)
        self.state["timestamp"] = 2
        response = self.client.get("/api/snapshot")
        self.assertEqual(response.json["timestamp"], 2)
        self.assertNotIn("Set-Cookie", response.headers)
        html = self.client.get("/").text
        self.assertNotIn("<form", html)
        self.assertNotIn("actions.js", html)

    def test_startup_uses_same_process_and_closes_real_listener(self):
        from werkzeug.serving import make_server
        servers = []

        def bind(host, port, application, threaded):
            self.assertEqual((host, port, threaded), ("127.0.0.1", 8082, True))
            server = make_server(host, 0, self.viewer, threaded=threaded)
            servers.append(server)
            return server

        def operator(**kwargs):
            self.assertEqual(kwargs, {"host": "0.0.0.0", "port": 8081, "debug": False})
            address = f"http://127.0.0.1:{servers[0].server_port}/api/snapshot"
            with urlopen(address, timeout=3) as response:
                self.assertIn(b'"timestamp":1', response.read())
            self.state["timestamp"] = 2
            with urlopen(address, timeout=3) as response:
                self.assertIn(b'"timestamp":2', response.read())

        with patch.dict(os.environ, {"NETEM_SHOWROOM_HOST": "127.0.0.1", "NETEM_SHOWROOM_PORT": "8082"}), \
             patch("werkzeug.serving.make_server", side_effect=bind), \
             patch.object(netem, "restore_runtime_state") as restore, \
             patch.object(netem, "start_background_workers") as workers, \
             patch.object(netem.app, "run", side_effect=operator):
            netem.run_servers()
        restore.assert_called_once()
        workers.assert_called_once()
        self.assertEqual(servers[0].socket.fileno(), -1)

    def test_disabled_and_invalid_ports(self):
        with patch.object(netem, "restore_runtime_state"), \
             patch.object(netem, "start_background_workers"), \
             patch.object(netem.app, "run") as run, \
             patch("werkzeug.serving.make_server") as bind:
            with patch.dict(os.environ, {"NETEM_SHOWROOM_PORT": "0"}):
                netem.run_servers()
                bind.assert_not_called()
                run.assert_called_once()
            for port in ("8081", "-1", "65536", "invalid"):
                with patch.dict(os.environ, {"NETEM_SHOWROOM_PORT": port}):
                    with self.assertRaises(ValueError):
                        netem.run_servers()


if __name__ == "__main__":
    unittest.main()


class StoryTests(unittest.TestCase):
    """The showroom tells a stable story: changes show once they last, impacts linger as resolved."""

    def setUp(self):
        mock = patch.object(netem, "SHOWROOM_STATE", {"values": {}, "impacts": {}})
        mock.start()
        self.addCleanup(mock.stop)
        self.outcome = {"experience": {"available": True, "verdicts": {"experience": "pass", "success": "pass"}}, "steering": []}

    def links(self, health="healthy", fault="normal", quality=100):
        return [{"id": "wan1", "name": "WAN1", "fault": fault, "health": health, "sla_pass": True, "quality": quality,
                 "delay_ms": 45, "loss_pct": 2}, {"id": "wan2", "name": "WAN2", "fault": "normal", "health": "healthy",
                                                  "sla_pass": True, "quality": 100, "delay_ms": 15, "loss_pct": 0.1}]

    def diagnosis(self, failing=True, voice_wan="WAN2"):
        findings = [{"source": "experience", "id": "media_loss", "severity": "bad", "applications": ["video", "voice"],
                     "title": "Video meeting: 29 of 31 bursts lost packets",
                     "wans": [{"link_id": "wan1", "label": "WAN1", "affected": 29, "cause_kinds": ["injected_loss"]}]}] if failing else []
        findings.append({"source": "experience", "id": "bandwidth_bound", "severity": "warn", "applications": ["updates"], "wans": []})
        classes = [{"class": "realtime", "label": "Voice & video", "verdict": "steered",
                    "shares": [{"label": voice_wan, "health": "healthy", "pct": 90}, {"label": "WAN1", "health": "degraded", "pct": 10}]},
                   {"class": "bulk", "label": "File transfers", "verdict": "stuck",
                    "shares": [{"label": "WAN1", "health": "degraded", "pct": 100}]}]
        return {"findings": findings, "steering": {"classes": classes}}

    def test_words_for_impairments_and_causes(self):
        self.assertEqual(netem.impairment_text({"fault": "blackhole"}), "Outage injected")
        self.assertEqual(netem.impairment_text({"fault": "normal", "quality": 100}), "No impairment")
        self.assertEqual(netem.impairment_text({"fault": "normal", "quality": 60, "delay_ms": 45, "loss_pct": 2}),
                         "Impaired to 60% quality: 45 ms delay, 2% loss")
        link = {"name": "WAN1", "loss_pct": 2, "delay_ms": 45}
        self.assertEqual(netem.cause_text(["queue_drops"], link), "WAN1 is full")
        self.assertEqual(netem.cause_text(["injected_loss", "queue_drops"], link), "2% packet loss on WAN1")
        self.assertEqual(netem.cause_text(["fault"], link), "WAN1 is down")
        self.assertIsNone(netem.cause_text([], link))

    def test_impacts_settle_then_linger_as_resolved(self):
        links = self.links("degraded", quality=60)
        first = netem.showroom_story(self.diagnosis(), links, self.outcome, now=100)
        # A new impact is not shown until it has lasted a few seconds; bandwidth-bound transfers never are.
        self.assertEqual(first["impacts"], [])
        settled = netem.showroom_story(self.diagnosis(), self.links("degraded", quality=60), self.outcome, now=109)
        self.assertEqual(settled["impacts"], [{"impact": "Voice and video: breaking up", "cause": "2% packet loss on WAN1",
                                               "wan": "WAN1", "severity": "bad", "state": "active"}])
        self.assertTrue(settled["status"].startswith("Users are affected."))
        resolved = netem.showroom_story(self.diagnosis(failing=False), self.links(), self.outcome, now=115)
        self.assertEqual(resolved["impacts"][0]["state"], "resolved")
        gone = netem.showroom_story(self.diagnosis(failing=False), self.links(), self.outcome, now=150)
        self.assertEqual(gone["impacts"], [])
        # A blip shorter than the settle time never appears.
        netem.showroom_story(self.diagnosis(), self.links(), self.outcome, now=200)
        blip = netem.showroom_story(self.diagnosis(failing=False), self.links(), self.outcome, now=203)
        self.assertEqual(blip["impacts"], [])

    def test_health_routes_and_carried_traffic_settle(self):
        links = self.links("congested")
        story = netem.showroom_story(self.diagnosis(failing=False), links, self.outcome, now=10)
        self.assertEqual(links[0]["display_health"], "congested")
        self.assertEqual(story["routes"], [{"label": "Voice and video", "wan": "WAN2", "state": "good"},
                                           {"label": "File transfers", "wan": "WAN1", "state": "warn"}])
        self.assertEqual(links[0]["carries"], ["file transfers"])
        self.assertEqual(links[1]["carries"], ["voice and video"])
        # A brief flip in health or route does not show; one that lasts does.
        links = self.links("healthy")
        netem.showroom_story(self.diagnosis(failing=False, voice_wan="WAN1"), links, self.outcome, now=12)
        self.assertEqual(links[0]["display_health"], "congested")
        links = self.links("healthy")
        story = netem.showroom_story(self.diagnosis(failing=False, voice_wan="WAN1"), links, self.outcome, now=21)
        self.assertEqual(links[0]["display_health"], "healthy")
        self.assertEqual(story["routes"][0]["wan"], "WAN1")
        # An outage shows at once.
        links = self.links(fault="blackhole")
        netem.showroom_story(self.diagnosis(failing=False), links, self.outcome, now=22)
        self.assertEqual((links[0]["display_health"], links[0]["impairment"]), ("failed", "Outage injected"))

    def test_status_reads_as_one_sentence_per_question(self):
        outcome = dict(self.outcome, steering=[{"label": "Voice & video", "reactions": [{"label": "WAN1", "steered_after_seconds": 12}]}])
        story = netem.showroom_story({"findings": [], "steering": {"classes": []}}, self.links(), outcome, now=5)
        self.assertEqual(story["status"], "Users are fine. All WANs are healthy. The appliance moved voice & video off WAN1 in 12 s.")
        waiting = netem.showroom_story({}, self.links(), {"experience": {"available": False}}, now=50)
        self.assertTrue(waiting["status"].startswith("Waiting for simulated users."))
