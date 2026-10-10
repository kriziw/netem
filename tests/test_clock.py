"""Clock steps and time sync: NetEm keeps working when its wall clock moves, and checks that
NetEm, the Traffic Simulator and the target agree on the time."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem


class ClockStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        for name, value in (("RUNTIME_DIR", Path(self.temp.name)), ("TELEMETRY_DB_PATH", Path(self.temp.name) / "telemetry.db")):
            mock = patch.object(netem, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        netem.init_telemetry_db()

    def insert(self, timestamp, down=5.0):
        row = {"timestamp": timestamp, "link_id": "wan1", "down_mbps": down, "up_mbps": 1.0, "down_pps": 10.0, "up_pps": 5.0,
               "delay_ms": 5.0, "jitter_ms": 1.0, "loss_pct": 0.0, "quality": 100.0, "sla_pass": 1, "fault": "normal",
               "session_id": None, "rate_valid": 1, "down_util_pct": 5.0}
        with netem.telemetry_connect() as conn:
            conn.execute(f"INSERT INTO telemetry_samples ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))

    def test_samples_dated_in_the_future_are_ignored_then_discarded(self):
        now = time.time()
        self.insert(now - 1, down=7.0)
        self.insert(now + 6207, down=0.0)  # recorded while the clock ran 1 h 43 min ahead
        self.assertEqual(netem.latest_telemetry_sample("wan1")["down_mbps"], 7.0)
        self.assertEqual([row["down_mbps"] for row in netem.recent_telemetry_samples("wan1")], [7.0])
        self.assertEqual(len(netem.query_telemetry_history("wan1", now - 60)), 1)
        with patch.object(netem, "log_event") as log, patch.object(netem, "CLOCK_STEPS", []):
            removed = netem.discard_future_samples()
            netem.note_clock_step(-6207.4, removed)
            self.assertEqual(removed, 1)
            self.assertIn("moved back 6207 s; discarded 1 samples", log.call_args.args[1])
            self.assertEqual(netem.CLOCK_STEPS[-1]["step_s"], -6207.4)

    def test_caches_and_schedules_survive_a_step_back(self):
        built = []
        with patch.object(netem, "DIAGNOSIS_CACHE", {"timestamp": time.time() + 6000, "checked": time.monotonic() - 10, "payload": {"old": True}}), \
             patch.object(netem, "build_diagnosis", side_effect=lambda: built.append(1) or {"new": True}):
            # The wall-clock age is now negative; the monotonic age still says the cache is stale.
            self.assertEqual(netem.current_diagnosis(max_age=4), {"new": True})
            self.assertEqual(netem.current_diagnosis(max_age=4), {"new": True})
        self.assertEqual(built, [1])
        state = {"running": False, "last_run": time.time() + 6000, "last_clock": time.monotonic() - 61, "error": None, "target": None}
        with patch.object(netem, "EGRESS_LEARN_STATE", state), patch.object(netem.shutil, "which", return_value="/usr/sbin/tcpdump"), \
             patch.object(netem.socket, "getaddrinfo", return_value=[(None, None, None, None, ("198.18.0.1", 0))]), \
             patch.object(netem.threading, "Thread") as learner:
            netem.maybe_learn_egress({}, {"status": "running", "run": {"target": "http://198.18.0.1:8090"}}, ["10.250.1.2"])
        learner.return_value.start.assert_called_once()

    def test_test_timing_follows_the_monotonic_clock(self):
        phases = [{"name": "Baseline", "planned_s": 60}, {"name": "Brownout", "planned_s": 90}]
        state = dict(netem.SCENARIO_STATE, active=True, started_at=time.time() + 6000, clock_start=100.0, phases=phases,
                     phase_index=1, phase="Brownout", phase_started_s=60.0, paused=True, paused_clock=170.0, paused_total_s=5.0)
        with patch.object(netem, "SCENARIO_STATE", state), patch.object(netem.time, "monotonic", return_value=180.0):
            snapshot = netem.scenario_snapshot()
        # 80 s on the monotonic clock, 5 s paused before and 10 s in the current pause.
        self.assertEqual((snapshot["elapsed_s"], snapshot["phase_remaining_s"]), (65.0, 85.0))


class TimeSyncTests(unittest.TestCase):
    def setUp(self):
        mock = patch.object(netem, "CLOCK_STATUS_CACHE", {"checked": None, "payload": None})
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(netem, "CLOCK_STEPS", [])
        mock.start()
        self.addCleanup(mock.stop)

    def status(self, local, remote=None, configured=True):
        netem.CLOCK_STATUS_CACHE.update(checked=None)
        with patch.object(netem, "local_time_sync", return_value=local), \
             patch.object(netem, "traffic_generator_config", return_value={"host": "192.168.0.135"} if configured else {}), \
             patch.object(netem, "traffic_generator_api_key", return_value="key"), \
             patch.object(netem, "load_config", return_value={}), \
             patch.object(netem, "simulator_clock", return_value=remote):
            return netem.platform_clock_status()

    def test_components_in_sync(self):
        status = self.status({"ntp": True, "synchronized": True},
                             {"offset_s": 0.4, "ntp": True, "synchronized": True, "container": True, "target_offset_s": -0.2})
        self.assertTrue(status["ok"])
        self.assertEqual([(item["name"], item["offset_s"]) for item in status["components"]],
                         [("NetEm", 0.0), ("Traffic Simulator", 0.4), ("Controlled target", 0.2)])
        self.assertIsNone(netem.clock_finding(status))

    def test_offsets_and_unsynchronized_clocks_are_reported(self):
        status = self.status({"ntp": False, "synchronized": False},
                             {"offset_s": 6207.4, "ntp": True, "synchronized": True, "container": True, "target_offset_s": 1.0})
        self.assertFalse(status["ok"])
        self.assertIn("The Traffic Simulator clock is 1 h 43 min ahead of NetEm.", status["issues"])
        self.assertIn("NetEm is not synchronized to a time server: time sync is turned off.", status["issues"])
        finding = netem.clock_finding(status)
        self.assertEqual((finding["source"], finding["severity"]), ("platform", "warn"))
        # An older simulator cannot be compared, which is not an issue by itself.
        older = self.status({"ntp": True, "synchronized": True}, None)
        self.assertTrue(older["ok"])
        self.assertEqual(older["components"][1]["note"], "Update the simulator to compare clocks.")

    def test_simulator_offset_uses_the_request_midpoint(self):
        with patch.object(netem.time, "time", side_effect=[1000.0, 1000.4]), \
             patch.object(netem, "traffic_generator_request", return_value={"time": 1003.2, "clock": {"ntp": True, "synchronized": True}}):
            remote = netem.simulator_clock()
        self.assertEqual((remote["offset_s"], remote["uncertainty_s"]), (3.0, 0.2))
        with patch.object(netem, "traffic_generator_request", return_value={"status": "ok"}):
            self.assertIsNone(netem.simulator_clock())

    def test_netem_turns_on_time_sync_only_when_it_is_off(self):
        with patch.object(netem, "local_time_sync", return_value={"ntp": True, "synchronized": True}), \
             patch.object(netem, "run_process") as run:
            self.assertFalse(netem.ensure_time_sync())
            run.assert_not_called()
        with patch.object(netem, "local_time_sync", return_value={"ntp": False, "synchronized": False}), \
             patch.object(netem, "run_process", return_value=(0, "", "")) as run, patch.object(netem, "log_event") as log:
            self.assertTrue(netem.ensure_time_sync())
        self.assertEqual(run.call_args.args[0][1:], ["set-ntp", "true"])
        self.assertIn("Turned on time sync", log.call_args.args[1])

    def test_settings_shows_clocks_and_offers_to_turn_on_time_sync(self):
        status = {"ok": False, "issues": ["The Traffic Simulator clock is 1.7 h ahead of NetEm."], "simulator_error": None,
                  "components": [{"name": "NetEm", "offset_s": 0.0, "ntp": False, "synchronized": False},
                                 {"name": "Traffic Simulator", "offset_s": 6207.4, "ntp": True, "synchronized": True, "container": True}],
                  "tolerance_s": 2.0, "checked_at": 0}
        client = netem.app.test_client()
        with patch.object(netem, "platform_clock_status", return_value=status):
            page = client.get("/settings").text
        for text in ("Time sync", "Check clocks", "+6207.4 s", "uses its Proxmox host", "1.7 h ahead", "Turn on time sync for NetEm"):
            self.assertIn(text, page)
        with patch.object(netem, "run_process", return_value=(1, "", "Access denied")), \
             patch.object(netem, "platform_clock_status", return_value=status):
            refused = client.post("/settings/time-sync", follow_redirects=True).text
        self.assertIn("sudo timedatectl set-ntp true", refused)

    def test_showroom_keeps_platform_findings_for_operators(self):
        finding = netem.clock_finding({"ok": False, "issues": ["The Traffic Simulator clock is 2 min ahead of NetEm."]})
        outcome = netem.showroom_outcome({"findings": [finding]}, None)
        self.assertEqual(outcome["findings"], [])


if __name__ == "__main__":
    unittest.main()
