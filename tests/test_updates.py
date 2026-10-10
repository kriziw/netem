"""Update progress: the status the update screen polls and the restart marker it waits for."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem

STATUS = {"ok": True, "error": "", "branch": "main", "target_branch": "main", "commit": "abc1234",
          "subject": "release", "remote_url": "https://example.invalid/netem.git", "behind": 2, "ahead": 0,
          "dirty": False, "installed_version": "0.12.0", "remote_version": "0.13.0"}


class UpdateScreenTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()

    def test_status_names_this_process_and_version(self):
        response = self.client.get("/updates/status")
        self.assertEqual(response.json, {"version": netem.get_app_version(), "instance": netem.PROCESS_INSTANCE})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_installed_update_marks_the_process_that_restarts(self):
        with patch.object(netem, "git_update_status", side_effect=[STATUS, dict(STATUS, behind=0, installed_version="0.13.0")]), \
             patch.object(netem, "run_process", return_value=(0, "", "")) as run, \
             patch.object(netem.threading, "Thread") as restart:
            page = self.client.post("/updates", data={"action": "update"}).text
        self.assertEqual(run.call_args.args[0][1:], ["merge", "--ff-only", "origin/main"])
        restart.return_value.start.assert_called_once()
        self.assertIn(f'id="update-restarting" data-instance="{netem.PROCESS_INSTANCE}" data-version="0.13.0"', page)

    def test_refused_update_has_no_restart_marker_and_says_why(self):
        with patch.object(netem, "git_update_status", return_value=dict(STATUS, dirty=True)), \
             patch.object(netem, "run_process") as run, patch.object(netem.threading, "Thread") as restart:
            page = self.client.post("/updates", data={"action": "update"}).text
        run.assert_not_called()
        restart.assert_not_called()
        self.assertNotIn('id="update-restarting"', page)
        self.assertIn("local changes", page)

    def test_install_form_describes_the_update_for_the_screen(self):
        with patch.object(netem, "git_update_status", return_value=STATUS):
            page = self.client.get("/updates").text
        for attribute in ('data-update-form', 'data-from="0.12.0"', 'data-to="0.13.0"',
                          f'data-instance="{netem.PROCESS_INSTANCE}"', 'data-status-url="/updates/status"'):
            self.assertIn(attribute, page)
        self.assertNotIn("onsubmit=", page)


class TelemetryReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        for name, value in (("RUNTIME_DIR", Path(self.temp.name)), ("TELEMETRY_DB_PATH", Path(self.temp.name) / "telemetry.db")):
            mock = patch.object(netem, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_latest_sample_is_a_plain_read(self):
        # Before the worker creates the table, the latest sample is unknown rather than an error.
        self.assertIsNone(netem.latest_telemetry_sample("wan1"))
        netem.init_telemetry_db()
        with patch.object(netem, "init_telemetry_db") as setup:
            self.assertIsNone(netem.latest_telemetry_sample("wan1"))
        setup.assert_not_called()

    def test_sample_written_while_the_snapshot_is_built_counts_as_fresh(self):
        state = {"id": "wan1", "label": "WAN1", "preset_name": "DIA", "runtime_quality": 100, "fault": "normal",
                 "sla": {"pass": True}, "effective": {"delay_ms": 5, "jitter_ms": 1, "loss_pct": 0}}
        sample = {"timestamp": 101.0, "rate_valid": 1, "down_mbps": 4.2, "up_mbps": 0.8}
        clock = iter([100.0])
        with patch.object(netem, "load_config", return_value={}), \
             patch.object(netem, "build_link_states", return_value=[state]), \
             patch.object(netem, "latest_telemetry_sample", return_value=sample), \
             patch.object(netem, "current_diagnosis", return_value={}), \
             patch.object(netem.time, "time", side_effect=lambda: next(clock, 102.0)):
            link = netem.showroom_snapshot()["links"][0]
        self.assertTrue(link["traffic_available"])
        self.assertEqual((link["down_mbps"], link["up_mbps"]), (4.2, 0.8))


if __name__ == "__main__":
    unittest.main()
