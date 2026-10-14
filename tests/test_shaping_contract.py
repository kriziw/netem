import unittest
from unittest.mock import patch
import app as netem
import calibration

class ShapingTests(unittest.TestCase):
    def test_unsupported_jitter_is_rejected_before_mutation(self):
        with patch.object(netem, "run_cmd") as run:
            self.assertFalse(netem.apply_netem("eth1", 0, 30, 0, 100)[0])
        run.assert_not_called()

    def test_identical_verified_profile_preserves_queues(self):
        values = (5, 1, 0, 100, 0, 0, 0, 0)
        with patch.dict(netem.APPLIED_QDISCS, {"eth1": {"values": values, "observed": "qdisc netem"}}, clear=True), \
             patch.object(netem, "run_cmd", return_value=(0, "qdisc netem", "")) as run:
            self.assertTrue(netem.apply_netem("eth1", *values)[0])
        self.assertEqual(run.call_count, 1)
        self.assertIn("qdisc show", run.call_args.args[0])

    def test_counters_are_not_differenced_across_a_qdisc_generation(self):
        previous = {"kind": "netem", "generation": 1, "packets": 10, "netem_drops": 1, "queue_drops": 0, "backlog_bytes": 0}
        current = dict(previous, generation=2, packets=100)
        self.assertIsNone(netem.qdisc_rates(current, previous, 1))

    def test_partial_failure_restores_old_profile_or_exposes_unknown(self):
        old = {"delay_ms": 5, "jitter_ms": 1, "loss_pct": 0, "download_mbit": 100, "upload_mbit": 50}
        link = {"id": "wan1", "inner": "in", "outer": "out"}
        for restored in (True, False):
            with self.subTest(restored=restored), patch.dict(netem.RUNTIME_EFFECTIVE, {"wan1": {"effective": old}}, clear=True), \
                 patch.object(netem, "get_effective_profile", return_value=dict(old, delay_ms=40)), \
                 patch.object(netem, "apply_netem", side_effect=[(True, "OK"), (False, "upload failed"), (restored, "rollback"), (restored, "rollback")]):
                self.assertFalse(netem.apply_selected_profile(link, {})[0])
                state = netem.RUNTIME_EFFECTIVE["wan1"]
                self.assertEqual(state["apply_status"], "rolled_back" if restored else "unknown")
                self.assertEqual(state["effective"], old if restored else {})


class CalibrationHostTests(unittest.TestCase):
    def test_missing_host_evidence_cannot_pass_calibration(self):
        self.assertEqual(calibration.judge_step(100, 100, 100, 0, "unknown", 0)["status"], "fail")


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

    def test_verified_same_hierarchy_changes_without_delete(self):
        previous = {"values": (5, 1, 1, 100, 0, 0, 0, 0), "observed": "qdisc netem"}
        with patch.dict(netem.APPLIED_QDISCS, {"test": previous}, clear=True), \
             patch.object(netem, "run_cmd", return_value=(0, "qdisc netem", "")) as command, \
             patch.object(netem, "clear_qdisc") as clear:
            self.assertTrue(netem.apply_netem("test", 40, 5, 3, 18)[0])
        clear.assert_not_called()
        self.assertEqual(sum("qdisc change" in call.args[0] for call in command.call_args_list), 3)
