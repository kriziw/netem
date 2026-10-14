import unittest
from unittest.mock import patch
import app as netem
import calibration

class PhaseEvidenceTests(unittest.TestCase):
    def test_paused_phase_queries_only_active_segments(self):
        state = {"active": True, "phase_marks": [{"name": "phase", "at": 100}], "phase_ends": [],
                 "pauses": [{"start": 102, "end": 104}]}
        with patch.dict(netem.TEST_RECORDER, state, clear=True), patch.object(netem.time, "time", return_value=106), \
             patch.object(netem, "phase_window_dem", return_value={"scoring_complete": True}) as query:
            netem.recorder_close_phase()
        self.assertEqual(query.call_args.kwargs["intervals"], [[100, 102], [104, 106]])
