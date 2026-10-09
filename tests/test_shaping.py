"""Queue sizing for the impairment and rate-limiter qdiscs."""
import unittest
from unittest.mock import patch

import app as netem


class ShaperSizingTests(unittest.TestCase):
    def test_queues_scale_with_rate_and_delay(self):
        self.assertEqual(netem.shaper_queue_sizes(300, 15, 0), (1000, 37500, 1_875_000))
        self.assertEqual(netem.shaper_queue_sizes(100, 5, 0), (1000, 12500, 625_000))
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
        self.assertIn("tbf rate 300mbit buffer 37500 limit 1875000", commands[2])


if __name__ == "__main__":
    unittest.main()
