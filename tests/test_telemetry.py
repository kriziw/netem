"""Deterministic measurement tests; no privileged network changes required."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as netem


LINK = {"id": "wan1", "inner": "inside", "outer": "outside"}
STATE = {"id": "wan1", "effective": {"delay_ms": 20, "jitter_ms": 3, "loss_pct": 1},
         "runtime_quality": 80, "sla": {"pass": True}, "fault": "normal"}


def observation(t=10, down=100, up=200, down_packets=10, up_packets=20):
    return {"valid": True, "identity": (("inside", 1), ("outside", 2)),
            "timestamp": 1000 + t, "monotonic_timestamp": t,
            "down_bytes": down, "up_bytes": up,
            "down_packets": down_packets, "up_packets": up_packets}


class RateTests(unittest.TestCase):
    def test_decimal_mbps_and_direction(self):
        before = observation()
        after = observation(12, 250100, 125200, 210, 120)
        self.assertEqual(netem.traffic_rates(after, before), (1, .5, 100, 50))
        after["timestamp"] = -5000  # NTP/wall-clock correction cannot affect rates.
        self.assertEqual(netem.traffic_rates(after, before), (1, .5, 100, 50))

    def test_idle_is_measured_zero(self):
        self.assertEqual(netem.traffic_rates(observation(12), observation()), (0, 0, 0, 0))

    def test_invalid_intervals_rebaseline(self):
        before = observation()
        after = observation(12, 300, 400, 30, 40)
        self.assertIsNone(netem.traffic_rates(after, None))
        for bad in (dict(after, valid=False), dict(after, monotonic_timestamp=10),
                    dict(after, down_bytes=99), dict(after, up_packets=19),
                    dict(after, identity=(("replacement", 3), ("outside", 2))),
                    dict(after, identity=(("inside", 9), ("outside", 2)))):
            self.assertIsNone(netem.traffic_rates(bad, before))
        self.assertIsNone(netem.traffic_rates(after, dict(before, valid=False)))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(netem, "RUNTIME_DIR", self.root),
                        patch.object(netem, "TELEMETRY_DB_PATH", self.root / "telemetry.db"),
                        patch.object(netem, "TELEMETRY_PREVIOUS", {}),
                        patch.object(netem, "load_config", return_value={"wan_links": [LINK]}),
                        patch.object(netem, "build_link_states", return_value=[STATE])]
        for item in self.patches:
            item.start()
        netem.init_telemetry_db()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def collect(self, sample):
        with patch.object(netem, "traffic_snapshot", return_value=sample):
            netem.collect_telemetry_sample()

    def test_invalid_samples_excluded_and_legacy_schema_migrated(self):
        self.collect(observation())
        # Simulate the exact pre-upgrade schema without destroying existing rows.
        with netem.telemetry_connect() as conn:
            conn.execute("ALTER TABLE telemetry_samples DROP COLUMN rate_valid")
        netem.init_telemetry_db()
        netem.init_telemetry_db()  # Migration is idempotent.
        with netem.telemetry_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)
            conn.execute("DELETE FROM telemetry_samples")
        netem.TELEMETRY_PREVIOUS.clear()
        self.collect(observation())
        self.assertIsNone(netem.latest_telemetry_sample("wan1")["down_mbps"])
        self.collect(observation(12, 250100, 125200, 210, 120))
        with patch.object(netem.time, "time", return_value=1014):
            history = netem.query_telemetry_history("wan1", 1000, max_points=50)
        self.assertIsNone(history[0]["down_mbps"])
        self.assertEqual(history[1]["down_mbps"], 1)
        self.assertTrue(netem.latest_telemetry_sample("wan1")["rate_valid"])

    def test_failed_read_and_recovery_do_not_create_spike(self):
        self.collect(observation())
        self.collect(dict(observation(12), valid=False, down_bytes=None))
        self.collect(observation(14, 100000000, 200000000))
        self.assertIsNone(netem.latest_telemetry_sample("wan1")["down_mbps"])
        self.collect(observation(16, 100250000, 200125000))
        self.assertEqual(netem.latest_telemetry_sample("wan1")["down_mbps"], 1)

    def test_session_report_does_not_treat_missing_rates_as_idle(self):
        session = {"id": "session1", "name": "Accuracy test", "started_at": 1000,
                   "ended_at": 1020, "active": False}
        with patch.object(netem, "active_session_id", return_value="session1"):
            self.collect(observation())
        with patch.object(netem, "LAB_SESSIONS", [session]):
            report = netem.build_session_report("session1")
            self.assertEqual(report["telemetry"]["wan1"]["valid_rate_samples"], 0)
            self.assertIsNone(report["telemetry"]["wan1"]["max_down_mbps"])
            response = netem.app.test_client().get("/sessions/session1/report")
            self.assertEqual(response.status_code, 200)
            self.assertIn("0 / 1", response.get_data(as_text=True))
        with patch.object(netem, "active_session_id", return_value="session1"):
            self.collect(observation(12, 250100, 125200))
        with patch.object(netem, "LAB_SESSIONS", [session]):
            item = netem.build_session_report("session1")["telemetry"]["wan1"]
            self.assertEqual(item["avg_down_mbps"], 1)
            self.assertEqual(item["valid_rate_samples"], 1)

    def test_removed_link_does_not_reuse_baseline(self):
        self.collect(observation())
        with patch.object(netem, "load_config", return_value={"wan_links": []}):
            netem.collect_telemetry_sample()
        self.assertFalse(netem.TELEMETRY_PREVIOUS)
        self.collect(observation(12, 250100))
        self.assertFalse(netem.latest_telemetry_sample("wan1")["rate_valid"])

    def test_stale_or_invalid_traffic_cannot_pass_assertion(self):
        self.collect(observation())
        condition = {"type": "traffic", "field": "down_mbps", "op": "<=", "value": 1}
        with patch.object(netem.time, "time", return_value=1010):
            self.assertFalse(netem.evaluate_scenario_condition(condition, "wan1")[0])
        self.collect(observation(12, 250100))
        with patch.object(netem.time, "time", return_value=1012):
            self.assertTrue(netem.evaluate_scenario_condition(condition, "wan1")[0])
        with patch.object(netem.time, "time", return_value=1020):
            self.assertFalse(netem.evaluate_scenario_condition(condition, "wan1")[0])


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(netem, "NET_SYSFS", self.root)
        self.patch.start()
        for index, name in enumerate(("inside", "outside"), 1):
            base = self.root / name
            (base / "statistics").mkdir(parents=True)
            (base / "ifindex").write_text(str(index))
            (base / "operstate").write_text("up")
            (base / "carrier").write_text("1")
            for key in netem.interface_counters(None):
                (base / "statistics" / key).write_text("100")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_missing_and_corrupt_counters_are_not_zero(self):
        self.assertIsNone(netem.interface_counters("missing")["tx_bytes"])
        (self.root / "inside/statistics/tx_bytes").write_text("bad")
        self.assertFalse(netem.traffic_snapshot(LINK)["valid"])

    def test_api_snapshot_exposes_identity_and_read_validity(self):
        with patch.object(netem, "load_config", return_value={"wan_links": [LINK]}):
            payload = netem.app.test_client().get("/api/v1/telemetry").get_json()
        self.assertTrue(payload["sampler_id"])
        link = payload["links"][0]
        self.assertTrue(link["counters_valid"])
        self.assertEqual(link["inner"]["ifindex"], 1)
        self.assertEqual(link["traffic"]["download"]["bytes"], 100)
        self.assertGreater(link["monotonic_timestamp"], 0)

    def test_prometheus_omits_failed_counter_reads(self):
        (self.root / "inside/statistics/tx_bytes").unlink()
        with patch.object(netem, "load_config", return_value={"wan_links": [LINK]}):
            response = netem.app.test_client().get("/metrics").get_data(as_text=True)
        self.assertNotIn('side="inner"', response)
        self.assertIn('side="outer"', response)


class ProbeTests(unittest.TestCase):
    probe = {"id": "ping1", "kind": "icmp", "target": "192.0.2.1", "link_id": "wan1", "timeout_s": .5}

    def run_ping(self, output):
        with patch.object(netem.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")) as run:
            result = netem.execute_probe(self.probe, {"wan_links": [LINK]})
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertIn("0.5", run.call_args.args[0])
        return result

    def test_echo_rtt_and_summary_fallback(self):
        self.assertEqual(self.run_ping("64 bytes: time=1.23 ms")["latency_ms"], 1.23)
        self.assertEqual(self.run_ping("rtt min/avg/max/mdev = 0.004/0.004/0.004/0.000 ms")["latency_ms"], .004)
        self.assertEqual(self.run_ping("64 bytes: time<1 ms\nrtt min/avg/max/mdev = 0.004/0.004/0.004/0.000 ms")["latency_ms"], .004)

    def test_unparseable_rtt_never_reports_process_runtime(self):
        result = self.run_ping("echo reply without RTT")
        self.assertFalse(result["success"])
        self.assertIsNone(result["latency_ms"])

    def test_failed_http_probe_closes_socket(self):
        from unittest.mock import Mock
        sock = Mock()
        sock.sendall.side_effect = OSError("timeout")
        with patch.object(netem, "open_bound_tcp", return_value=sock):
            result = netem.execute_probe(dict(self.probe, kind="http", target="http://example.test/"), {})
        self.assertFalse(result["success"])
        sock.close.assert_called()

    def test_dns_requires_a_matching_complete_response_with_answers(self):
        from unittest.mock import Mock
        def query(flags, answers, transaction=123):
            response = (transaction.to_bytes(2, "big") + flags.to_bytes(2, "big")
                        + b"\x00\x01" + answers.to_bytes(2, "big") + b"\x00" * 4)
            sock = Mock()
            sock.recv.return_value = response
            with patch.object(netem.socket, "getaddrinfo", return_value=[(2, 2, 17, "", ("192.0.2.53", 53))]), patch.object(netem.socket, "socket", return_value=sock), patch.object(netem.time, "time_ns", return_value=123):
                result = netem.execute_probe(dict(self.probe, kind="dns", target="example.test"), {})
            sock.connect.assert_called_once_with(("192.0.2.53", 53))
            sock.close.assert_called()
            return result
        self.assertTrue(query(0x8180, 1)["success"])
        for flags, answers, transaction in [(0x0100, 1, 123), (0x8380, 1, 123),
                                            (0x8183, 0, 123), (0x8180, 0, 123),
                                            (0x8180, 1, 456)]:
            self.assertFalse(query(flags, answers, transaction)["success"])


class QdiscTests(unittest.TestCase):
    def test_units_handles_and_child_order(self):
        result = netem.parse_qdisc_output("qdisc tbf a: root rate 900Kbit\nqdisc netem b: parent a:1 delay 500us 1s loss random 2%")
        self.assertEqual(result["parsed"], {"kind": "netem", "delay_ms": .5, "jitter_ms": 1000, "loss_pct": 2, "rate_mbit": .9})
        self.assertAlmostEqual(netem.parse_qdisc_output("qdisc tbf 10: root rate 800bit")["parsed"]["rate_mbit"], .0008)
        self.assertEqual(netem.parse_qdisc_output("qdisc netem 1: root limit 1000")["parsed"]["loss_pct"], 0)

    def test_model_sla_identifies_its_source(self):
        result = netem.evaluate_sla(STATE["effective"], netem.DEFAULT_SLA_PROFILE)
        self.assertEqual(result["source"], "impairment_model")


if __name__ == "__main__":
    unittest.main()
