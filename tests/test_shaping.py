"""Queue sizing for the impairment and rate-limiter qdiscs."""
import unittest
from unittest.mock import patch

import app as netem


class ShaperSizingTests(unittest.TestCase):
    def test_queues_scale_with_rate_and_delay(self):
        self.assertEqual(netem.shaper_queue_sizes(300, 15, 0), (1000, 375000, 937_500))
        self.assertEqual(netem.shaper_queue_sizes(100, 5, 0), (1000, 125000, 312_500))
        # 300 ms at 100 Mbit/s keeps ~2,500 full-size packets in flight: netem's default 1000 would drop.
        self.assertEqual(netem.shaper_queue_sizes(100, 300, 0)[0], 5625)
        self.assertEqual(netem.shaper_queue_sizes(1, 0, 0), (1000, 3200, 65536))
        # Unshaped links are sized as 1 Gbit/s: 20 ms + 3 × 5 ms jitter is 4.4 MB in flight.
        self.assertEqual(netem.shaper_queue_sizes(0, 20, 5)[0], 6563)

    def test_applied_commands_use_the_sizes(self):
        commands = []
        with patch.object(netem, "run_cmd", side_effect=lambda cmd: commands.append(cmd) or (0, "", "")):
            self.assertEqual(netem.apply_netem("eth1", 15, 0, 0.1, 300), (True, "OK"))
        self.assertIn("netem limit 1000 delay 15.0ms loss 0.100%", commands[1])
        self.assertIn("tbf rate 300mbit buffer 375000 limit 1875000", commands[2])
        self.assertIn("parent 10:1 handle 20: fq_codel memory_limit 937500 target 5ms interval 100ms", commands[3])
        self.assertEqual(len(commands), 5)
        self.assertIn("qdisc show dev eth1", commands[-1])

    def test_counters_read_the_fair_queue_behind_the_rate_limiter(self):
        # The fair queue holds the backlog and makes the drops, so its drops are the queue drops and its
        # backlog is counted once, not again inside the rate limiter.
        lines = [
            "qdisc netem 1: root refcnt 2 limit 1000 delay 15ms loss 0.1%",
            " Sent 1000000 bytes 9000 pkt (dropped 120, overlimits 0 requeues 0)",
            " backlog 0b 0p requeues 0",
            "qdisc tbf 10: parent 1:1 rate 300Mbit burst 375000b lat 25ms",
            " Sent 990000 bytes 8900 pkt (dropped 35, overlimits 5000 requeues 0)",
            " backlog 40Kb 30p requeues 0",
            "qdisc fq_codel 20: parent 10:1 limit 10240p memory_limit 937500b target 5ms interval 100ms",
            " Sent 980000 bytes 8880 pkt (dropped 35, overlimits 0 requeues 0)",
            " backlog 36Kb 25p requeues 0",
        ]
        output = chr(10).join(lines) + chr(10)
        with patch.object(netem, "run_cmd", return_value=(0, output, "")):
            value = netem.qdisc_counters("eth1")
        self.assertIn("intentional_delay", value.pop("queues"))
        self.assertEqual(value.pop("queue_accounting")["backlog_basis"], "rate queue only")
        self.assertEqual(value, {"kind": "netem", "generation": netem.QDISC_GENERATIONS.get("eth1", 0), "packets": 9000, "netem_drops": 120, "queue_drops": 35,
                                 "backlog_bytes": 36 * 1024, "shaped": True})


if __name__ == "__main__":
    unittest.main()
