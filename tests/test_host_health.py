"""The NetEm host's own headroom: readings parsed from /proc and /sys, assessed against lab limits."""
import tempfile
import unittest
from pathlib import Path

import host_health as hh


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.proc = Path(self.directory.name) / "proc"
        self.sys_net = Path(self.directory.name) / "sys" / "net"
        write(self.proc, "stat", "cpu  100 0 50 800 20 0 5 10 0 0\ncpu0 1 2 3 4\n")
        write(self.proc, "meminfo", "MemTotal:       8000000 kB\nMemAvailable:   1200000 kB\n")

    def test_cpu_busy_steal_and_iowait_come_from_the_difference_between_two_readings(self):
        before = hh.read_cpu_counters(self.proc)
        write(self.proc, "stat", "cpu  300 0 150 1200 60 0 15 40 0 0\ncpu0 1 2 3 4\n")
        after = hh.read_cpu_counters(self.proc)
        # 780 jiffies passed; 440 of them were idle (I/O wait counts as idle), 30 stolen, 40 spent in I/O wait.
        values = hh.cpu_percentages(before, after)
        self.assertAlmostEqual(values["cpu_busy_pct"], 43.6, places=1)
        self.assertAlmostEqual(values["cpu_steal_pct"], 3.8, places=1)
        self.assertAlmostEqual(values["cpu_iowait_pct"], 5.1, places=1)
        self.assertEqual(hh.cpu_percentages(None, after), {})
        self.assertEqual(hh.cpu_percentages(before, before), {})

    def test_memory_available_is_a_share_of_total(self):
        self.assertEqual(hh.read_meminfo(self.proc), {"mem_available_pct": 15.0})
        self.assertEqual(hh.read_meminfo(Path(self.directory.name) / "missing"), {})

    def test_pressure_reads_the_ten_second_average_and_is_none_when_absent(self):
        write(self.proc, "pressure/cpu", "some avg10=12.50 avg60=3.00 avg300=1.00 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        write(self.proc, "pressure/io", "some avg10=0.25 avg60=0.00 avg300=0.00 total=5\n")
        values = hh.read_pressure(self.proc)
        self.assertEqual(values, {"psi_cpu": 12.5, "psi_memory": None, "psi_io": 0.25})

    def test_nic_drops_and_errors_are_summed_over_the_wan_interfaces(self):
        for field, value in (("rx_dropped", "3"), ("tx_dropped", "0"), ("rx_errors", "1"), ("tx_errors", "0")):
            write(self.sys_net, f"eth1/statistics/{field}", value)
        self.assertEqual(hh.nic_total(self.sys_net, ["eth1", "eth2"]), 4)
        self.assertIsNone(hh.nic_total(self.sys_net, ["eth9"]))

    def test_a_reading_carries_the_rate_of_nic_drops_between_two_samples(self):
        write(self.sys_net, "eth1/statistics/rx_dropped", "10")
        _, state = hh.sample_host({}, ["eth1"], 0.0, self.proc, self.sys_net)
        write(self.sys_net, "eth1/statistics/rx_dropped", "30")
        reading, state = hh.sample_host(state, ["eth1"], 10.0, self.proc, self.sys_net)
        self.assertEqual(reading["nic_drops_ps"], 2.0)
        write(self.sys_net, "eth1/statistics/rx_dropped", "5")  # a reset counter gives no rate, not a negative one
        reading, _ = hh.sample_host(state, ["eth1"], 20.0, self.proc, self.sys_net)
        self.assertIsNone(reading["nic_drops_ps"])

    def test_disk_free_is_unavailable_where_statvfs_is_missing(self):
        self.assertIsNone(hh.disk_free_pct(Path("/no/such/path")))


class AssessmentTests(unittest.TestCase):
    def test_a_quiet_host_has_headroom(self):
        self.assertEqual(hh.assess({"cpu_busy_pct": 40.0, "mem_available_pct": 60.0}), {"status": "ok", "reasons": []})

    def test_a_busy_reading_and_an_at_limit_reading_are_named(self):
        busy = hh.assess({"cpu_busy_pct": 85.0})
        self.assertEqual((busy["status"], [item["label"] for item in busy["reasons"]]), ("busy", ["CPU busy"]))
        verdict = hh.assess({"cpu_busy_pct": 85.0, "psi_memory": 25.0})
        self.assertEqual(verdict["status"], "at_limit")
        self.assertEqual([item["state"] for item in verdict["reasons"]], ["at_limit", "busy"])

    def test_memory_and_disk_are_worse_when_they_fall(self):
        self.assertEqual(hh.assess({"mem_available_pct": 4.0})["status"], "at_limit")
        self.assertEqual(hh.assess({"disk_free_pct": 8.0})["status"], "busy")
        self.assertEqual(hh.assess({"disk_free_pct": 50.0})["status"], "ok")

    def test_a_late_sampler_is_itself_a_disturbance(self):
        verdict = hh.assess({"sampler_lag_ms": 1500.0})
        self.assertEqual((verdict["status"], verdict["reasons"][0]["label"]), ("at_limit", "Sampling lateness"))

    def test_no_readings_is_unknown_not_ok(self):
        self.assertEqual(hh.assess({}), {"status": "unknown", "reasons": []})


class SummaryTests(unittest.TestCase):
    def row(self, timestamp, status, reasons=(), **readings):
        base = {"timestamp": timestamp, "status": status, "reasons": [{"label": label, "state": status} for label in reasons]}
        base.update(readings)
        return base

    def test_the_window_counts_seconds_in_each_state_and_groups_the_intervals(self):
        rows = [
            self.row(0, "ok", cpu_busy_pct=30.0, sampler_lag_ms=5.0),
            self.row(2, "busy", ["CPU busy"], cpu_busy_pct=85.0, sampler_lag_ms=300.0),
            self.row(4, "at_limit", ["CPU busy", "Sampling lateness"], cpu_busy_pct=95.0, sampler_lag_ms=1500.0),
            self.row(6, "at_limit", ["CPU steal (hypervisor)"], cpu_busy_pct=60.0, cpu_steal_pct=6.0, sampler_lag_ms=10.0),
            self.row(8, "ok", cpu_busy_pct=35.0, sampler_lag_ms=4.0),
            self.row(10, "busy", ["Memory pressure"], psi_memory=8.0, cpu_busy_pct=35.0),
        ]
        summary = hh.summarize(rows, interval_s=2.0, window_s=600)
        self.assertEqual(summary["status"], "busy")
        self.assertEqual(summary["reasons"], [{"label": "Memory pressure", "state": "busy"}])
        self.assertEqual(summary["at_limit_seconds"], 4.0)
        self.assertEqual(summary["busy_seconds"], 4.0)
        self.assertEqual(summary["late_seconds"], 2.0)
        self.assertEqual([item["status"] for item in summary["intervals"]], ["at_limit", "busy"])
        self.assertEqual(summary["intervals"][0]["reasons"], ["CPU busy", "Sampling lateness", "CPU steal (hypervisor)"])
        cpu = next(item for item in summary["readings"] if item["key"] == "cpu_busy_pct")
        self.assertEqual((cpu["worst"], cpu["current"], cpu["busy"], cpu["limit"]), (95.0, 35.0, 80.0, 90.0))

    def test_an_empty_window_is_unknown(self):
        summary = hh.summarize([], interval_s=2.0, window_s=600)
        self.assertEqual((summary["status"], summary["samples"], summary["readings"], summary["intervals"]),
                         ("unknown", 0, [], []))


if __name__ == "__main__":
    unittest.main()
