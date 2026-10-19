"""Real SQLite compaction, failure boundaries and administrator-only storage data."""
from contextlib import ExitStack
import copy
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app as netem
import storage_admin as storage


class StorageMeasurementsTests(unittest.TestCase):
    def test_threshold_and_reserved_blocks_match_df(self):
        for free, available, expected in [(30, 20, False), (25, 20, False), (20, 20, True), (15, 10, True)]:
            stat = SimpleNamespace(f_blocks=100, f_bfree=free, f_bavail=available, f_frsize=4096)
            with patch.object(storage.os, 'statvfs', return_value=stat):
                result = storage.disk_usage(Path('/'))
            self.assertEqual(result['warning'], expected)
            self.assertEqual(result['available'], available * 4096)
        with patch.object(storage.os, 'statvfs', side_effect=OSError('unavailable')):
            self.assertIsNone(storage.disk_usage(Path('/'))['available'])

    def test_journal_allocation_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'journal').mkdir()
            log = root/'journal'/'system.journal'
            log.write_bytes(b'x' * 6000)
            (root/'journal'/'outside').symlink_to(root/'secret')
            self.assertEqual(storage.journal_size([root/'journal'])['bytes'], log.stat().st_blocks * 512)
            self.assertIsNone(storage.file_size(root/'journal'/'outside'))
            self.assertEqual(storage.file_size(root/'missing'), 0)


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'telemetry.db'
        self.netem = SimpleNamespace(TELEMETRY_DB_PATH=self.path, app=netem.app)
        self.manager = storage.StorageAdmin(self.netem)
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY, content TEXT)')
            conn.executemany('INSERT INTO samples(content) VALUES(?)', [('preserve-' + 'x'*2000,)] * 1000)
            conn.execute('DELETE FROM samples WHERE id > 20')
        self.expected = self.rows()

    def rows(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute('SELECT * FROM samples ORDER BY id').fetchall()

    def test_real_vacuum_shrinks_database_and_preserves_remaining_rows(self):
        before = self.path.stat().st_size
        self.manager.compact()
        job = self.manager.status()
        self.assertEqual(job['state'], 'completed')
        self.assertLess(self.path.stat().st_size, before)
        self.assertGreater(job['reclaimed_bytes'], 0)
        self.assertEqual(self.rows(), self.expected)
        self.assertIsNotNone(job['finished_at'])

    def test_low_space_refuses_before_any_database_write(self):
        before = self.path.read_bytes()
        with patch.object(storage, 'disk_usage', return_value={'available': 1}), self.assertLogs(netem.app.logger, 'ERROR'):
            self.manager.compact()
        self.assertEqual(self.manager.status()['state'], 'failed')
        self.assertIn('Not enough working space', self.manager.status()['message'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), self.expected)

    def test_busy_writer_returns_failure_without_losing_rows(self):
        with sqlite3.connect(self.path) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE samples SET content=content WHERE id=1")
            with self.assertLogs(netem.app.logger, 'ERROR'):
                self.manager.compact()
            writer.rollback()
        self.assertEqual(self.manager.status()['state'], 'failed')
        self.assertEqual(self.rows(), self.expected)

    def test_symlink_database_is_refused(self):
        link = self.path.with_name('link.db')
        link.symlink_to(self.path)
        self.netem.TELEMETRY_DB_PATH = link
        with self.assertLogs(netem.app.logger, 'ERROR'):
            self.manager.compact()
        self.assertEqual(self.manager.status()['state'], 'failed')
        self.assertEqual(self.rows(), self.expected)

    def test_second_job_is_not_started(self):
        with self.manager.lock:
            self.manager.job['state'] = 'running'
        with patch.object(storage.threading, 'Thread') as thread, self.assertRaisesRegex(ValueError, 'already running'):
            self.manager.start()
        thread.assert_not_called()


class AdminStorageRoutesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for item in (
            patch.object(netem.ACCESS_STORE, 'path', self.root/'access.sqlite3'),
            patch.object(netem, 'RUNTIME_DIR', self.root),
            patch.object(netem, 'TELEMETRY_DB_PATH', self.root/'telemetry.db'),
            patch.object(netem, 'EVENT_LOG_PATH', self.root/'events.jsonl'),
            patch.object(netem, 'load_config', return_value={'wan_links': []}),
            patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=True, SESSION_COOKIE_SECURE=False),
            patch.dict(netem.ACTIVE_SESSION, active=False),
            patch.dict(netem.SCENARIO_STATE, active=False),
            patch.dict(netem.SITE_PLAN_STATE, active=False),
            patch.dict(netem.CAPTURE_STATE, active=False),
            patch.dict(netem.TEST_RECORDER, active=False),
            patch.object(netem, 'EPHEMERAL_TELEMETRY', None),
            patch.object(netem, 'traffic_generator_config', return_value={}),
            patch.object(netem.STORAGE_ADMIN, 'job', dict(state='idle', message=None)),
        ):
            self.stack.enter_context(item)
        self.users = {'admin': netem.ACCESS_STORE.create('admin', 'admin-storage-password', 'admin')}
        for role in ('viewer', 'presenter', 'operator'):
            self.users[role] = netem.ACCESS_STORE.create(role, 'role-storage-password', role)
        self.client = netem.app.test_client()
        netem.init_telemetry_db()
        self.sign_in('admin')

    def sign_in(self, role):
        user_id = self.users[role]
        with self.client.session_transaction() as session:
            session['access_identity'] = dict(id=user_id, version=netem.ACCESS_STORE.get(user_id)['version'], at=time.time())
            session['integration_csrf'] = 'storage-csrf'

    def post(self, data=None):
        return self.client.post('/settings/storage/compact', data={'integration_csrf': 'storage-csrf'} if data is None else data)

    def test_non_admins_cannot_read_metrics_or_compact_even_by_direct_request(self):
        for role in ('viewer', 'presenter', 'operator'):
            self.sign_in(role)
            self.assertEqual(self.client.get('/settings/storage').status_code, 403)
            self.assertEqual(self.client.get('/api/v1/storage').status_code, 403)
            self.assertEqual(self.post().status_code, 403)

    def test_admin_page_and_api_include_size_components_and_csrf(self):
        response = self.client.get('/settings/storage')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Storage &amp; database', response.text)
        self.assertIn('value="storage-csrf"', response.text)
        self.assertIn('Compact database', response.text)
        self.assertEqual(self.post({}).status_code, 400)
        metrics = self.client.get('/api/v1/storage').json
        self.assertIsInstance(metrics['disk']['available'], int)
        self.assertNotIn('access_identity', str(metrics))

    def test_banner_only_admins_at_eighty_percent(self):
        disk = dict(total=100, used=85, available=15, used_percent=85, warning=True, error=None)
        with patch.object(storage, 'disk_usage', return_value=disk):
            response = self.client.get('/sessions')
            self.assertIn('id="admin-storage-warning"', response.text)
            self.sign_in('operator')
            self.assertNotIn('id="admin-storage-warning"', self.client.get('/sessions').text)
        self.sign_in('admin')
        with patch.object(storage, 'disk_usage', return_value=dict(disk, warning=False)):
            self.assertNotIn('id="admin-storage-warning"', self.client.get('/sessions').text)

    def test_active_lab_blocks_compaction(self):
        for state in (netem.ACTIVE_SESSION, netem.SCENARIO_STATE, netem.SITE_PLAN_STATE, netem.CAPTURE_STATE):
            with patch.dict(state, active=True):
                self.assertEqual(self.post().status_code, 409)

    def test_new_lab_operations_block_while_compaction_runs(self):
        with patch.dict(netem.STORAGE_ADMIN.job, state='running'):
            for endpoint in ('/sessions/start', '/lab/scenario/start', '/site/run', '/traffic-generator/start', '/lab/capture/start'):
                response = self.client.post(endpoint, data={'integration_csrf': 'storage-csrf'})
                self.assertEqual(response.status_code, 409, endpoint)
                self.assertIn('maintenance is running', response.text)

    def test_simulator_workload_or_unknown_status_blocks_compaction(self):
        with patch.object(netem, 'traffic_generator_config', return_value={'host': 'simulator'}):
            for status in ('running', 'starting', None):
                with patch.object(netem, 'traffic_generator_request', return_value={'status': status}):
                    self.assertEqual(self.post().status_code, 409)
            with patch.object(netem, 'traffic_generator_request', side_effect=RuntimeError('offline')):
                self.assertEqual(self.post().status_code, 409)

    def test_start_is_async_and_duplicate_cannot_create_second_worker(self):
        with patch.object(netem.STORAGE_ADMIN, 'require_space'), patch.object(storage.threading, 'Thread') as thread:
            response = self.post()
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.json['accepted'])
            thread.return_value.start.assert_called_once()
            self.assertEqual(self.post().status_code, 409)
            thread.return_value.start.assert_called_once()
