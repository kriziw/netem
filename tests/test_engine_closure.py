import unittest
import calibration
import app as netem
from unittest.mock import patch


class DemandEvidenceTests(unittest.TestCase):
    def test_missing_and_insufficient_demand_cannot_establish_capacity(self):
        args = (100, 300, 98, 0, "ok", 0)
        self.assertEqual(calibration.judge_step(*args, require_demand=True)["status"], "fail")
        evidence = {"complete": True, "offered_mbps": 100, "started_mbps": 50, "tolerance_mbps": 5}
        result = calibration.judge_step(*args, require_demand=True, demand=evidence)
        self.assertEqual(result["status"], "fail")
        self.assertIn("generator demand", calibration.limited_by(result))

    def test_demand_uses_monotonic_counter_deltas_and_rejects_resets(self):
        first = {"timestamp_monotonic": 100, "directions": {"down": dict(target_mbps=8,
                 offered_bytes=100, admitted_bytes=100, started_bytes=100, delivered_bytes=100)}}
        last = {"timestamp_monotonic": 130, "directions": {"down": dict(target_mbps=8,
                offered_bytes=30000100, admitted_bytes=30000100, started_bytes=15000100, delivered_bytes=10000100)}}
        evidence = calibration.demand_window(first, last)
        self.assertEqual(evidence["offered_mbps"], 8)
        self.assertEqual(evidence["started_mbps"], 4)
        last["directions"]["down"]["started_bytes"] = 0
        self.assertFalse(calibration.demand_window(first, last)["complete"])

    def test_paused_phase_queries_only_active_segments(self):
        state = {"active": True, "phase_marks": [{"name": "phase", "at": 100}], "phase_ends": [],
                 "pauses": [{"start": 102, "end": 104}]}
        with patch.dict(netem.TEST_RECORDER, state, clear=True), patch.object(netem.time, "time", return_value=106), \
             patch.object(netem, "phase_window_dem", return_value={"scoring_complete": True}) as query:
            netem.recorder_close_phase()
        self.assertEqual(query.call_args.kwargs["intervals"], [[100, 102], [104, 106]])

    def test_verified_same_hierarchy_changes_without_delete(self):
        previous = {"values": (5, 1, 1, 100, 0, 0, 0, 0), "observed": "qdisc netem"}
        with patch.dict(netem.APPLIED_QDISCS, {"test": previous}, clear=True), \
             patch.object(netem, "run_cmd", return_value=(0, "qdisc netem", "")) as command, \
             patch.object(netem, "clear_qdisc") as clear:
            self.assertTrue(netem.apply_netem("test", 40, 5, 3, 18)[0])
        clear.assert_not_called()
        self.assertEqual(sum("qdisc change" in call.args[0] for call in command.call_args_list), 3)
