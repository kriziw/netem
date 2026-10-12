import copy
import unittest
from unittest.mock import patch

import app as netem


STATE = {
    'version': '0.16.0',
    'system': {'busy': False, 'workload_active': False, 'ready': True, 'job': {}, 'selected': {},
               'release': {}, 'target': {}, 'interface_configs': {}, 'inventory': {}},
    'network': {'management': 'eth0', 'interfaces': [{'interface': 'eth2', 'addresses': ['10.250.1.100/24']}],
                'paths': [], 'leases': {}, 'probe': {}, 'cards': [
                    {'role': 'wan1', 'title': 'WAN1 calibration', 'connection': 'Through NetEm WAN1',
                     'name': 'WAN1 calibration', 'interface': 'eth2', 'address': '10.250.1.100/24',
                     'gateway': '10.250.1.1', 'target': '198.18.0.1', 'bridge': 'vmbr1', 'wan_id': 'wan1',
                     'id': 'a' * 32, 'path': None}]},
    'experience': {'applications': {'voice': {'label': 'Voice'}}, 'policy': {'voice': {
        'critical': True, 'deadline_ms': 150, 'jitter_max_ms': 30, 'loss_max_pct': 2,
        'min_down_mbps': 0, 'min_up_mbps': 0, 'min_sustained_down_mbps': 0, 'min_sustained_up_mbps': 0}}},
}


class NativeTrafficControlsTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'

    def test_native_settings_use_netem_shell_and_only_json_agent_data(self):
        with patch.object(netem, 'traffic_generator_request', return_value=copy.deepcopy(STATE)) as api, \
             patch.object(netem.APPLIANCE_PLATFORM.store, 'state', return_value={}), \
             patch.object(netem, 'scenario_snapshot', return_value={'active': False}):
            for section in ('network', 'experience', 'maintenance'):
                response = self.client.get('/settings/traffic/' + section)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertIn(b'Command Center', response.data)
                self.assertIn(b'data-native-traffic', response.data)
                self.assertNotIn(b'/simulator/static', response.data)
                self.assertNotIn(b'<iframe', response.data)
                self.assertIn(b'/settings/traffic/', response.data)
            self.assertIn(b'Saved', self.client.get('/settings/traffic/network').data)
        self.assertTrue(all(call.args[0] == '/api/v1/controller/settings' for call in api.call_args_list))

    def test_save_unverified_path_submits_data_and_preserves_warning(self):
        with patch.object(netem, 'traffic_generator_request', return_value={'message': 'Interface saved with warning.'}) as api, \
             patch.object(netem, 'scenario_snapshot', return_value={'active': False}), patch.object(netem, 'log_event'):
            reply = self.client.post('/settings/traffic/network/network-setup', data={
                'integration_csrf': 'token', 'role': 'appliance', 'action': 'save_interface'})
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(api.call_args.args[0], '/api/v1/controller/commands/network-setup')
        self.assertNotIn('integration_csrf', api.call_args.kwargs['payload'])

    def test_validation_keeps_entered_fields_and_does_not_render_agent_html(self):
        with patch.object(netem, 'traffic_generator_request', side_effect=[RuntimeError('Invalid address'), copy.deepcopy(STATE)]), \
             patch.object(netem.APPLIANCE_PLATFORM.store, 'state', return_value={}), \
             patch.object(netem, 'scenario_snapshot', return_value={'active': False}):
            reply = self.client.post('/settings/traffic/network/network-setup', data={
                'integration_csrf': 'token', 'role': 'wan1', 'address': 'my-entered-address'})
        self.assertEqual(reply.status_code, 400)
        self.assertIn(b'Invalid address', reply.data)
        self.assertIn(b'value="my-entered-address"', reply.data)

    def test_configuration_cannot_override_an_active_program(self):
        with patch.object(netem, 'scenario_snapshot', return_value={'active': True}), \
             patch.object(netem, 'traffic_generator_request', return_value=copy.deepcopy(STATE)) as api, \
             patch.object(netem.APPLIANCE_PLATFORM.store, 'state', return_value={}):
            reply = self.client.post('/settings/traffic/maintenance/system', data={
                'integration_csrf': 'token', 'action': 'install_update'})
        self.assertEqual(reply.status_code, 400)
        self.assertTrue(all(call.kwargs.get('method', 'GET') == 'GET' for call in api.call_args_list))
