"""Administrator-confirmed inventory removal preserves evidence and protects live paths."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import app as netem
from appliance_inventory import Store, validate


class ApplianceRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = Store(self.tmp.name)
        self.stack.enter_context(patch.object(netem.APPLIANCE_PLATFORM, 'store', self.store))
        self.stack.enter_context(patch.object(netem.ACCESS_STORE, 'path', Path(self.tmp.name)/'access.db'))
        self.stack.enter_context(patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=True, SESSION_COOKIE_SECURE=False))
        self.stack.enter_context(patch.object(netem.FORTINET_LAB, 'job', {'state': 'idle'}))
        self.stack.enter_context(patch.object(netem.STORAGE_ADMIN, 'idle', return_value=True))
        self.audit = self.stack.enter_context(patch.object(netem, 'log_event'))
        self.id = self.store.save(validate(dict(name='Lab FortiGate', vendor='fortinet', kind='vm',
            endpoint='https://192.0.2.10', vdom='root', required_licenses='vm'), netem.SDWAN_VENDORS), token='private-removal-token')
        self.admin = netem.ACCESS_STORE.create('admin', 'administrator-password', 'admin')
        self.client = netem.app.test_client()
        self.login(self.admin)
        self.url = f'/appliances/{self.id}/remove'

    def login(self, user_id):
        user = netem.ACCESS_STORE.get(user_id)
        with self.client.session_transaction() as state:
            state['access_identity'] = {'id': user_id, 'version': user['version'], 'at': time.time()}
            state['integration_csrf'] = 'test-token'

    def remove(self, confirmation='Lab FortiGate'):
        return self.client.post('/appliances/archive', data={'id': self.id, 'confirmation': confirmation,
                                                          'integration_csrf': 'test-token'})

    def test_list_and_inspect_link_to_explicit_confirmation_without_mutating(self):
        for url in ('/appliances', f'/appliances?detail={self.id}'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(self.url, response.text)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="confirmation"', response.text)
        self.assertIn('Retain historical observations', response.text)
        self.assertIn('does not reset or erase', response.text)
        self.assertNotIn('private-removal-token', response.text)
        self.assertIsNotNone(self.store.get(self.id))
        self.assertEqual(self.store.token(self.id), 'private-removal-token')

    def test_missing_or_wrong_confirmation_cannot_remove_device(self):
        for text in ('', 'Other appliance', 'lab fortigate'):
            self.assertEqual(self.remove(text).status_code, 302)
            self.assertIsNotNone(self.store.get(self.id))
            self.assertEqual(self.store.token(self.id), 'private-removal-token')
        self.audit.assert_not_called()

    def test_confirmed_removal_deletes_token_and_retains_observations_and_recovery(self):
        observation = {'availability': 'reachable', 'api': 'connected', 'health': {'state': 'healthy', 'metrics': {}},
                       'licenses': [], 'errors': [], 'identity': {}}
        self.store.record(self.store.get(self.id), observation)
        self.store.state('fortinet_lab_recovery:' + self.id, {'actions': ['retained evidence']})
        with patch.object(netem, 'traffic_generator_request') as remote:
            self.assertEqual(self.remove().status_code, 302)
            remote.assert_not_called()
        self.assertIsNone(self.store.get(self.id))
        self.assertEqual(self.store.token(self.id), '')
        self.assertEqual(len(self.store.history(self.id)), 1)
        self.assertEqual(self.store.state('fortinet_lab_recovery:' + self.id)['actions'], ['retained evidence'])
        self.audit.assert_called_once()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_current_selection_pending_switch_and_active_lab_are_blocked(self):
        for key, value, message in (
            ('selection', {'id': self.id, 'active': False}, 'selected for traffic'),
            ('pending', {'appliance_id': self.id}, 'pending appliance switch')):
            self.store.state(key, value)
            response = self.client.get(self.url)
            self.assertIn(message, response.text)
            self.assertNotIn('name="confirmation"', response.text)
            self.remove()
            self.assertIsNotNone(self.store.get(self.id))
            self.store.state(key, {})
        with patch.object(netem.STORAGE_ADMIN, 'idle', return_value=False):
            self.remove()
            self.assertIsNotNone(self.store.get(self.id))
        with patch.object(netem.FORTINET_LAB, 'job', {'state': 'applying'}):
            self.assertEqual(self.remove().status_code, 409)
            self.assertIsNotNone(self.store.get(self.id))

    def test_authorization_and_csrf_apply_to_confirmation_and_mutation(self):
        self.assertEqual(self.client.post('/appliances/archive', data={'id': self.id, 'confirmation': 'Lab FortiGate'}).status_code, 400)
        for role in ('viewer', 'presenter', 'operator'):
            self.login(netem.ACCESS_STORE.create(role, 'sample-user-password', role))
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.remove().status_code, 403)
            self.assertNotIn(self.url, self.client.get('/appliances').text)
        self.assertIsNotNone(self.store.get(self.id))
