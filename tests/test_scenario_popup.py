import copy
import unittest
from unittest.mock import patch

import app as netem


class ScenarioPopupTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        self.config = copy.deepcopy(netem.load_config())
        self.config['mgmt_interface'] = 'eth0'
        self.config['wan_links'] = [{'id': 'wan1', 'name': 'WAN 1', 'inner': 'eth1',
                                     'outer': 'eth2', 'bridge': 'br-wan1', 'preset': 'dia'}]
        self.config_patch = patch.object(netem, 'load_config', return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_shortcuts_load_only_the_selected_form_without_starting_a_test(self):
        overview = self.client.get('/').text
        for scenario_id in ('progressive_brownout', 'sla_failover', 'flaky_underlay'):
            with self.subTest(scenario=scenario_id), patch.object(netem.threading, 'Thread') as thread:
                response = self.client.get('/tests/scenario/' + scenario_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn('data-scenario-configuration', response.text)
                self.assertIn('name="scenario_id" value="' + scenario_id + '"', response.text)
                self.assertIn('name="return_to" value="overview"', response.text)
                self.assertIn('aria-label="WAN"', response.text)
                self.assertIn('name="length_min"', response.text)
                self.assertIn('step="0.1"', response.text)
                self.assertEqual(response.text.count('action="/lab/scenario/start"'), 1)
                self.assertNotIn('app-shell', response.text)
                self.assertIn('data-scenario-modal="/tests/scenario/' + scenario_id + '"', overview)
                self.assertIn('/tests?scenario=' + scenario_id, overview)
                thread.assert_not_called()
        with self.client.session_transaction() as state:
            self.assertIn(state['integration_csrf'], response.text)

    def test_missing_scenario_is_not_silently_replaced(self):
        self.assertEqual(self.client.get('/tests/scenario/unknown').status_code, 404)

    def test_active_test_or_site_plan_disables_start(self):
        for test_active, site_active in ((True, False), (False, True)):
            with patch.object(netem, 'scenario_snapshot', return_value={'active': test_active}), \
                 patch.object(netem, 'site_plan_snapshot', return_value={'active': site_active}):
                response = self.client.get('/tests/scenario/progressive_brownout')
                self.assertIn('A test is already running', response.text)
                self.assertIn('type="submit" disabled', response.text)

    def test_no_wan_disables_start_and_explains_setup(self):
        self.config['wan_links'] = []
        response = self.client.get('/tests/scenario/progressive_brownout')
        self.assertIn('Configure a WAN', response.text)
        self.assertIn('type="submit" disabled', response.text)

    def test_tests_page_uses_the_same_form_with_its_existing_return_path(self):
        with patch.object(netem, 'traffic_generator_snapshot', return_value={'configured': False}):
            response = self.client.get('/tests?scenario=progressive_brownout')
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="test-progressive_brownout"', response.text)
        self.assertIn('name="return_to" value="tests"', response.text)
        self.assertIn('name="scenario_id" value="progressive_brownout"', response.text)
