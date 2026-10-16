"""Permission boundaries, account lifecycle and real Authlib token validation."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response

import app as netem
import access_control as access


class AccessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'access.sqlite3'
        replacement = patch.object(netem.ACCESS_STORE, 'path', self.path)
        replacement.start()
        self.addCleanup(replacement.stop)
        flags = patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=False, SESSION_COOKIE_SECURE=False)
        flags.start()
        self.addCleanup(flags.stop)
        self.store = netem.ACCESS_STORE
        self.admin = self.store.create('admin', 'local-admin-password', 'admin')
        self.client = netem.app.test_client()

    def identity(self, user_id):
        user = self.store.get(user_id)
        with self.client.session_transaction() as state:
            state['access_identity'] = {'id': user_id, 'version': user['version'], 'at': time.time()}
            state['integration_csrf'] = 'test-token'

    def test_unauthenticated_ui_and_api_are_blocked(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/v1/state').status_code, 401)
        self.assertEqual(self.client.post('/lab/scenario/stop').status_code, 401)
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_server_enforces_the_role_matrix_even_for_direct_requests(self):
        cases = [('overview', '/', 'GET', 0), ('api_state', '/api/v1/state', 'GET', 0),
                 ('lab_scenario_start', '/lab/scenario/start', 'POST', 1),
                 ('lab_scenario_phase', '/lab/scenario/phase', 'POST', 1),
                 ('quick_wan_action', '/wan/quick', 'POST', 2),
                 ('lab_capture_start', '/lab/capture/start', 'POST', 2),
                 ('traffic_generator_adjust', '/traffic-generator/adjust', 'POST', 2),
                 ('integrations', '/integrations', 'GET', 3),
                 ('simulator_controller', '/simulator/', 'GET', 3),
                 ('access_settings', '/settings/users', 'GET', 3),
                 ('access_create', '/settings/users/create', 'POST', 3),
                 ('setup', '/setup', 'POST', 3), ('updates', '/updates', 'POST', 3)]
        handlers = {endpoint: lambda **kwargs: 'ok' for endpoint, *_ in cases}
        with patch.dict(netem.app.view_functions, handlers):
            for level, role in enumerate(access.ROLES):
                user_id = self.store.create('test-' + role, 'sample-user-password', role)
                self.identity(user_id)
                for _, path, method, required in cases:
                    with self.subTest(role=role, path=path):
                        response = self.client.open(path, method=method, data={'integration_csrf': 'test-token'})
                        self.assertEqual(response.status_code, 200 if level >= required else 403)

    def test_csrf_covers_all_authenticated_mutations(self):
        self.identity(self.admin)
        with patch.dict(netem.app.view_functions, lab_scenario_stop=lambda: 'ok'):
            self.assertEqual(self.client.post('/lab/scenario/stop').status_code, 400)
            self.assertEqual(self.client.post('/lab/scenario/stop', headers={'X-CSRF-Token': 'test-token'}).status_code, 200)
            self.assertEqual(self.client.post('/lab/scenario/stop', data={'integration_csrf': 'bad'}).status_code, 400)

    def test_local_login_logout_and_open_redirect_rejection(self):
        self.client.get('/login')
        with self.client.session_transaction() as state:
            token = state['integration_csrf']
        response = self.client.post('/login', data={'username': 'ADMIN', 'password': 'local-admin-password',
                                                   'integration_csrf': token, 'next': '//evil.example/'})
        self.assertEqual(response.location, '/')
        with self.client.session_transaction() as state:
            self.assertEqual(state['access_identity']['id'], self.admin)
            self.assertNotIn('password', json.dumps(dict(state)))
            new_token = state['integration_csrf']
        self.assertNotEqual(new_token, token)
        self.assertEqual(self.client.post('/logout', data={'integration_csrf': new_token}).status_code, 302)
        self.assertEqual(self.client.get('/api/v1/state').status_code, 401)

    def test_role_password_and_disable_changes_revoke_existing_sessions(self):
        user = self.store.create('operator', 'operator-password', 'operator')
        for password, active in (('', True), ('changed-password', True), ('', False)):
            self.identity(user)
            self.store.update(user, 'viewer', active, password)
            self.assertEqual(self.client.get('/api/v1/state').status_code, 401)
        self.assertIsNone(self.store.authenticate('operator', 'changed-password'))

    def test_last_local_admin_cannot_be_disabled_or_demoted(self):
        for role, active in (('viewer', True), ('admin', False)):
            with self.assertRaises(ValueError):
                self.store.update(self.admin, role, active)
        self.store.create('recovery', 'recovery-password', 'admin')
        self.store.update(self.admin, 'viewer', True)
        self.assertEqual(self.store.get(self.admin)['role'], 'viewer')

    def test_settings_hide_secrets_and_insert_csrf_into_forms(self):
        self.identity(self.admin)
        self.store.save_oidc(dict(enabled=False, groups={}, client_secret='top-secret'))
        html = self.client.get('/settings/users').get_data(as_text=True)
        self.assertNotIn('top-secret', html)
        self.assertIn('name="integration_csrf" value="test-token"', html)
        self.assertIn('Authentik', html)

    def test_oidc_group_mapping_subject_binding_and_disabled_identity(self):
        settings = {'issuer': 'https://auth.example/application/o/netem/',
                    'groups': {'presenters': 'presenter', 'admins': 'admin'}}
        claims = {'iss': settings['issuer'], 'sub': 'subject-1', 'preferred_username': 'admin',
                  'groups': ['presenters', 'admins']}
        user = self.store.oidc_user(claims, settings)
        self.assertNotEqual(user['id'], self.admin)
        self.assertIsNone(user['password'])
        self.assertEqual(user['role'], 'admin')
        claims['groups'] = ['presenters']
        self.assertEqual(self.store.oidc_user(claims, settings)['role'], 'presenter')
        claims['groups'] = []
        with self.assertRaises(ValueError):
            self.store.oidc_user(claims, settings)
        claims['groups'] = ['presenters']
        self.store.update(user['id'], 'presenter', False)
        with self.assertRaises(ValueError):
            self.store.oidc_user(claims, settings)
        claims['iss'] = 'https://another.example/'
        with self.assertRaises(ValueError):
            self.store.oidc_user(claims, settings)

    def test_enforced_mode_blocks_access_before_provisioning(self):
        other = Path(self.path.parent) / 'empty.sqlite3'
        with patch.object(self.store, 'path', other), patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=True):
            self.assertEqual(self.client.get('/api/v1/state').status_code, 401)
            self.assertEqual(self.client.post('/settings/users/create').status_code, 401)

    def test_oidc_real_flow_checks_state_nonce_signature_issuer_audience_and_expiry(self):
        issuer = 'https://auth.example/application/o/netem/'
        settings = dict(enabled=True, issuer=issuer, client_id='netem-client', client_secret='secret',
                        redirect_uri='https://netem.example/auth/oidc/callback', groups={'netem-viewers': 'viewer'})
        self.store.save_oidc(settings)
        key = RSAKey.generate_key(2048, parameters={'kid': 'test'})
        other_key = RSAKey.generate_key(2048, parameters={'kid': 'test'})
        nonce, claims, signature_key = '', {}, key
        def http(_session, method, url, **kwargs):
            response = Response()
            response.status_code, response.url = 200, url
            if url.endswith('openid-configuration'):
                payload = {'issuer': issuer, 'authorization_endpoint': issuer + 'authorize/',
                    'token_endpoint': issuer + 'token/', 'jwks_uri': issuer + 'jwks/',
                    'id_token_signing_alg_values_supported': ['RS256']}
            elif url.endswith('jwks/'):
                payload = {'keys': [key.as_dict(private=False)]}
            elif url.endswith('token/'):
                self.assertIn('code_verifier', kwargs.get('data', {}))
                payload = {'access_token': 'token', 'token_type': 'Bearer',
                           'id_token': jwt.encode({'alg': 'RS256', 'kid': 'test'}, claims, signature_key)}
            else:
                raise AssertionError('Unexpected OIDC request: ' + url)
            response._content = json.dumps(payload).encode()
            response.headers['Content-Type'] = 'application/json'
            return response
        for bad in (None, 'state', 'nonce', 'signature', 'iss', 'aud', 'exp'):
            with self.subTest(invalid=bad), patch('requests.sessions.Session.request', http):
                self.client = netem.app.test_client()
                response = self.client.get('/auth/oidc/login')
                self.assertEqual(response.status_code, 302)
                query = parse_qs(urlsplit(response.location).query)
                self.assertEqual(query['code_challenge_method'], ['S256'])
                nonce = query['nonce'][0]
                claims = {'iss': issuer, 'sub': 'oidc-subject', 'aud': 'netem-client', 'iat': int(time.time()),
                          'exp': int(time.time()) + 600, 'nonce': nonce, 'groups': ['netem-viewers']}
                signature_key = other_key if bad == 'signature' else key
                if bad in ('nonce', 'iss', 'aud'):
                    claims[bad] = 'incorrect'
                if bad == 'exp':
                    claims['exp'] = int(time.time()) - 3600
                state = 'incorrect' if bad == 'state' else query['state'][0]
                response = self.client.get('/auth/oidc/callback', query_string={'code': 'code', 'state': state})
                self.assertEqual(response.status_code, 302 if bad is None else 403)
                with self.client.session_transaction() as state:
                    self.assertEqual('access_identity' in state, bad is None)


if __name__ == '__main__':
    unittest.main()
