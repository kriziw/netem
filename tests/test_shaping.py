"""Queue sizing for the impairment and rate-limiter qdiscs."""
import unittest
from unittest.mock import patch

import app as netem


class ShaperSizingTests(unittest.TestCase):
    def test_queues_scale_with_rate_and_delay(self):
        self.assertEqual(netem.shaper_queue_sizes(300, 15, 0), (146485, 375000, 150000000))
        self.assertEqual(netem.shaper_queue_sizes(100, 5, 0), (48829, 125000, 50000000))
        # The generous floor holds ordinary bursts and long-delay in-flight packets.
        self.assertEqual(netem.shaper_queue_sizes(100, 300, 0)[0], 48829)
        self.assertEqual(netem.shaper_queue_sizes(1, 0, 0), (8192, 3200, 8388608))
        # Unshaped links also receive the generous packet guard.
        self.assertEqual(netem.shaper_queue_sizes(0, 20, 5)[0], 488282)

    def test_applied_commands_use_the_sizes(self):
        commands = []
        with patch.object(netem, "run_cmd", side_effect=lambda cmd: commands.append(cmd) or (0, "", "")):
            self.assertEqual(netem.apply_netem("eth1", 15, 0, 0.1, 300), (True, "OK"))
        self.assertIn("netem limit 146485 delay 15.0ms loss 0.100%", commands[1])
        self.assertIn("tbf rate 300mbit buffer 375000 limit 300000000", commands[2])
        self.assertIn("parent 10:1 handle 20: fq_codel limit 146485 memory_limit 150000000 target 5ms interval 100ms", commands[3])
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


class QueueResourceTests(unittest.TestCase):
    def test_low_rate_guards_are_generous_and_independent_of_wire_bytes(self):
        for rate in (1, 18, 42, 100, 300):
            packets, burst, memory = netem.shaper_queue_sizes(rate, 40, 5)
            self.assertGreaterEqual(packets, 8192)
            self.assertGreaterEqual(memory, 8 * 1024 * 1024)
            self.assertGreaterEqual(burst, 3200)

    def test_queue_resource_budget_scales_per_direction_with_bounded_guards(self):
        down_packets, down_memory = netem.shaper_queue_limits(42)
        up_packets, up_memory = netem.shaper_queue_limits(18)
        self.assertEqual((down_packets, down_memory), (20508, 21000000))
        self.assertEqual((up_packets, up_memory), (8790, 9000000))
        self.assertGreater(down_packets, up_packets)
        self.assertGreater(down_memory, up_memory)
        self.assertEqual(netem.shaper_queue_limits(100000)[1], 512 * 1024 * 1024)
        self.assertEqual(netem.shaper_queue_limits(100000)[0], 1048576)

    def test_drop_reasons_do_not_double_count_memory_as_packet_limit(self):
        def counter(packets, total, limit, memory):
            return {'kind': 'netem', 'generation': 1, 'packets': packets,
                    'netem_drops': total + 10, 'queue_drops': total, 'backlog_bytes': 0,
                    'queue_accounting': {'fair_queue_drops': {
                        'drops': total, 'drop_overlimit': limit, 'drop_overmemory': memory}}}
        first = counter(100, 10, 6, 4)
        last = counter(300, 30, 18, 12)
        rates = netem.qdisc_rates(last, first, 2)
        self.assertEqual(rates['queue_drops_ps'], 10)
        self.assertEqual(rates['queue_memory_drops_ps'], 4)
        self.assertEqual(rates['queue_packet_drops_ps'], 2)
        self.assertEqual(rates['queue_other_drops_ps'], 4)
        self.assertEqual(sum(rates[key] for key in ('queue_memory_drops_ps', 'queue_packet_drops_ps', 'queue_other_drops_ps')), rates['queue_drops_ps'])
        first['queue_accounting']['fair_queue_drops']['drop_overmemory'] = None
        self.assertNotIn('queue_memory_drops_ps', netem.qdisc_rates(last, first, 2))
