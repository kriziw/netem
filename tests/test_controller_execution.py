import unittest
from unittest.mock import patch
import app as netem


class ControllerExecutionTests(unittest.TestCase):
    def setUp(self):
        netem.reset_scenario_state(None, None)
        netem.SCENARIO_STATE['active'] = True
        self.addCleanup(netem.reset_scenario_state, None, None)
        self.cfg = {'wan_links': [{'id': 'wan1', 'name': 'WAN1', 'inner': 'ens19', 'outer': 'ens20', 'preset': 'dia', 'quality': 100}]}
        self.scenario = {'id': 'manual', 'name': 'Progressive brownout', 'auto_workload': True,
                         'controller_managed': True, 'steps': [{'after': 0, 'action': 'quality', 'value': 40, 'phase': 'Baseline'}]}
        for name, value in (('load_config', self.cfg), ('ensure_traffic_path', {}), ('simulator_catalog', None),
                            ('active_site_plan', {'start': {'operation': 'start', 'users': 87, 'personas': {'knowledge_worker': 87}}}),
                            ('apply_selected_profile', (True, 'OK', {})), ('apply_mtu_limit', (True, 'OK')),
                            ('recorder_start', None), ('recorder_note', None), ('recorder_finish', None), ('log_event', None)):
            mock = patch.object(netem, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    @staticmethod
    def running(run_id='owned', users=10):
        return {'status': 'running', 'users': users, 'run': {'run_id': run_id}}

    def test_manual_test_starts_site_workload_before_impairments_and_stops_only_owned_run(self):
        events = []
        current = {'status': 'idle', 'users': 0, 'run': None}
        def api(path, **kwargs):
            nonlocal current
            events.append(path)
            if path.endswith('/start'):
                self.assertEqual(kwargs['payload']['users'], 87)
                self.assertEqual(kwargs['payload']['controller_lease_seconds'], 30)
                self.assertNotIn('operation', kwargs['payload'])
                self.assertTrue(netem.SCENARIO_STATE['preparing_workload'])
                self.assertEqual(netem.scenario_snapshot()['elapsed_s'], 0)
                current = self.running()
            if path.endswith('/stop'):
                self.assertEqual(kwargs['payload'], {'run_id': 'owned'})
                current = {'status': 'stopped'}
            return current
        self.apply_selected_profile.side_effect = lambda *args: events.append('impairment') or (True, 'OK', {})
        with patch.object(netem, 'traffic_generator_request', side_effect=api):
            netem.run_scenario('wan1', self.scenario)
        self.assertLess(events.index('/api/v1/workloads/start'), events.index('impairment'))
        self.assertIn('/api/v1/controller/heartbeat', events)
        self.assertIn('/api/v1/workloads/stop', events)
        self.assertEqual(netem.SCENARIO_STATE['result'], 'passed')

    def test_existing_workload_is_borrowed_and_left_running(self):
        with patch.object(netem, 'traffic_generator_request', return_value=self.running('existing')) as api:
            netem.run_scenario('wan1', self.scenario)
        self.assertNotIn('/api/v1/workloads/start', [call.args[0] for call in api.call_args_list])
        self.assertNotIn('/api/v1/workloads/stop', [call.args[0] for call in api.call_args_list])
        self.assertEqual(netem.SCENARIO_STATE['result'], 'passed')

    def test_failed_start_does_not_advance_clock_or_apply_any_impairment(self):
        with patch.object(netem, 'traffic_generator_request', side_effect=[{'status': 'idle'}, RuntimeError('executor offline')]):
            netem.run_scenario('wan1', self.scenario)
        self.apply_selected_profile.assert_not_called()
        self.recorder_start.assert_not_called()
        self.assertEqual(netem.SCENARIO_STATE['result'], 'failed')
        self.assertIn('offline', netem.SCENARIO_STATE['error'])

    def test_lost_executor_fails_and_does_not_stop_replacement_run(self):
        responses = [self.running(), {'status': 'stopped'}]
        with patch.object(netem, 'traffic_generator_request', side_effect=responses) as api:
            netem.run_scenario('wan1', self.scenario)
        self.assertEqual(netem.SCENARIO_STATE['result'], 'failed')
        self.assertNotIn('/api/v1/workloads/stop', [call.args[0] for call in api.call_args_list])
        self.apply_selected_profile.assert_called_once()  # Baseline restored, no failed-phase impairment.

    def test_executor_wait_requires_active_users_and_stop_is_interruptible(self):
        with patch.object(netem, 'traffic_generator_request', return_value=self.running(users=5)) as api:
            netem.wait_for_test_executor(self.running(users=0), 'owned')
        self.assertEqual(api.call_args.args[0], '/api/v1/controller/heartbeat')
        netem.SCENARIO_STOP.set()
        with self.assertRaises(InterruptedError):
            netem.wait_for_test_executor(self.running(users=0), 'owned')

    def test_failed_restoration_cannot_report_a_pass(self):
        self.apply_selected_profile.side_effect = [(True, 'OK', {}), (False, 'bridge missing', {})]
        with patch.object(netem, 'traffic_generator_request', return_value=self.running('existing')):
            netem.run_scenario('wan1', self.scenario)
        self.assertEqual(netem.SCENARIO_STATE['result'], 'failed')
        self.assertIn('WAN restoration failed', netem.SCENARIO_STATE['error'])

    def test_start_buttons_share_the_controller_managed_runner(self):
        with patch.object(netem, 'get_scenarios', return_value=[self.scenario]), patch.object(netem, 'session_snapshot', return_value={'active': True}), \
             patch.object(netem.threading, 'Thread') as thread:
            netem.SCENARIO_STATE['active'] = False
            netem.app.test_client().post('/lab/scenario/start', data={'link_id': 'wan1', 'scenario_id': 'manual'})
        scenario = thread.call_args.kwargs['args'][1]
        self.assertTrue(scenario['auto_workload'] and scenario['controller_managed'])


class ControllerScreenTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'

    def test_legacy_bookmarks_go_to_native_pages_without_agent_html(self):
        with patch.object(netem, 'traffic_generator_request') as api:
            response = self.client.get('/simulator/settings/network-setup')
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, '/settings/traffic/network')
            self.assertEqual(self.client.post('/simulator/settings/network-setup').status_code, 405)
        api.assert_not_called()

    def test_native_commands_require_csrf_and_active_tests_cannot_be_overridden(self):
        with patch.object(netem, 'traffic_generator_request', return_value={}) as api:
            self.assertEqual(self.client.post('/settings/traffic/network/network-setup').status_code, 400)
            with patch.object(netem, 'scenario_snapshot', return_value={'active': True}):
                response = self.client.post('/traffic-generator/stop', data={'integration_csrf': 'token'})
                self.assertEqual(response.status_code, 302)
        api.assert_not_called()
