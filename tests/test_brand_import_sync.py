import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import app as netem
import branding
from branding_sync import BrandingSync

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
CONFIG = {'schema_version': 1, 'branding': {'name': 'Example brand', 'logo': 'logo.png',
          'tokens': {'accent': '#5514B4'}}, 'assets': {'logo.png': base64.b64encode(PNG).decode()}}


class BrandImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.store = branding.BrandStore(Path(temporary.name) / 'brands')

    def test_round_trip_local_assets_urls_and_heading_font(self):
        raw = copy.deepcopy(CONFIG)
        raw['branding'].update(font_family='BT Curve', heading_font_family='BT Curve Headline', fonts=[
            {'file': 'https://assets.example.com/headline.woff2', 'weight': 400, 'family': 'heading'}])
        pack = self.store.import_config(raw)
        self.store.set_active(pack)
        self.assertEqual(self.store.export_config(), raw)
        self.assertEqual(self.store.current()[1]['heading_font_family'], 'BT Curve Headline')
        self.store.save(pack, {'logo_url': 'https://assets.example.com/logo.png',
                              'font_regular_url': 'https://assets.example.com/body.woff2'}, {})
        brand = self.store.current()[1]
        self.assertEqual(brand['logo'], 'https://assets.example.com/logo.png')
        self.assertEqual(len(brand['fonts']), 2)

    def test_import_rejects_unsafe_config_without_creating_a_pack(self):
        bad_configs = []
        for name, value in [('logo', '../outside.png'), ('logo', 'javascript:alert(1)'),
                            ('logo', 'https://user:password@example.com/a.png'), ('font_family', 'x";body{display:none}')]:
            raw = copy.deepcopy(CONFIG)
            raw['branding'][name] = value
            bad_configs.append(raw)
        raw = copy.deepcopy(CONFIG)
        raw['assets'] = {'../outside.png': base64.b64encode(PNG).decode()}
        bad_configs.append(raw)
        raw = copy.deepcopy(CONFIG)
        raw['assets']['logo.png'] = 'invalid base64'
        bad_configs.append(raw)
        for raw in bad_configs:
            with self.assertRaises(ValueError):
                self.store.import_config(raw)
        self.assertFalse(self.store.packs())

    def test_ui_import_requires_csrf_and_syncs_active_brand(self):
        client = netem.app.test_client()
        with client.session_transaction() as state:
            state['integration_csrf'] = 'token'
        with patch.object(netem, 'BRAND_STORE', self.store), patch.object(netem.BRANDING_SYNC, 'sync', return_value={'synced': True}) as sync:
            self.assertEqual(client.post('/settings/branding/import').status_code, 400)
            response = client.post('/settings/branding/import', data={'integration_csrf': 'token', 'activate': '1',
                'config': (io.BytesIO(json.dumps(CONFIG).encode()), 'brand.json')}, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.store.active_id(), 'example-brand')
            sync.assert_called_once()
            exported = client.get('/settings/branding/export/example-brand')
            self.assertEqual(exported.json, CONFIG)

    def test_retry_and_reconnected_agent_receive_current_brand(self):
        self.store.set_active(self.store.import_config(CONFIG))
        sender = Mock(side_effect=RuntimeError('offline'))
        sync = BrandingSync(self.store, sender, Mock())
        self.assertFalse(sync.sync()['synced'])
        sender.side_effect = None
        sender.return_value = {'revision': None}
        self.assertTrue(sync.sync()['synced'])
        self.assertEqual(sender.call_args.kwargs['payload'], CONFIG)
        revision = sync.state['revision']
        sender.reset_mock()
        sender.return_value = {'revision': revision}
        self.assertTrue(sync.sync()['synced'])
        sender.assert_called_once()  # Read-only revision check; no redundant uploads.
