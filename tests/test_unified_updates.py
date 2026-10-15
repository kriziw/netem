"""One place for every update: NetEm, the Traffic Simulator and the controlled target, under Settings → Updates."""
import copy
import unittest
from unittest.mock import patch

import app as netem
STATUS = {"ok": True, "error": "", "branch": "main", "target_branch": "main", "commit": "abc1234",
          "subject": "release", "remote_url": "https://example.invalid/netem.git", "behind": 2, "ahead": 0,
          "dirty": False, "installed_version": "0.12.0", "remote_version": "0.13.0", "release_commit": "f00d123",
          "release_behind": 2, "release_reachable": True, "unreleased": 0, "update_available": True}

SIMULATOR = {
    'version': '0.16.0',
    'system': {
        'busy': False, 'workload_active': False, 'ready': True, 'job': {}, 'selected': {},
        'release': {'tag': 'v0.17.0', 'display_tag': '0.17.0', 'available': True},
        'target': {'connected': True, 'host': '198.18.0.1', 'version': '0.9.1', 'version_match': True,
                   'busy': False, 'job': {'message': ''},
                   'release': {'tag': 'v0.9.2', 'display_tag': '0.9.2', 'available': True}},
        'interface_configs': {}, 'inventory': {},
    },
    'network': {'management': 'eth0', 'interfaces': [], 'paths': [], 'leases': {}, 'probe': {}, 'cards': []},
    'experience': {'applications': {}, 'policy': {}},
}


class UpdatesPageTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'

    def page(self, simulator=None, simulator_error=None):
        patches = [patch.object(netem, 'git_update_status', return_value=STATUS),
                   patch.object(netem, 'simulator_update_state', return_value=(simulator or {}, simulator_error))]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        return self.client.get('/updates').get_data(as_text=True)

    def test_one_page_shows_all_three_components(self):
        page = self.page(copy.deepcopy(SIMULATOR))
        for name in ('>NetEm<', '>Traffic Simulator<', '>Controlled target<'):
            self.assertIn(name, page)
        self.assertIn('0.17.0', page)
        self.assertIn('0.9.2', page)
        self.assertIn('Update available', page)

    def test_simulator_and_target_actions_go_through_the_simulator_command_and_return_here(self):
        page = self.page(copy.deepcopy(SIMULATOR))
        for action in ('install_update', 'target_install'):
            with self.subTest(action=action):
                self.assertIn(f'name="action" value="{action}"', page)
        self.assertEqual(page.count('name="return_to" value="updates"'), 2)
        self.assertEqual(page.count('>Check for updates</button>'), 1)
        self.assertIn('data-all-update-check', page)
        self.assertIn('action="/settings/traffic/maintenance/system"', page)
        self.assertIn('name="tag" value="v0.17.0"', page)

    def test_an_unconfigured_or_unreachable_simulator_says_so_and_disables_its_actions(self):
        page = self.page({}, 'The Traffic Simulator is not configured. Connect it under Integrations first.')
        self.assertIn('not configured', page)
        self.assertIn('Not connected', page)
        simulator = page.split('>Traffic Simulator<', 1)[1].split('>Controlled target<', 1)[0]
        self.assertEqual(simulator.count('disabled'), 1)

    def test_the_netem_install_keeps_its_progress_form(self):
        page = self.page(copy.deepcopy(SIMULATOR))
        self.assertIn('data-update-form', page)
        self.assertIn('data-from="0.12.0"', page)
        self.assertIn('Update available', page)


class SimulatorUpdateActionTests(unittest.TestCase):
    def setUp(self):
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state['integration_csrf'] = 'token'
        for mock in (patch.object(netem, 'scenario_snapshot', return_value={'active': False}),
                     patch.object(netem, 'log_event')):
            mock.start()
            self.addCleanup(mock.stop)

    def post(self, data, **patches):
        with patch.object(netem, 'traffic_generator_request', **patches) as api:
            reply = self.client.post('/settings/traffic/maintenance/system', data=dict(
                {'integration_csrf': 'token'}, **data))
        return reply, api

    def test_install_from_the_updates_page_returns_there_and_is_not_sent_to_the_simulator(self):
        reply, api = self.post({'return_to': 'updates', 'action': 'install_update', 'tag': 'v0.17.0'},
                               return_value={'message': 'Agent update queued.'})
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(reply.location.endswith('/updates'), reply.location)
        self.assertEqual(api.call_args.args[0], '/api/v1/controller/commands/system')
        self.assertEqual(api.call_args.kwargs['payload'], {'action': 'install_update', 'tag': 'v0.17.0'})

    def test_refused_update_returns_to_the_updates_page_with_the_reason(self):
        reply, _api = self.post({'return_to': 'updates', 'action': 'target_install', 'tag': 'v0.9.2'},
                                side_effect=RuntimeError('This test controls traffic. Stop & restore the test first.'))
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(reply.location.endswith('/updates'), reply.location)
        with self.client.session_transaction() as state:
            messages = [message for _category, message in state.get('_flashes', [])]
        self.assertTrue(any('Stop & restore the test first.' in message for message in messages), messages)

    def test_other_forms_still_return_to_their_section(self):
        reply, api = self.post({'action': 'repair'}, return_value={'message': 'Repair queued.'})
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(reply.location.endswith('/settings/traffic/maintenance'), reply.location)
        self.assertEqual(api.call_args.kwargs['payload'], {'action': 'repair'})


class AgentAndTargetPageTests(unittest.TestCase):
    def test_update_buttons_moved_to_the_updates_page(self):
        client = netem.app.test_client()
        with patch.object(netem, 'traffic_generator_request', return_value=copy.deepcopy(SIMULATOR)), \
             patch.object(netem, 'scenario_snapshot', return_value={'active': False}):
            page = client.get('/settings/traffic/maintenance').get_data(as_text=True)
        for label in ('Install agent update', 'Check updates', 'Install target update', 'Check target updates'):
            self.assertNotIn(label, page)
        self.assertIn('href="/updates"', page)
        self.assertIn('Refresh target status', page)
        self.assertIn('Repair workload path', page)


class NavigationTests(unittest.TestCase):
    def test_settings_has_one_updates_entry_and_no_separate_application_updates_menu(self):
        client = netem.app.test_client()
        with patch.object(netem, 'git_update_status', return_value=STATUS):
            page = client.get('/settings?section=system').get_data(as_text=True)
        self.assertIn('Updates', page)
        self.assertNotIn('Application updates', page)
        self.assertNotIn('Application &amp; updates', page)
        self.assertEqual(page.count('href="/updates"'), 2)


if __name__ == '__main__':
    unittest.main()
