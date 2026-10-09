"""Live bottleneck diagnosis: qdisc drops, per-WAN signals and symptom correlation."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem


TBF_OUTPUT = """qdisc netem 1: root refcnt 2 limit 1000 delay 15ms loss 0.1%
 Sent 1234567 bytes 9000 pkt (dropped 120, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
qdisc tbf 10: parent 1:1 rate 50Mbit burst 3200b lat 4.9ms
 Sent 1230000 bytes 8990 pkt (dropped 100, overlimits 5000 requeues 0)
 backlog 31Kb 21p requeues 0
"""
BLACKHOLE_OUTPUT = """qdisc netem 1: root refcnt 2 limit 1000 loss 100%
 Sent 0 bytes 0 pkt (dropped 42, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
"""


def counters(packets, netem_drops, queue_drops, kind="netem", shaped=True):
    return {"kind": kind, "packets": packets, "netem_drops": netem_drops, "queue_drops": queue_drops,
            "backlog_bytes": 1514, "shaped": shaped}


def state(link_id="wan2", label="WAN2", loss=0.1, delay=15.0, fault="normal", down=300, up=50):
    return {"id": link_id, "label": label, "fault": fault,
            "effective": {"delay_ms": delay, "jitter_ms": 0.0, "loss_pct": loss, "download_mbit": down, "upload_mbit": up}}


def sample(**values):
    base = dict(rate_valid=1, down_mbps=1.0, up_mbps=47.5, down_util_pct=0.3, up_util_pct=95.0,
                down_queue_drops_ps=0.0, up_queue_drops_ps=120.0, down_injected_drops_ps=0.4,
                up_injected_drops_ps=0.0, down_drop_pct=0.1, up_drop_pct=2.5, down_backlog_bytes=0, up_backlog_bytes=30000)
    base.update(values)
    return base


class QdiscStatsTests(unittest.TestCase):
    def test_parses_netem_and_rate_limiter_counters(self):
        netem_qdisc, tbf = netem.parse_qdisc_stats(TBF_OUTPUT)
        self.assertEqual((netem_qdisc["kind"], netem_qdisc["root"], netem_qdisc["drops"]), ("netem", True, 120))
        self.assertEqual((tbf["kind"], tbf["root"], tbf["drops"], tbf["overlimits"]), ("tbf", False, 100, 5000))
        self.assertEqual((tbf["backlog_bytes"], tbf["backlog_packets"]), (31 * 1024, 21))
        self.assertEqual(netem.parse_qdisc_stats(""), [])

    def test_counters_separate_queue_overflow_from_injected_loss(self):
        with patch.object(netem, "run_cmd", return_value=(0, TBF_OUTPUT, "")):
            value = netem.qdisc_counters("eth1")
        self.assertEqual(value, {"kind": "netem", "packets": 9000, "netem_drops": 120, "queue_drops": 100,
                                 "backlog_bytes": 31 * 1024, "shaped": True})
        with patch.object(netem, "run_cmd", return_value=(0, BLACKHOLE_OUTPUT, "")):
            self.assertFalse(netem.qdisc_counters("eth1")["shaped"])
        with patch.object(netem, "run_cmd", return_value=(1, "", "Cannot find device")):
            self.assertIsNone(netem.qdisc_counters("eth1"))

    def test_drop_rates_per_second_and_rebaseline_on_reset(self):
        rates = netem.qdisc_rates(counters(1900, 30, 20), counters(1000, 10, 0), 2.0)
        self.assertEqual(rates["queue_drops_ps"], 10.0)
        self.assertEqual(rates["injected_drops_ps"], 0.0)
        self.assertAlmostEqual(rates["drop_pct"], 20 * 100 / 920)
        injected = netem.qdisc_rates(counters(2000, 14, 0), counters(1000, 10, 0), 2.0)
        self.assertEqual(injected["injected_drops_ps"], 2.0)
        # A reapplied profile resets the counters: no rate instead of a negative one.
        self.assertIsNone(netem.qdisc_rates(counters(5, 0, 0), counters(1000, 10, 0), 2.0))
        self.assertIsNone(netem.qdisc_rates(counters(2000, 0, 0, kind="tbf"), counters(1000, 0, 0), 2.0))
        self.assertIsNone(netem.qdisc_rates(counters(2000, 0, 0), None, 2.0))


class TelemetryBottleneckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        link = {"id": "wan2", "inner": "inside", "outer": "outside"}
        self.patches = [patch.object(netem, "RUNTIME_DIR", root), patch.object(netem, "TELEMETRY_DB_PATH", root / "telemetry.db"),
                        patch.object(netem, "TELEMETRY_PREVIOUS", {}),
                        patch.object(netem, "load_config", return_value={"wan_links": [link]}),
                        patch.object(netem, "build_link_states", return_value=[dict(state(), runtime_quality=100, sla={"pass": True})])]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def collect(self, t, up_bytes, up_counters):
        snapshot = {"valid": True, "identity": (("inside", 1), ("outside", 2)), "timestamp": 1000 + t,
                    "monotonic_timestamp": t, "down_bytes": 0, "up_bytes": up_bytes, "down_packets": 0, "up_packets": 0}
        by_interface = {"inside": counters(0, 0, 0), "outside": up_counters}
        with patch.object(netem, "traffic_snapshot", return_value=snapshot), \
                patch.object(netem, "qdisc_counters", side_effect=lambda name: by_interface.get(name)):
            return netem.collect_telemetry_sample()

    def test_samples_store_utilization_and_drops_and_old_tables_migrate(self):
        netem.init_telemetry_db()
        with netem.telemetry_connect() as conn:
            conn.execute("ALTER TABLE telemetry_samples DROP COLUMN up_util_pct")
        netem.init_telemetry_db()
        self.collect(10, 0, counters(1000, 0, 0))
        rows = self.collect(12, 11_875_000, counters(1900, 240, 240))
        self.assertAlmostEqual(rows[0]["up_util_pct"], 95.0)
        self.assertEqual(rows[0]["up_queue_drops_ps"], 120.0)
        latest = netem.latest_telemetry_sample("wan2")
        self.assertAlmostEqual(latest["up_util_pct"], 95.0)
        self.assertEqual(netem.recent_telemetry_samples("missing"), [])
        self.assertEqual(len(netem.recent_telemetry_samples("wan2", seconds=10 ** 10)), 2)


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.learned = patch.object(netem, "EGRESS_LEARNED", {})
        self.learned.start()
        self.cfg = {"wan_links": [{"id": "wan1", "appliance_addresses": ["198.18.1.2"]},
                                  {"id": "wan2", "appliance_addresses": ["198.18.2.0/30"]}]}
        self.signals = [netem.path_signals(state("wan1", "WAN1", loss=0.0, delay=5.0, down=100, up=100),
                                           [sample(down_util_pct=91.0, up_util_pct=10.0, up_queue_drops_ps=0.0, down_injected_drops_ps=None)]),
                        netem.path_signals(state(), [sample()])]

    def tearDown(self):
        self.learned.stop()

    def test_path_signals_name_saturation_queue_drops_loss_and_delay(self):
        kinds = [(cause["kind"], cause["direction"]) for cause in self.signals[1]["causes"]]
        self.assertEqual(kinds, [("saturated", "up"), ("queue_drops", "up"), ("injected_loss", "down"), ("injected_delay", "down")])
        self.assertEqual(self.signals[1]["full"], ["up"])
        self.assertIn("95% of 50 Mbit/s", self.signals[1]["causes"][0]["text"])
        faulted = netem.path_signals(state(fault="blackhole"), [])
        self.assertEqual([cause["kind"] for cause in faulted["causes"]], ["fault"])

    def test_symptoms_are_attached_to_the_wan_that_carried_them(self):
        diagnosis = {"findings": [
            {"id": "media_loss", "severity": "bad", "title": "Video: 21 of 118 bursts lost packets", "detail": "",
             "by_egress": {"198.18.2.2": 18, "unknown": 3}},
            {"id": "bandwidth_bound", "severity": "warn", "title": "Downloads limited by bandwidth", "detail": "",
             "direction": "download", "by_egress": {"198.18.1.2": {"transfers": 6, "p95_ms": 4400}}},
            {"id": "slow_wait", "severity": "warn", "title": "Web waits", "detail": "", "by_egress": {"198.18.1.2": {"requests": 4, "p95_wait_ms": 900}}},
            {"id": "http_errors", "severity": "warn", "title": "HTTP 403", "detail": "", "by_egress": {"198.18.1.2": 2}},
        ]}
        findings = {item["id"]: item for item in netem.correlate_findings(diagnosis, self.signals, self.cfg)}
        media = findings["media_loss"]
        self.assertEqual([(wan["label"], wan["affected"]) for wan in media["wans"]], [("WAN2", 18)])
        self.assertTrue(any("random loss" in text for text in media["wans"][0]["causes"]))
        self.assertTrue(any("queue full" in text for text in media["wans"][0]["causes"]))
        self.assertEqual((media["unattributed"], media["candidates"]), (3, ["WAN2"]))
        bandwidth = findings["bandwidth_bound"]
        self.assertEqual(bandwidth["wans"][0]["label"], "WAN1")
        self.assertEqual(bandwidth["wans"][0]["causes"], ["Download at 91% of 100 Mbit/s"])
        # WAN1 adds 5 ms; a 900 ms wait is not NetEm's doing, so point at the appliance or target.
        self.assertEqual(findings["slow_wait"]["wans"][0]["causes"], [])
        self.assertIn("appliance", findings["slow_wait"]["hint"])
        delayed = netem.correlate_findings({"findings": [dict(diagnosis["findings"][2], by_egress={"198.18.1.2": {"requests": 4, "p95_wait_ms": 15}})]},
                                           self.signals, self.cfg)[0]
        self.assertIn("5 ms delay added to every round trip", delayed["wans"][0]["causes"])
        self.assertIn("not caused by network quality", findings["http_errors"]["hint"])
        # WAN2's saturated upload is explained by the media finding, so it is not repeated as path-only.
        self.assertNotIn("saturated", findings)

    def test_path_only_findings_without_a_simulator(self):
        findings = netem.correlate_findings(None, self.signals, self.cfg)
        self.assertTrue(all(item["source"] == "path" for item in findings))
        self.assertIn("WAN2: Upload at 95% of 50 Mbit/s", [item["title"] for item in findings])
        self.assertEqual(findings[0]["severity"], "warn")

    def test_egress_resolution_prefers_entered_addresses_and_flags_ambiguity(self):
        netem.EGRESS_LEARNED.update({"198.18.1.2": {"links": {"wan2"}, "seen_at": 1},
                                     "10.0.0.5": {"links": {"wan1", "wan2"}, "seen_at": 1},
                                     "198.18.9.9": {"links": {"wan2"}, "seen_at": 1}})
        self.assertEqual(netem.resolve_egress("198.18.1.2", self.cfg), {"link_id": "wan1", "source": "manual"})
        self.assertEqual(netem.resolve_egress("198.18.9.9", self.cfg)["link_id"], "wan2")
        self.assertEqual(netem.resolve_egress("10.0.0.5", self.cfg)["source"], "ambiguous")
        self.assertIsNone(netem.resolve_egress("not-an-ip", self.cfg))
        finding = netem.egress_mapping_finding({"10.0.0.5": netem.resolve_egress("10.0.0.5", self.cfg)}, {}, True)
        self.assertIn("not translating", finding["detail"])
        self.assertIn("tcpdump", netem.egress_mapping_finding({"203.0.113.9": None}, {}, False)["detail"])
        self.assertIsNone(netem.egress_mapping_finding({"198.18.1.2": {"link_id": "wan1"}}, {}, True))

    def test_wan_experience_from_simulator_egress(self):
        diagnosis = {"egress": {
            "198.18.2.2": {"requests": 100, "failures": 18, "applications": {"video": {"requests": 40, "failures": 18}, "web_saas": {"requests": 60, "failures": 0}}},
            "unknown": {"requests": 3, "failures": 3, "applications": {}},
        }}
        experience, unattributed = netem.wan_experience(diagnosis, self.signals, self.cfg)
        self.assertIsNone(experience["wan1"])
        self.assertEqual(experience["wan2"], {"requests": 100, "availability_pct": 82.0, "worst_app": "video", "worst_availability_pct": 55.0})
        self.assertEqual(unattributed, {"requests": 3, "failures": 3})

    def test_tcpdump_sources(self):
        output = ("12:00:00.000001 IP 198.18.2.2.40000 > 198.18.0.1.9000: UDP, length 1200\n"
                  "12:00:00.000002 IP6 2001:db8::2.443 > 2001:db8::1.8090: tcp 0\n"
                  "12:00:00.000003 IP 10.1.2.3 > 198.18.0.1: ICMP echo request\n")
        self.assertEqual(netem.parse_tcpdump_sources(output), {"198.18.2.2", "2001:db8::2"})


class EventTests(unittest.TestCase):
    def test_problems_are_logged_once_and_cleared_after_quiet_period(self):
        finding = {"source": "experience", "id": "media_loss", "severity": "bad", "title": "Video: 3 of 10 bursts lost packets",
                   "wans": [{"label": "WAN2", "affected": 3}]}
        with patch.object(netem, "DIAGNOSIS_ACTIVE", {}), patch.object(netem, "log_event") as log:
            netem.track_diagnosis_events([finding], now=100)
            netem.track_diagnosis_events([dict(finding, title="Video: 5 of 12 bursts lost packets")], now=103)
            netem.track_diagnosis_events([], now=110)
            netem.track_diagnosis_events([], now=140)
        messages = [call.args[1] for call in log.call_args_list]
        self.assertEqual(messages, ["Detected: Video: 3 of 10 bursts lost packets · WAN2", "Cleared: Video: 3 of 10 bursts lost packets"])
        self.assertEqual(log.call_args_list[1].kwargs["duration_seconds"], 3)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.patches = [patch.object(netem, "CONFIG_PATH", root / "config.json"), patch.object(netem, "RUNTIME_DIR", root),
                        patch.object(netem, "TELEMETRY_DB_PATH", root / "telemetry.db"), patch.object(netem, "EVENT_LOG_PATH", root / "events.jsonl"),
                        patch.object(netem, "DIAGNOSIS_CACHE", {"timestamp": 0.0, "payload": None}),
                        patch.object(netem, "DIAGNOSIS_ACTIVE", {}), patch.object(netem, "EGRESS_LEARNED", {})]
        for item in self.patches:
            item.start()
        netem.save_config({"wan_links": [{"id": "wan2", "name": "WAN2", "inner": "lo", "outer": "lo", "preset": "dia",
                                          "appliance_addresses": ["198.18.2.2"]}]})
        netem.init_telemetry_db()
        self.client = netem.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def diagnosis_with(self, status):
        snapshot = {"configured": True, "connected": status is not None, "status": status, "error": None}
        with patch.object(netem, "traffic_generator_snapshot", return_value=snapshot):
            netem.DIAGNOSIS_CACHE.update(timestamp=0.0, payload=None)
            return self.client.get("/api/v1/diagnosis").get_json()

    def test_diagnosis_api_with_old_and_new_simulators(self):
        old = self.diagnosis_with({"status": "running", "run": {}, "users": 5, "dem": {"p95_ms": 300}})
        self.assertFalse(old["simulator"]["diagnosis_available"])
        self.assertEqual(old["links"][0]["link_id"], "wan2")
        new = self.diagnosis_with({"status": "running", "run": {"target": "http://198.18.0.1:8090"}, "users": 5, "dem": {
            "interactive_p95_ms": 40, "diagnosis": {"media_mode": "realistic", "findings": [
                {"id": "media_no_reply", "severity": "bad", "title": "Voice: 4 of 4 bursts got no reply", "detail": "", "by_egress": {"198.18.2.2": 4}}],
                "egress": {"198.18.2.2": {"requests": 4, "failures": 4, "applications": {"voice": {"requests": 4, "failures": 4}}}}}}})
        self.assertEqual(new["simulator"], {"diagnosis_available": True, "media_mode": "realistic", "interactive_p95_ms": 40})
        self.assertEqual(new["findings"][0]["wans"][0]["label"], "WAN2")
        self.assertEqual(new["links"][0]["experience"]["availability_pct"], 0.0)
        self.assertEqual(new["egress"]["addresses"]["198.18.2.2"]["source"], "manual")

    def test_appliance_addresses_are_validated_and_saved(self):
        self.assertEqual(self.client.post("/wan/egress", data={"link_id": "wan2", "appliance_addresses": "198.18.2.2, 198.18.3.0/30"}).status_code, 302)
        self.assertEqual(netem.load_config()["wan_links"][0]["appliance_addresses"], ["198.18.2.2", "198.18.3.0/30"])
        self.client.post("/wan/egress", data={"link_id": "wan2", "appliance_addresses": "not-an-address"})
        self.assertEqual(netem.load_config()["wan_links"][0]["appliance_addresses"], ["198.18.2.2", "198.18.3.0/30"])
        self.client.post("/wan/egress", data={"link_id": "wan2", "appliance_addresses": ""})
        self.assertNotIn("appliance_addresses", netem.load_config()["wan_links"][0])

    def test_media_mode_is_forwarded_only_when_chosen(self):
        with self.client.session_transaction() as session:
            session["integration_csrf"] = "token"
        with patch.object(netem, "traffic_generator_request", return_value={"run": {"run_id": "r"}, "users": 1}) as api:
            self.client.post("/traffic-generator/start", data={"integration_csrf": "token", "media_mode": "realistic"})
            self.client.post("/traffic-generator/start", data={"integration_csrf": "token"})
            self.client.post("/traffic-generator/adjust", data={"integration_csrf": "token", "media_mode": "strict"})
        payloads = [call.kwargs["payload"] for call in api.call_args_list]
        self.assertEqual(payloads[0]["media_mode"], "realistic")
        self.assertNotIn("media_mode", payloads[1])
        self.assertEqual(payloads[2], {"media_mode": "strict"})


if __name__ == "__main__":
    unittest.main()


class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(netem, "STEERING_STATE", {"links": {}, "classes": {}, "baseline": {}}),
                        patch.object(netem, "EGRESS_LEARNED", {}), patch.object(netem, "log_event")]
        for item in self.patches:
            item.start()
        self.log = netem.log_event
        self.cfg = {"wan_links": [{"id": "wan1", "appliance_addresses": ["198.18.1.2"]},
                                  {"id": "wan2", "appliance_addresses": ["198.18.2.2"]}]}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def signals(self, wan2_sla=True):
        sla = lambda ok: {"pass": ok, "checks": {"latency": True, "jitter": True, "loss": ok, "data_plane": True}}
        return [netem.path_signals(dict(state("wan1", "WAN1", loss=0, delay=5), sla=sla(True)), []),
                netem.path_signals(dict(state(loss=5.0), sla=sla(wan2_sla)), [])]

    def dem(self, video_on, failures=0, wan2_p95=None):
        address = {"wan1": "198.18.1.2", "wan2": "198.18.2.2"}[video_on]
        egress = {address: {"applications": {"video": {"requests": 20, "failures": failures, "p95_ms": wan2_p95 or 40}}}}
        if video_on == "wan2":
            egress["198.18.1.2"] = {"applications": {"web_saas": {"requests": 30, "failures": 0, "p95_ms": 40},
                                                     "video": {"requests": 1, "failures": 0, "p95_ms": 40}}}
        return {"applications": {"video": {"class": "realtime"}, "web_saas": {"class": "interactive"}},
                "diagnosis": {"egress": egress, "egress_recent": {"window_seconds": 10, "egress": {
                    address: {"video": {"requests": 8, "failures": 0}},
                    "198.18.1.2" if video_on == "wan2" else "198.18.9.9": {"web_saas": {"requests": 6, "failures": 0}}}}}}

    def realtime(self, result):
        return next(item for item in result["classes"] if item["class"] == "realtime")

    def test_verdicts_follow_where_traffic_goes_and_what_users_feel(self):
        healthy = self.realtime(netem.assess_steering(self.dem("wan2"), self.signals(), self.cfg, now=90))
        self.assertEqual((healthy["verdict"], healthy["severity"]), ("balanced", "good"))
        stuck = self.realtime(netem.assess_steering(self.dem("wan2", failures=6), self.signals(False), self.cfg, now=100))
        self.assertEqual((stuck["verdict"], stuck["severity"]), ("stuck_impact", "bad"))
        self.assertIn("6 of 20 failed", stuck["text"])
        self.assertEqual(stuck["shares"][1]["health"], "degraded")
        self.assertIn("Model SLA fails on loss", stuck["shares"][1]["health_reason"])
        slow = self.realtime(netem.assess_steering(self.dem("wan2", wan2_p95=300), self.signals(False), self.cfg, now=101))
        self.assertIn("P95 300 ms there vs 40 ms on WAN1", slow["text"])
        quiet = self.realtime(netem.assess_steering(self.dem("wan2"), self.signals(False), self.cfg, now=102))
        self.assertEqual(quiet["verdict"], "stuck")
        self.assertEqual(netem.steering_findings({"classes": [stuck]})[0]["title"], "SD-WAN keeps voice & video on an impaired WAN")

    def test_reaction_time_is_measured_from_degradation_to_steering(self):
        netem.assess_steering(self.dem("wan2"), self.signals(), self.cfg, now=90)
        netem.assess_steering(self.dem("wan2"), self.signals(False), self.cfg, now=100)
        moved = self.realtime(netem.assess_steering(self.dem("wan1"), self.signals(False), self.cfg, now=114))
        self.assertEqual(moved["verdict"], "steered")
        self.assertEqual(moved["reactions"][0]["steered_after_seconds"], 14)
        self.assertEqual(self.log.call_args.args[1], "SD-WAN moved voice & video off WAN2 14 s after it became degraded")

    def test_slow_steering_is_flagged_once_and_unused_wans_are_not_timed(self):
        netem.assess_steering(self.dem("wan2"), self.signals(), self.cfg, now=90)
        for now in (100, 165, 170):
            netem.assess_steering(self.dem("wan2"), self.signals(False), self.cfg, now=now)
        messages = [call.args[1] for call in self.log.call_args_list]
        self.assertEqual(messages, ["SD-WAN still sends 100% of voice & video over degraded WAN2 after 65 s"])
        # Interactive traffic never used WAN2, so moving "off" it is not a steering reaction.
        result = netem.assess_steering(self.dem("wan2"), self.signals(False), self.cfg, now=171)
        interactive = next(item for item in result["classes"] if item["class"] == "interactive")
        self.assertFalse(interactive["reactions"][0]["was_used"])
        self.assertEqual(interactive["verdict"], "unaffected")

    def test_steering_needs_the_short_window_breakdown(self):
        self.assertIsNone(netem.assess_steering({"diagnosis": {"egress": {}}}, self.signals(), self.cfg))

    def test_unknown_source_is_traffic_not_idle(self):
        dem = {"diagnosis": {"egress_recent": {"window_seconds": 10, "egress": {
            "unknown": {"video": {"requests": 16}}}}}}
        item = self.realtime(netem.assess_steering(dem, self.signals(), self.cfg, now=100))
        self.assertEqual((item["verdict"], item["requests"], item["unattributed_pct"]), ("unattributed", 16, 100.0))
        self.assertIn("no target-reported source address", item["text"])
        self.assertIn("Steering cannot be verified", item["text"])
        self.assertEqual(item["reactions"], [])

        finding = netem.steering_findings({"classes": [item]})[0]
        self.assertIn("Cannot verify SD-WAN steering", finding["title"])
        self.assertEqual(finding["unattributed"], 16)
        self.assertEqual(finding["wans"], [])

    def test_unmapped_source_explains_mapping_instead_of_missing_replies(self):
        dem = {"diagnosis": {"egress_recent": {"egress": {
            "10.250.2.2": {"video": {"requests": 16}}}}}}
        item = self.realtime(netem.assess_steering(dem, self.signals(), self.cfg, now=100))
        self.assertEqual(item["verdict"], "unattributed")
        self.assertIn("Map observed addresses 10.250.2.2", item["text"])
        self.assertNotIn("no target-reported source address", item["text"])
        self.cfg["wan_links"][1]["appliance_addresses"] = ["10.250.2.2"]
        mapped = self.realtime(netem.assess_steering(dem, self.signals(), self.cfg, now=101))
        self.assertEqual((mapped["verdict"], mapped["unattributed"]), ("balanced", 0))
        self.assertEqual(mapped["shares"][1]["pct"], 100.0)

    def test_partial_attribution_cannot_prove_steering_or_finish_reaction(self):
        netem.assess_steering(self.dem("wan2"), self.signals(), self.cfg, now=90)
        netem.assess_steering(self.dem("wan2"), self.signals(False), self.cfg, now=100)
        dem = self.dem("wan1")
        dem["diagnosis"]["egress_recent"]["egress"]["unknown"] = {"video": {"requests": 8}}
        item = self.realtime(netem.assess_steering(dem, self.signals(False), self.cfg, now=114))
        self.assertEqual((item["verdict"], item["requests"], item["unattributed_pct"]), ("partial", 16, 50.0))
        self.assertEqual(item["shares"][0]["pct"], 50.0)
        self.assertEqual(item["reactions"], [])
        self.assertFalse(any(entry["steered_at"] for entry in netem.STEERING_STATE["classes"].values()))
        self.log.assert_not_called()

    def test_idle_means_no_completed_transactions_in_the_window(self):
        dem = {"diagnosis": {"egress_recent": {"egress": {}}}}
        item = self.realtime(netem.assess_steering(dem, self.signals(), self.cfg, now=100))
        self.assertEqual((item["verdict"], item["requests"]), ("idle", 0))
        self.assertIsNone(item["unattributed_pct"])
