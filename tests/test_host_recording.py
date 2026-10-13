"""Host readings are stored with each telemetry sample, and the Platform Health summary reads them back."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem


class HostRecordingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        patches = [
            patch.object(netem, "TELEMETRY_DB_PATH", Path(directory.name) / "telemetry.db"),
            patch.object(netem, "load_config", return_value={"wan_links": []}),
            patch.object(netem, "log_event"),
            patch.dict(netem.HOST_STATE, {}, clear=True),
            patch.dict(netem.HOST_STATUS, {"status": None}),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        self.log = netem.log_event
        netem.init_telemetry_db()

    def reading(self, **values):
        return patch.object(netem.host_health, "sample_host", return_value=(dict(values), {}))

    def test_a_saturated_host_is_stored_and_reported_with_its_reasons(self):
        with self.reading(cpu_busy_pct=95.0, mem_available_pct=60.0):
            netem.record_host_sample(lag_ms=1500.0, collect_ms=40.0)
        headroom = netem.host_headroom(window_s=600)
        self.assertEqual(headroom["status"], "at_limit")
        self.assertEqual(sorted(item["label"] for item in headroom["reasons"]), ["CPU busy", "Sampling lateness"])
        self.assertEqual(headroom["samples"], 1)
        self.assertEqual(headroom["late_seconds"], netem.TELEMETRY_SAMPLE_SECONDS)
        self.assertEqual(self.log.call_count, 1)

    def test_only_changes_of_status_are_logged(self):
        with self.reading(cpu_busy_pct=40.0):
            netem.record_host_sample(0.0, 5.0)
            netem.record_host_sample(0.0, 5.0)
        with self.reading(cpu_busy_pct=85.0):
            netem.record_host_sample(0.0, 5.0)
        self.assertEqual([call.args[1] for call in self.log.call_args_list],
                         ["NetEm host ok", "NetEm host busy: CPU busy"])

    def test_a_failing_reading_is_logged_and_never_stops_the_telemetry(self):
        with patch.object(netem.host_health, "sample_host", side_effect=RuntimeError("no /proc")):
            netem.record_host_sample(0.0, 5.0)
        self.assertEqual(self.log.call_args.args[1], "Host reading failed")
        self.assertEqual(netem.host_headroom(window_s=600)["samples"], 0)


if __name__ == "__main__":
    unittest.main()
