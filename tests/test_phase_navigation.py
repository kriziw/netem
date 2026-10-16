import copy
import time
import unittest
from unittest.mock import patch

import app as netem


class PhaseNavigationTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(active=True, scenario_id='test', started_at=100, phase='Baseline', phase_index=0,
                          phase_revision=0, phase_jump=None, navigating=False, paused=False,
                          phases=[{'name': name, 'planned_s': 60} for name in ('Baseline', 'Outage', 'Recovery')])
        for mock in (patch.dict(netem.SCENARIO_STATE, self.state, clear=True),
                     patch.dict(netem.TEST_RECORDER, {'active': False}, clear=True),
                     patch.object(netem, 'log_event')):
            mock.start()
            self.addCleanup(mock.stop)
        netem.SCENARIO_STOP.clear()
        netem.SCENARIO_PAUSE.clear()
        self.addCleanup(netem.SCENARIO_STOP.clear)
        self.addCleanup(netem.SCENARIO_PAUSE.clear)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as session:
            session['integration_csrf'] = 'navigation-token'

    def post(self, direction='next', **extra):
        data = dict(integration_csrf='navigation-token', direction=direction, scenario_id='test', started_at='100', phase_index='0', phase_revision='0')
        data.update(extra)
        return self.client.post('/lab/scenario/phase', data=data)

    def test_admission_checks_bounds_stale_state_and_duplicate_requests(self):
        self.assertEqual(self.post('previous').status_code, 409)
        self.assertEqual(self.post(phase_index='1').status_code, 409)
        self.assertEqual(self.post(started_at='99').status_code, 409)
        self.assertEqual(self.post(phase_revision='1').status_code, 409)
        self.assertEqual(self.post('invalid').status_code, 400)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(netem.SCENARIO_STATE['phase_jump'], 1)
        self.assertEqual(self.post().status_code, 409)

    def test_intro_preparation_stop_and_transition_do_not_accept_navigation(self):
        for key in ('intro', 'preparing_workload', 'navigating', 'finishing'):
            with patch.dict(netem.SCENARIO_STATE, {key: True}):
                self.assertEqual(self.post().status_code, 409)
        netem.SCENARIO_STOP.set()
        self.assertEqual(self.post().status_code, 409)

    def test_waits_are_interruptible_but_stop_takes_priority(self):
        netem.SCENARIO_STATE['phase_jump'] = 1
        with self.assertRaises(netem.PhaseNavigation):
            netem.scenario_sleep(60)
        with self.assertRaises(netem.PhaseNavigation):
            netem.wait_for_scenario_condition({'type': 'sla'}, 'wan1', 60, 5)
        netem.SCENARIO_STOP.set()
        self.assertTrue(netem.scenario_sleep(60))

    def test_runner_can_skip_forward_then_return_to_an_earlier_phase(self):
        scenario = {'id': 'test', 'name': 'Navigation test', 'steps': [
            {'after': 0, 'action': 'quality', 'value': 100, 'phase': 'Baseline'},
            {'after': 60, 'action': 'fault', 'value': 'blackhole', 'phase': 'Outage'},
            {'after': 60, 'action': 'fault', 'value': 'normal', 'phase': 'Recovery'},
            {'after': 60, 'action': 'phase', 'phase': 'Recovery'}]}
        cfg = {'wan_links': [{'id': 'wan1', 'name': 'WAN1', 'inner': 'eth1', 'outer': 'eth2', 'quality': 100}]}
        waits, changes, phases = [], [], []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) in (2, 4):
                netem.SCENARIO_STATE['phase_jump'] = 2 if len(waits) == 2 else 1
                raise netem.PhaseNavigation()
            return False
        def note(kind, item):
            if kind == 'manual_navigation':
                changes.append(item)
            if kind == 'phase_marks':
                phases.append(item['name'])
        with patch.object(netem, 'load_config', return_value=cfg), \
             patch.object(netem, 'scenario_sleep', side_effect=wait), \
             patch.object(netem, 'apply_selected_profile', return_value=(True, 'ok', {})) as profile, \
             patch.object(netem, 'apply_mtu_limit', return_value=(True, 'ok')), \
             patch.object(netem, 'apply_runtime_fault', return_value=(True, 'ok')) as fault, \
             patch.object(netem, 'recorder_start'), patch.object(netem, 'recorder_close_phase'), \
             patch.object(netem, 'recorder_finish') as finish, \
             patch.object(netem, 'recorder_note', side_effect=note):
            netem.run_scenario('wan1', scenario)
        self.assertEqual([item['to'] for item in changes], ['Recovery', 'Outage'])
        self.assertEqual(phases, ['Baseline', 'Recovery', 'Outage', 'Recovery'])
        self.assertEqual([call.args[1] for call in fault.call_args_list], ['normal', 'blackhole', 'blackhole', 'normal'])
        self.assertEqual(finish.call_args.args[0], 'passed')
        self.assertGreaterEqual(profile.call_count, 4)
        self.assertEqual(waits[2], 0)  # Jump skips the delay belonging to the previous phase.
        self.assertFalse(netem.SCENARIO_STATE['active'])

    def test_revisiting_a_phase_resets_live_identity_and_resumes_a_pause(self):
        original = {'id': 'wan1'}
        steps = [{'action': 'phase', 'phase': 'Baseline'}, {'action': 'phase', 'phase': 'Outage'}]
        before = netem.test_live_context()['phase_id']
        netem.SCENARIO_STATE.update(phase_jump=0, paused=True, paused_clock=time.monotonic()-3, paused_total_s=0)
        netem.SCENARIO_PAUSE.set()
        with patch.object(netem, 'restore_phase_prefix', return_value=original), patch.object(netem, 'recorder_close_phase'):
            index, _ = netem.accept_phase_navigation(original, steps, [0, 1], {})
        netem.SCENARIO_STATE['phase_index'] = 0
        self.assertNotEqual(before, netem.test_live_context()['phase_id'])
        self.assertEqual(index, 1)
        self.assertFalse(netem.SCENARIO_PAUSE.is_set())
        self.assertFalse(netem.SCENARIO_STATE['paused'])
        self.assertGreaterEqual(netem.SCENARIO_STATE['paused_total_s'], 3)

    def test_revisited_phase_windows_are_not_merged(self):
        dem = lambda score: {'requests': 100, 'scoring_complete': True, 'experience': score,
                              'success': 100, 'interactive_p95_ms': 100}
        record = {'name': 'Manual', 'started_at': 100, 'phases': ['Baseline', 'Outage'],
            'phase_marks': [{'name': 'Baseline', 'at': 100}, {'name': 'Outage', 'at': 110}, {'name': 'Baseline', 'at': 120}],
            'phase_ends': [{'name': 'Baseline', 'started_at': 100, 'dem': dem(90)},
                           {'name': 'Outage', 'started_at': 110, 'dem': dem(80)},
                           {'name': 'Baseline', 'started_at': 120, 'dem': dem(100)}],
            'manual_navigation': [{'at': 120, 'from': 'Outage', 'to': 'Baseline'}]}
        result = netem.summarize_test(record, 'passed', None, ended_at=130)
        self.assertEqual([phase['experience_score'] for phase in result['phases']], [90, 80, 100])
        self.assertNotEqual(result['experience_result'], 'pass')
        self.assertTrue(result['manual_navigation'])
        self.assertIn('Manually navigated test', ' '.join(result['conclusion']))


if __name__ == '__main__':
    unittest.main()
