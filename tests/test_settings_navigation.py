import copy
import unittest
from unittest.mock import patch

import app as netem


class SettingsNavigationTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()

    def test_groups_show_only_their_settings(self):
        for section, visible, hidden in [
            ('network', 'Topology &amp; interfaces', 'id="appliance"'),
            ('profiles', 'baseline latency', 'Time sync'),
            ('appearance', 'id="appliance"', 'Time sync'),
            ('system', 'Time sync', 'id="appliance"'),
        ]:
            with self.subTest(section=section):
                response = self.client.get('/settings?section=' + section)
                self.assertEqual(response.status_code, 200)
                content = response.text.split('<div class="settings-content">', 1)[1]
                self.assertIn(visible, content)
                self.assertNotIn(hidden, content)
                self.assertIn('aria-label="Settings"', response.text)
                self.assertEqual(response.text.count('aria-current="page"'), 1)

    def test_child_pages_keep_navigation_and_current_item(self):
        for url in ['/setup', '/presets', '/updates', '/integrations', '/settings/branding',
                    '/settings/traffic/network', '/settings/traffic/experience', '/settings/traffic/maintenance']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn('aria-label="Settings"', response.text)
                self.assertEqual(response.text.count('aria-current="page"'), 1)

    def test_unknown_group_falls_back_to_network(self):
        response = self.client.get('/settings?section=unknown')
        self.assertEqual(response.status_code, 200)
        self.assertIn('<h1>Traffic &amp; network</h1>', response.text)

    def test_appliance_save_returns_to_its_group(self):
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'
        with patch.object(netem, 'save_config') as save:
            response = self.client.post('/settings/appliance', data={
                'integration_csrf': 'token', 'vendor': '', 'model': 'Lab appliance'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/settings?section=appearance#appliance'))
        self.assertEqual(save.call_args.args[0]['appliance']['model'], 'Lab appliance')

    def test_sla_save_and_validation_return_to_profiles(self):
        with patch.object(netem, 'save_config') as save, patch.object(netem, 'log_event'):
            response = self.client.post('/lab/sla', data={
                'return_to': 'settings', 'return_section': 'profiles', 'name': 'Lab SLA',
                'latency_ms': '80', 'jitter_ms': '20', 'loss_pct': '1'})
            self.assertEqual(response.location, '/settings?section=profiles')
            self.assertEqual(save.call_args.args[0]['sla_profile']['latency_ms'], 80)
            response = self.client.post('/lab/sla', data={
                'return_to': 'settings', 'return_section': 'profiles', 'latency_ms': 'invalid'})
            self.assertEqual(response.location, '/settings?section=profiles')
            self.assertEqual(save.call_count, 1)

    def test_command_center_prioritizes_live_controls(self):
        config = copy.deepcopy(netem.load_config())
        config['mgmt_interface'] = 'eth0'
        config['wan_links'] = [{'id': 'wan1', 'inner': 'eth1', 'outer': 'eth2',
                                'bridge': 'br-wan1', 'preset': 'dia'}]
        with patch.object(netem, 'load_config', return_value=config):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertLess(page.index('id="wan-pulse"'), page.index('id="command-topology"'))
        self.assertLess(page.index('id="traffic-generator-overview"'), page.index('id="command-topology"'))
        for name in ['command-topology', 'command-history', 'command-activity']:
            self.assertIn('<details class="card command-detail" id="' + name + '">', page)
        self.assertIn('data-open-wan="wan1"', page)
        self.assertIn('data-chart="wan1-down"', page)
