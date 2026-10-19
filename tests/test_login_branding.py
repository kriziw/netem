"""Current branding must be usable before authentication, including first setup."""
from contextlib import ExitStack
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as netem
import branding


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def marked(self, name):
        return next(attrs for tag, attrs in self.nodes if name in attrs)


class LoginBrandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(netem.ACCESS_STORE, 'path', self.root/'access.db'))
        self.stack.enter_context(patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=True, SESSION_COOKIE_SECURE=False))
        self.stack.enter_context(patch.object(netem.BRAND_STORE, 'packs_dir', self.root/'brands'))
        self.stack.enter_context(patch.object(netem.BRAND_STORE, 'local_dir', None))
        self.stack.enter_context(patch.object(netem.BRAND_STORE, '_cache', (None, None, dict(branding.DEFAULT_BRAND))))
        self.stack.enter_context(patch.object(netem.BRAND_STORE, 'error', None))
        self.client = netem.app.test_client()
        self.admin = netem.ACCESS_STORE.create('admin', 'administrator-password', 'admin')
        self.pack('first', 'Example Network Lab', '#a75fff')
        netem.BRAND_STORE.set_active('first')

    def pack(self, pack_id, name, accent):
        root = self.root/'brands'/pack_id
        (root/'assets').mkdir(parents=True)
        (root/'assets'/'logo.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40"><rect width="120" height="40" fill="blue"/></svg>')
        (root/'assets'/'icon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><circle cx="16" cy="16" r="14" fill="blue"/></svg>')
        (root/'assets'/'regular.woff2').write_bytes(b'font')
        (root/'assets'/'private.txt').write_text('private-pack-data')
        (root/'branding.json').write_text(json.dumps(dict(name=name, subtitle='Our branded lab', mark='EX', logo='logo.svg',
            favicon='icon.svg', tokens={'accent': accent}, font_family='Example Sans', fonts=[{'file':'regular.woff2','weight':400}])))

    def test_anonymous_login_uses_current_name_logo_favicon_fonts_and_theme(self):
        response = self.client.get('/login?next=/tests')
        self.assertEqual(response.status_code, 200)
        self.assertIn('<title>Sign in · Example Network Lab</title>', response.text)
        self.assertIn('Our branded lab', response.text)
        self.assertNotIn('Sign in to NetEm', response.text)
        page = Page(response.text)
        logo, icon, theme = (page.marked(n) for n in ('data-brand-logo', 'data-brand-icon', 'data-brand-theme'))
        self.assertEqual(logo['alt'], 'Example Network Lab')
        self.assertNotIn('hidden', logo)
        for url in (logo['src'], icon['href'], theme['href']):
            self.assertIn('brand=', url)
            with self.client.get(url) as asset:
                self.assertEqual(asset.status_code, 200)
        self.assertIn('--accent:#a75fff', self.client.get(theme['href']).text)
        self.assertTrue(any(tag == 'link' and attrs.get('as') == 'font' for tag, attrs in page.nodes))
        with self.client.get('/branding/assets/regular.woff2') as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertIn('name="next" value="/tests"', response.text)
        self.assertIn('name="integration_csrf"', response.text)
        self.assertIn('--heading-font-family:', self.client.get(theme['href']).text)

    def test_brand_switch_changes_public_revision_assets_and_title_without_restart(self):
        first = Page(self.client.get('/login').text)
        self.pack('second', 'Second Brand', '#12aabb')
        netem.BRAND_STORE.set_active('second')
        response = self.client.get('/login')
        self.assertIn('<title>Sign in · Second Brand</title>', response.text)
        second = Page(response.text)
        self.assertNotEqual(first.marked('data-brand-logo')['src'], second.marked('data-brand-logo')['src'])
        status = self.client.get('/branding/status')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json['name'], 'Second Brand')
        self.assertIn('/static/branding-live.js', response.text)

    def test_only_presentation_is_public_and_private_files_stay_allowlisted(self):
        for path in ('/api/v1/state', '/api/v1/storage', '/api/v1/integrations/fortinet'):
            self.assertEqual(self.client.get(path).status_code, 401)
        for path in ('/settings/branding', '/settings/branding/export/first'):
            self.assertNotEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get('/branding/assets/private.txt').status_code, 404)
        self.assertEqual(self.client.get('/branding/assets/../branding.json').status_code, 404)
        self.assertEqual(self.client.get('/branding/assets/missing.svg').status_code, 404)
        self.assertEqual(self.client.get('/branding/branding.json').status_code, 404)

    def test_first_admin_setup_can_load_brand_before_any_account_exists(self):
        with patch.object(netem.ACCESS_STORE, 'path', self.root/'empty-access.db'):
            with netem.app.test_request_context():
                from flask import url_for
                url = url_for('access_bootstrap')
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Example Network Lab', response.text)
            page = Page(response.text)
            for attribute, prop in (('data-brand-logo','src'),('data-brand-icon','href'),('data-brand-theme','href')):
                with self.client.get(page.marked(attribute)[prop]) as asset:
                    self.assertEqual(asset.status_code, 200)
            self.assertEqual(self.client.get('/branding/status').status_code, 200)
            self.assertEqual(self.client.get('/api/v1/state').status_code, 401)

    def test_default_and_invalid_brand_have_a_clean_mark_fallback(self):
        netem.BRAND_STORE.set_active(None)
        for invalid in (False, True):
            if invalid:
                netem.BRAND_STORE.set_active('first')
                (self.root/'brands'/'first'/'branding.json').write_text('{invalid')
            response = self.client.get('/login')
            self.assertEqual(response.status_code, 200)
            self.assertIn(branding.DEFAULT_BRAND['name'], response.text)
            self.assertNotIn('data-brand-theme', response.text)
            self.assertNotIn('data-brand-icon', response.text)
            page = Page(response.text)
            self.assertNotIn('hidden', page.marked('data-brand-mark'))
            self.assertIn('hidden', page.marked('data-brand-logo'))

    def test_failed_login_keeps_brand_error_and_auth_controls(self):
        self.client.get('/login')
        with self.client.session_transaction() as state:
            token = state['integration_csrf']
        response = self.client.post('/login', data={'username':'admin','password':'incorrect', 'integration_csrf':token})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Invalid username or password.', response.text)
        self.assertIn('Example Network Lab', response.text)
        self.assertIn('autocomplete="current-password"', response.text)
        self.assertEqual(self.client.post('/login', data={'username':'admin','password':'incorrect'}).status_code, 400)

    def test_single_sign_on_is_preserved_with_the_branded_local_form(self):
        with patch.object(netem.ACCESS_STORE, 'oidc', return_value={'enabled': True}):
            response = self.client.get('/login')
        self.assertIn('Authentik single sign-on', response.text)
        self.assertIn('autocomplete="username"', response.text)
        self.assertIn('autocomplete="current-password"', response.text)
