import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app as netem
from component_updates import ComponentChecks
from test_unified_updates import SIMULATOR, STATUS


def agent(job=None, busy=False):
    state = copy.deepcopy(SIMULATOR)
    state['system'].update(job=job or {}, busy=busy)
    return state, None


class CheckCoordinatorTests(unittest.TestCase):
    def coordinator(self, states):
        owner = SimpleNamespace(load_config=lambda: {}, traffic_generator_config=lambda cfg: {'host': 'agent', 'port': 8443},
            git_update_status=Mock(return_value=STATUS), simulator_update_state=Mock(side_effect=states),
            traffic_generator_request=Mock(side_effect=[{'job_id': 'sim'}, {'job_id': 'target'}]))
        return ComponentChecks(owner), owner

    def test_all_components_check_and_wait_for_exact_job_before_next_command(self):
        checks, owner = self.coordinator([agent(), agent({'id': 'old', 'state': 'completed'}, busy=True),
            agent({'id': 'sim', 'state': 'completed'}), agent(), agent({'id': 'target', 'state': 'completed'})])
        with patch('component_updates.time.sleep') as pause:
            checks.run(('agent', 8443))
        owner.git_update_status.assert_called_once_with(fetch=True)
        self.assertEqual([call.kwargs['payload']['action'] for call in owner.traffic_generator_request.call_args_list],
                         ['check_update', 'target_check'])
        pause.assert_called_once_with(1)
        self.assertEqual([item['state'] for item in checks.snapshot()['components'].values()], ['complete'] * 3)
        self.assertFalse(checks.snapshot()['running'])

    def test_one_component_error_does_not_skip_the_other_checks(self):
        checks, owner = self.coordinator([agent(), agent({'id': 'sim', 'state': 'failed', 'message': 'GitHub unavailable'}),
                                         agent(), agent({'id': 'target', 'state': 'completed'})])
        owner.git_update_status.return_value = {'ok': False, 'error': 'Git unavailable'}
        checks.run(('agent', 8443))
        state = checks.snapshot()['components']
        self.assertEqual(state['netem']['state'], 'error')
        self.assertEqual(state['simulator']['message'], 'GitHub unavailable')
        self.assertEqual(state['target']['state'], 'complete')

    def test_disconnected_simulator_and_connection_changes_do_not_enqueue(self):
        checks, owner = self.coordinator([({}, 'Agent offline')] * 2)
        checks.run(('agent', 8443))
        owner.traffic_generator_request.assert_not_called()
        self.assertEqual(checks.snapshot()['components']['netem']['state'], 'complete')
        self.assertEqual(checks.snapshot()['components']['target']['state'], 'error')
        checks.run(('previous-agent', 8443))
        owner.traffic_generator_request.assert_not_called()
        self.assertIn('connection changed', checks.snapshot()['components']['simulator']['message'])

    def test_duplicate_clicks_do_not_start_competing_check_workers(self):
        checks, _ = self.coordinator([])
        with patch('component_updates.threading.Thread') as worker:
            self.assertTrue(checks.start())
            self.assertFalse(checks.start())
        worker.return_value.start.assert_called_once()

    def test_wait_times_out_instead_of_treating_an_unrelated_job_as_success(self):
        checks, _ = self.coordinator([agent({'id': 'other', 'state': 'completed'})])
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            checks.wait_agent(('agent', 8443), 'expected', timeout=0)


class ComponentUpdateRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'

    def test_shared_button_starts_check_all_and_requires_the_form_token(self):
        with patch.object(netem.COMPONENT_CHECKS, 'start') as start:
            self.assertEqual(self.client.post('/updates', data={'action': 'check'}).status_code, 400)
            start.assert_not_called()
            response = self.client.post('/updates', data={'action': 'check', 'integration_csrf': 'token'})
            self.assertEqual(response.status_code, 302)
            start.assert_called_once()

    def test_poll_refreshes_badges_install_buttons_and_exact_checked_tags(self):
        snapshot = {'running': False, 'components': {name: {'state': 'complete'} for name in ('netem', 'simulator', 'target')}}
        with patch.object(netem, 'git_update_status', return_value=STATUS), \
             patch.object(netem, 'simulator_update_state', return_value=agent()), \
             patch.object(netem.COMPONENT_CHECKS, 'snapshot', return_value=snapshot):
            response = self.client.get('/updates/components')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json['html'].count('>Update available</span>'), 3)
        self.assertNotIn('disabled', response.json['html'])
        self.assertIn('name="tag" value="v0.17.0"', response.json['html'])
        self.assertIn('name="tag" value="v0.9.2"', response.json['html'])

    def test_pending_checks_block_installs_on_server_and_mark_all_cards(self):
        snapshot = {'running': True, 'components': {name: {'state': 'checking'} for name in ('netem', 'simulator', 'target')}}
        with patch.object(netem, 'git_update_status', return_value=STATUS), \
             patch.object(netem, 'simulator_update_state', return_value=agent()), \
             patch.object(netem.COMPONENT_CHECKS, 'snapshot', return_value=snapshot), \
             patch.object(netem, 'traffic_generator_request') as api, patch.object(netem, 'run_process') as git:
            response = self.client.get('/updates/components')
            self.assertEqual(response.json['html'].count('>Checking…</span>'), 3)
            self.assertEqual(response.json['html'].count('disabled'), 3)
            self.client.post('/updates', data={'action': 'update'})
            self.client.post('/settings/traffic/maintenance/system', data={'integration_csrf': 'token',
                'return_to': 'updates', 'action': 'target_install', 'tag': 'v0.9.2'})
            api.assert_not_called()
            git.assert_not_called()
