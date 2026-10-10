"""Release-based updates, the status the update screen polls and the restart marker it waits for."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem

STATUS = {"ok": True, "error": "", "branch": "main", "target_branch": "main", "commit": "abc1234",
          "subject": "release", "remote_url": "https://example.invalid/netem.git", "behind": 2, "ahead": 0,
          "dirty": False, "installed_version": "0.12.0", "remote_version": "0.13.0", "release_commit": "f00d123",
          "release_behind": 2, "release_reachable": True, "unreleased": 0, "update_available": True}


def git(outputs):
    """Fake git: answers by the command's subcommand and first argument."""
    def run(command, timeout=None):
        args = command[1:]
        for key, result in outputs.items():
            if tuple(args[:len(key)]) == key:
                return result
        return 0, "", ""
    return run


def channel(installed, remote, behind, release_behind, unreleased, ancestor=True):
    return {("branch",): (0, "main", ""), ("log", "-1", "--pretty=%h%x09%s"): (0, "abc1234\tsubject", ""),
            ("status",): (0, "", ""), ("show",): (0, remote, ""), ("rev-parse",): (0, "deadbeef", ""),
            ("rev-list", "--count", "HEAD..origin/main"): (0, str(behind), ""),
            ("rev-list", "--count", "origin/main..HEAD"): (0, "0", ""),
            ("log", "-1", "--format=%h", "origin/main"): (0, "f00d123", ""),
            ("rev-list", "--count", "HEAD..f00d123"): (0, str(release_behind), ""),
            ("rev-list", "--count", "f00d123..origin/main"): (0, str(unreleased), ""),
            ("merge-base",): (0 if ancestor else 1, "", ""), ("remote",): (0, "https://example.invalid/netem.git", "")}


class ReleaseChannelTests(unittest.TestCase):
    def status(self, installed, *args, **kwargs):
        with patch.object(netem, "run_process", side_effect=git(channel(installed, *args, **kwargs))), \
             patch.object(netem, "get_app_version", return_value=installed):
            return netem.git_update_status()

    def test_merged_commits_without_a_release_are_not_an_update(self):
        status = self.status("0.13.0", "0.13.0", behind=4, release_behind=0, unreleased=4)
        self.assertFalse(status["update_available"])
        self.assertEqual((status["behind"], status["unreleased"]), (4, 4))

    def test_newer_release_is_an_update_to_its_release_commit(self):
        status = self.status("0.12.0", "0.13.0", behind=5, release_behind=3, unreleased=2)
        self.assertTrue(status["update_available"])
        self.assertEqual((status["release_commit"], status["release_behind"]), ("f00d123", 3))
        self.assertTrue(status["release_reachable"])
        self.assertFalse(self.status("0.12.0", "0.13.0", behind=5, release_behind=3, unreleased=2, ancestor=False)["release_reachable"])
        # Without semantic versions, release commits still decide.
        self.assertTrue(self.status("dev", "dev", behind=1, release_behind=1, unreleased=0)["update_available"])

    def test_page_and_settings_say_up_to_date_while_commits_wait_for_a_release(self):
        waiting = dict(STATUS, installed_version="0.13.0", update_available=False, release_behind=0, unreleased=4, behind=4)
        client = netem.app.test_client()
        with patch.object(netem, "git_update_status", return_value=waiting):
            page = client.get("/updates").text
            settings = client.get("/settings").text
            with patch.object(netem, "run_process") as run:
                refused = client.post("/updates", data={"action": "update"}).text
        self.assertIn('<span class="status good">Up to date</span>', page)
        self.assertNotIn("Update available", page)
        self.assertIn("latest release installed", settings)
        run.assert_not_called()
        self.assertIn("NetEm already runs the latest release. 4 merged commit(s) on main will come with the next release.", refused)


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
        self.assertEqual(run.call_args.args[0][1:], ["merge", "--ff-only", "f00d123"])
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
