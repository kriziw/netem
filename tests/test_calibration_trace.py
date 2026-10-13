"""The simulator's per-second readings joined to the WAN, and the cause each quiet second is given."""
import time
import unittest

import calibration_trace as ct


def poll(at, counters=None, loop=(), target=(), offset_clock=0.0):
    reply = {"time": at + offset_clock, "loop": {"samples": [list(item) for item in loop]},
             "target_probe": {"samples": [list(item) for item in target]}}
    if counters is not None:
        reply["counters"] = counters
    return ct.poll_record(at, at, reply)


def wan(at, down):
    return {"timestamp": at, "down_mbps": down, "up_mbps": 1.0, "down_backlog_bytes": 0, "down_queue_drops_ps": 0.0}


class PollRecordTests(unittest.TestCase):
    def test_latency_offset_and_malformed_samples_are_read(self):
        reply = {"time": 1000.5, "counters": {"bytes": 10, "requests": 2},
                 "loop": {"samples": [[999.9, 4.0], ["bad"], [1000.0, None]]},
                 "target_probe": {"samples": [[1000.1, None], [1000.2, 12.5]]}}
        record = ct.poll_record(999.9, 1000.1, reply)
        self.assertEqual(record["latency_ms"], 200.0)
        self.assertEqual(record["offset_s"], 0.5)
        self.assertEqual(record["loop"], [[999.9, 4.0]])
        self.assertEqual(record["target"], [[1000.1, None], [1000.2, 12.5]])

    def test_a_simulator_older_than_the_counters_is_read_without_them(self):
        record = ct.poll_record(10.0, 10.1, {"status": "ok"})
        self.assertIsNone(record["counters"])
        self.assertEqual(ct.rate_intervals([record]), ([], 0))


class RateTests(unittest.TestCase):
    def test_rates_come_from_counter_deltas_and_a_restart_is_not_a_negative_rate(self):
        polls = [poll(0.0, {"bytes": 0, "requests": 0}), poll(1.0, {"bytes": 6_250_000, "requests": 10}),
                 poll(2.0, {"bytes": 100, "requests": 1}), poll(3.0, {"bytes": 6_250_100, "requests": 11})]
        intervals, restarts = ct.rate_intervals(polls)
        self.assertEqual(restarts, 1)
        self.assertEqual([item["mbps"] for item in intervals], [50.0, 50.0])
        self.assertEqual([item["requests_ps"] for item in intervals], [10.0, 10.0])

    def test_in_flight_is_started_minus_completed(self):
        self.assertEqual(ct.in_flight({"paced_started": 7, "paced_completed": 5}), 2)
        self.assertIsNone(ct.in_flight(None))
        self.assertIsNone(ct.in_flight({"bytes": 1}))


class ClockTests(unittest.TestCase):
    def test_simulator_samples_move_onto_this_hosts_clock(self):
        polls = [poll(1000.0, loop=[(1002.5, 4.0)], offset_clock=2.0)]
        self.assertEqual(ct._local(polls, "loop"), [(1000.5, 4.0)])


class TraceTests(unittest.TestCase):
    def build(self):
        wan_rows = [wan(1001.0, 50.0), wan(1002.0, 0.0), wan(1003.0, 0.0), wan(1004.0, 0.0)]
        polls = [
            poll(1000.0, {"bytes": 0, "requests": 0, "paced_started": 0, "paced_completed": 0}),
            poll(1001.0, {"bytes": 6_250_000, "requests": 10, "paced_started": 2, "paced_completed": 2}),
            poll(1002.0, {"bytes": 6_250_000, "requests": 10, "paced_started": 2, "paced_completed": 2}),
            poll(1003.0, {"bytes": 6_250_000, "requests": 10, "paced_started": 4, "paced_completed": 2},
                 target=[(1002.6, 5.0)]),
            poll(1004.0, {"bytes": 6_250_000, "requests": 10, "paced_started": 4, "paced_completed": 4}),
        ]
        polls[2]["loop"] = [[1001.5, 300.0]]
        return ct.build(polls, wan_rows, 1000.0, 1004.0)

    def test_each_quiet_second_is_given_its_own_cause(self):
        summary = self.build()["summary"]
        self.assertEqual(summary["quiet_seconds"], 3)
        self.assertEqual(summary["quiet_loop_seconds"], 1)
        self.assertEqual(summary["quiet_target_seconds"], 0)
        self.assertEqual(summary["quiet_waiting_seconds"], 1)
        self.assertEqual(summary["quiet_idle_seconds"], 1)
        self.assertEqual(summary["loop_max_ms"], 300.0)
        self.assertEqual(summary["loop_stall_seconds"], 1)

    def test_the_window_rate_comes_from_the_first_and_last_poll(self):
        self.assertEqual(self.build()["summary"]["sim_mbps_mean"], 12.5)

    def test_quiet_seconds_are_located_in_the_window(self):
        trace = self.build()
        self.assertEqual([row["quiet"] for row in trace["seconds"]], [False, True, True, True])
        self.assertEqual(trace["summary"]["quiet_offsets"], [2.0, 3.0, 4.0])

    def test_a_poll_that_fails_is_counted_not_hidden(self):
        polls = [poll(1000.0, {"bytes": 0, "requests": 0}), ct.poll_record(1001.0, 1003.0, error="timed out")]
        summary = ct.build(polls, [], 1000.0, 1004.0)["summary"]
        self.assertEqual(summary["polls"], 2)
        self.assertEqual(summary["poll_errors"], 1)


class PollerTests(unittest.TestCase):
    def test_a_failed_poll_is_recorded_and_stop_returns_the_polls(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("timed out")
            return {"time": time.time(), "counters": {"bytes": 0, "requests": 0}}

        poller = ct.SimulatorPoller(fetch=fetch, interval_s=0.01).start()
        time.sleep(0.2)
        polls = poller.stop()
        self.assertGreaterEqual(len(polls), 2)
        self.assertTrue(any(item["error"] for item in polls))
        self.assertTrue(any(item["counters"] for item in polls))


if __name__ == "__main__":
    unittest.main()
