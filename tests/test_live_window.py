import time
import unittest
from unittest.mock import patch

import app as netem


class LiveWindowTests(unittest.TestCase):
    def test_start_window_validation_and_form_controls(self):
        self.assertEqual(netem.requested_live_window(), 15)
        self.assertEqual(netem.requested_live_window('60'), 60)
        for value in (True, 4, 121, 15.5, 'nan', 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                netem.requested_live_window(value)
        for name in ('_scenario_card.html', '_site_scenario.html'):
            self.assertIn('_live_window_field.html', netem.app.jinja_env.loader.get_source(netem.app.jinja_env, name)[0])

    def test_phase_identity_changes_only_with_phase_and_ack_is_required(self):
        state = dict(active=True, workload_supervised=True, workload_run_id='run-a',
                     scenario_id='test-a', started_at=100, phase_index=0, phase='Baseline', live_window_seconds=15)
        with patch.dict(netem.SCENARIO_STATE, state, clear=True):
            before = netem.test_live_context()
            self.assertEqual(netem.test_live_context(), before)
            netem.SCENARIO_STATE['phase_index'] = 1
            after = netem.test_live_context()
            self.assertNotEqual(before['phase_id'], after['phase_id'])
            with patch.object(netem, 'traffic_generator_request', return_value={'status': 'running', 'users': 2, 'run': {'run_id': 'run-a'}, 'dem': {'live_phase_id': after['phase_id']}}) as api:
                netem.publish_test_live_context()
                self.assertEqual(api.call_args.kwargs['payload']['live_context']['window_seconds'], 15)
            with patch.object(netem, 'traffic_generator_request', return_value={'status': 'running', 'users': 2, 'run': {'run_id': 'run-a'}, 'dem': {'live_phase_id': before['phase_id']}}):
                with self.assertRaises(RuntimeError):
                    netem.publish_test_live_context()

    def test_previous_phase_cached_snapshot_cannot_show_its_score(self):
        state = dict(active=True, workload_supervised=True, workload_run_id='run-a',
                     scenario_id='test-a', started_at=100, phase_index=1, phase='Degraded', live_window_seconds=15)
        old = {'run': {'run_id': 'run-a'}, 'dem': {'live_phase_id': 'old', 'requests': 100, 'experience_score': 100}}
        with patch.dict(netem.SCENARIO_STATE, state, clear=True), \
             patch.object(netem, 'load_config', return_value={}), \
             patch.object(netem, 'traffic_generator_config', return_value={'host': 'sim'}), \
             patch.object(netem, 'traffic_generator_api_key', return_value='key'), \
             patch.object(netem, 'traffic_generator_request', return_value=old):
            dem = netem.traffic_generator_snapshot()['status']['dem']
            self.assertEqual(dem['requests'], 0)
            self.assertIn('live_phase_pending', dem['evidence_issues'])
            self.assertNotIn('experience_score', dem)

    def test_short_live_window_does_not_change_full_phase_report_boundaries(self):
        recorder = dict(active=True, live_window_seconds=15,
                        phase_marks=[{'name': 'Degradation', 'at': 100}], phase_ends=[], pauses=[])
        with patch.dict(netem.TEST_RECORDER, recorder, clear=True), \
             patch.dict(netem.SCENARIO_STATE, {'live_window_seconds': 15}), \
             patch.object(netem.time, 'time', return_value=160), \
             patch.object(netem, 'phase_window_dem', return_value={'requests': 120}) as query:
            netem.recorder_close_phase()
            self.assertEqual(query.call_args.args, (60,))
            self.assertEqual(query.call_args.kwargs, {'since': 100, 'until': 160})
            self.assertEqual(netem.TEST_RECORDER['phase_ends'][0]['dem']['requests'], 120)
