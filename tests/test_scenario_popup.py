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


class AuthenticatedScenarioStartTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import time
        from pathlib import Path
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(netem.ACCESS_STORE, 'path', Path(temp) / 'access.sqlite3'))
        self.stack.enter_context(patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=True, SESSION_COOKIE_SECURE=False))
        netem.ACCESS_STORE.create('admin', 'scenario-admin-password', 'admin')
        user_id = netem.ACCESS_STORE.create('operator', 'scenario-test-password', 'operator')
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['access_identity'] = {'id': user_id, 'version': netem.ACCESS_STORE.get(user_id)['version'], 'at': time.time()}
            state['integration_csrf'] = 'scenario-test-token'
        self.real_preflight = netem.APPLIANCE_PLATFORM.preflight
        self.preflight = self.stack.enter_context(patch.object(netem.APPLIANCE_PLATFORM, 'preflight'))
        self.temp_path = temp
        self.thread = self.stack.enter_context(patch.object(netem.threading, 'Thread'))
        self.stack.enter_context(patch.dict(netem.ACTIVE_SESSION, active=True))
        self.stack.enter_context(patch.dict(netem.SCENARIO_STATE, active=False))
        self.stack.enter_context(patch.dict(netem.SITE_PLAN_STATE, active=False))
        self.config = copy.deepcopy(netem.load_config())
        self.config['wan_links'] = [{'id': 'wan1', 'name': 'WAN 1', 'inner': 'eth1',
                                     'outer': 'eth2', 'bridge': 'br-wan1', 'preset': 'dia'}]
        self.stack.enter_context(patch.object(netem, 'load_config', return_value=self.config))
        self.data = {'integration_csrf': 'scenario-test-token', 'scenario_id': 'progressive_brownout',
                     'link_id': 'wan1', 'length_min': '3', 'live_window_seconds': '15'}

    def post(self, data=None):
        return self.client.post('/lab/scenario/start', data=self.data if data is None else data,
                                headers={'Accept': 'application/json'})

    def test_real_authenticated_configuration_form_can_start_without_rendering_dashboard(self):
        import re
        response = self.client.get('/tests/scenario/progressive_brownout')
        token = re.search(r'name="integration_csrf" value="([^"]+)"', response.text).group(1)
        self.data['integration_csrf'] = token
        with patch.object(netem, 'build_link_states', side_effect=AssertionError('POST must not render dashboard')):
            response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['scenario_id'], 'progressive_brownout')
        self.assertTrue(response.json['accepted'])
        self.thread.return_value.start.assert_called_once()

    def test_preflight_refusal_is_json_without_a_redirect_or_test_thread(self):
        for reason in ('Run the scenario on the WAN assigned to this direct NetEm path.',
                       'The direct path is not verified. Check Platform Health before starting traffic.'):
            with patch.object(netem.APPLIANCE_PLATFORM, 'preflight', side_effect=ValueError(reason)):
                response = self.post()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json, {'error': reason})
        self.thread.assert_not_called()

    def test_invalid_inputs_are_explained_without_starting(self):
        for fields, expected in (({'link_id': 'missing'}, 'Unknown WAN'),
                                 ({'scenario_id': 'missing'}, 'Unknown scenario'),
                                 ({'length_min': 'bad'}, 'bad')):
            response = self.post(dict(self.data, **fields))
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected.lower(), response.json['error'].lower())
        self.thread.assert_not_called()

    def test_csrf_protection_still_rejects_before_execution(self):
        response = self.post(dict(self.data, integration_csrf='bad'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid form token', response.text)
        self.thread.assert_not_called()

    def test_verified_direct_path_starts_brownout_but_wrong_wan_is_explained(self):
        from appliance_inventory import Store
        platform = netem.APPLIANCE_PLATFORM
        self.stack.enter_context(patch.object(platform, 'store', Store(self.temp_path)))
        platform.store.state('traffic_paths', {'paths': [{'id': 'direct'}]})
        self.stack.enter_context(patch.object(platform, 'check_platform', return_value={
            'network': {'selected': {'mode': 'direct_netem', 'wan_id': 'wan1', 'name': 'Direct NetEm'}},
            'path': {'ready': True}}))
        self.stack.enter_context(patch.object(netem, 'log_event'))
        self.preflight.side_effect = self.real_preflight
        response = self.post(dict(self.data, link_id='wan2'))
        self.assertEqual(response.status_code, 409)
        self.assertIn('WAN assigned', response.json['error'])
        self.thread.assert_not_called()
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json['accepted'])
        self.thread.return_value.start.assert_called_once()

    def test_unsaved_scenario_publishes_live_data_then_waits_on_showcase_listener(self):
        from showroom import create_showroom_app
        self.stack.enter_context(patch.dict(netem.ACTIVE_SESSION, active=False))
        self.addCleanup(netem.end_unsaved_telemetry)
        # The real authenticated popup flow must retain the explicit unsaved acknowledgement.
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertIn('id="no-session-warning"', response.text)
        self.thread.assert_not_called()
        response = self.post(dict(self.data, no_session_ack='1'))
        self.assertEqual(response.status_code, 202)
        self.assertTrue(netem.SCENARIO_STATE['unsaved'])
        state = {'id': 'wan1', 'label': 'WAN 1', 'preset_name': 'DIA', 'runtime_quality': 40,
                 'fault': 'normal', 'sla': {'pass': False},
                 'effective': {'delay_ms': 80, 'jitter_ms': 10, 'loss_pct': 5,
                               'download_mbit': 100, 'upload_mbit': 20}}
        self.stack.enter_context(patch.object(netem, 'build_link_states', return_value=[state]))
        self.stack.enter_context(patch.object(netem, 'latest_telemetry_sample', side_effect=lambda _: {
            'timestamp': netem.time.time(), 'rate_valid': True, 'down_mbps': 12.5, 'up_mbps': 1.5}))
        self.stack.enter_context(patch.object(netem, 'current_diagnosis', return_value={}))
        self.stack.enter_context(patch.object(netem, 'showcase_video_public', return_value=None))
        viewer = create_showroom_app(netem.showroom_snapshot).test_client()
        snapshot = viewer.get('/api/snapshot').json
        self.assertTrue(snapshot['scenario']['active'])
        self.assertTrue(snapshot['scenario']['unsaved'])
        self.assertFalse(snapshot['session']['active'])
        link = snapshot['links'][0]
        self.assertEqual((link['quality'], link['delay_ms'], link['loss_pct']), (40, 80, 5))
        self.assertTrue(link['traffic_available'])
        self.assertEqual(link['down_mbps'], 12.5)
        self.assertIsNone(snapshot['last_test'])
        # Completion and Stop & restore both clear the active flag. Neither may leave a report
        # from this trial or an older session on the public screen.
        for result in ('passed', 'stopped', 'failed'):
            with patch.dict(netem.SCENARIO_STATE, active=False, result=result), \
                 patch.object(netem, 'LAST_TEST_SUMMARY', {'name': 'Old saved test', 'ended_at': netem.time.time()}):
                snapshot = viewer.get('/api/snapshot').json
            self.assertEqual(snapshot['scenario'], {'active': False})
            self.assertEqual(snapshot['links'], [])
            self.assertFalse(snapshot['session']['active'])
            self.assertIsNone(snapshot['last_test'])
            self.assertIsNone(snapshot['plan_summary'])
