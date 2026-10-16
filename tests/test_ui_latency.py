"""Presentation routes must return even while the configured simulator stalls."""
import concurrent.futures
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app as netem
from presentation_cache import PresentationCache


class UiLatencyTests(unittest.TestCase):
    def test_pages_and_showcase_return_while_simulator_is_stalled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = PresentationCache()
            release = threading.Event()
            requests = []
            cfg = {'mgmt_interface': 'lo', 'wan_links': [{'id': 'wan1', 'inner': 'lo', 'outer': 'lo', 'preset': 'dia'}],
                   'traffic_generator': {'host': 'simulator.invalid', 'tls_sha256': 'pinned'}}
            def simulator(path, **kwargs):
                requests.append(path)
                release.wait(5)
                if path == '/api/v1/status':
                    return {'status': 'idle', 'run': None, 'users': 0, 'dem': {}}
                return {'ready': True}
            with patch.object(netem, 'CONFIG_PATH', root / 'config.json'), \
                    patch.object(netem, 'load_config', return_value=cfg), \
                    patch.object(netem, 'traffic_generator_api_key', return_value='test-key'), \
                    patch.object(netem, 'PRESENTATION_READS', cache), \
                    patch.object(netem, 'traffic_generator_request', side_effect=simulator), \
                    patch.object(netem, 'TELEMETRY_DB_PATH', root / 'telemetry.db'), \
                    patch.object(netem.APPLIANCE_PLATFORM, 'selected', return_value=None), \
                    patch.object(netem, 'appliance_under_test', return_value={}), \
                    patch.object(netem, 'ACTIVE_SESSION', dict(netem.ACTIVE_SESSION, active=True, id='latency', started_at=time.time())), \
                    patch.object(netem, 'SCENARIO_STATE', dict(netem.SCENARIO_STATE, active=False)), \
                    patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=False), \
                    patch.object(netem.ACCESS_STORE, 'enabled', return_value=False):
                try:
                    # Separate request threads let the test fail rather than hang on old synchronous code.
                    def get(path):
                        return netem.app.test_client().get(path).status_code
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        for path in ('/', '/tests', '/api/v1/traffic-generator', '/api/v1/traffic-generator/path', '/api/v1/diagnosis'):
                            with self.subTest(path=path):
                                self.assertIn(pool.submit(get, path).result(1), (200, 202, 503))
                        viewer = pool.submit(lambda: netem.showroom_app.test_client().get('/api/snapshot'))
                        self.assertEqual(viewer.result(1).status_code, 200)
                    self.assertFalse(release.is_set())
                    self.assertLessEqual(requests.count('/api/v1/status'), 1)
                finally:
                    release.set()
                    # Keep patches alive until background readers finish; no test thread leaks.
                    for _ in range(4):
                        with cache.lock:
                            pending = [entry['done'] for entry in cache.entries.values() if entry['loading']]
                        for done in pending:
                            self.assertTrue(done.wait(2))

    def test_diagnosis_does_not_wait_for_path_metadata(self):
        with patch.object(netem, 'traffic_generator_snapshot', return_value={'configured': True, 'connected': False}), \
                patch.object(netem, 'build_link_states', return_value=[]), \
                patch.object(netem, 'traffic_path_readiness', return_value={'available': False}) as path, \
                patch.object(netem, 'track_diagnosis_events'):
            netem.build_diagnosis()
        path.assert_called_once_with(cached=True)

    def test_cached_status_rechecks_the_current_phase_before_showing_evidence(self):
        cache = PresentationCache()
        fallback = {'configured': True, 'connected': False}
        raw = {'configured': True, 'connected': True, 'status': {'run': {'run_id': 'run'},
               'dem': {'live_phase_id': 'old-phase', 'experience_score': 100, 'scoring_complete': True}}}
        cache.fresh('status', 'source', lambda: raw)
        with patch.object(netem, 'PRESENTATION_READS', cache), \
                patch.object(netem, 'presentation_source', return_value='source'), \
                patch.object(netem, 'traffic_generator_empty_snapshot', return_value=fallback), \
                patch.object(netem, 'test_live_context', return_value={'phase_id': 'new-phase'}), \
                patch.object(netem, 'SCENARIO_STATE', dict(active=True, workload_supervised=True, workload_run_id='run')):
            result = netem.traffic_generator_snapshot(cached=True)
        self.assertFalse(result['status']['dem']['scoring_complete'])
        self.assertIsNone(result['status']['dem']['experience_score'])
        # The shared raw snapshot is intact for another caller; filtering uses a copy.
        self.assertEqual(cache.read('status', 'source', lambda: {}, {})['status']['dem']['experience_score'], 100)

    def test_status_connection_outlives_evidence_but_not_forever(self):
        now = [0.0]
        cache = PresentationCache(clock=lambda: now[0])
        raw = {'configured': True, 'connected': True, 'status': {'status': 'stopped', 'users': 0,
               'dem': {'experience_score': 100, 'scoring_complete': True}}}
        cache.fresh('status', 'source', lambda: raw)
        release = threading.Event()
        def stalled():
            release.wait(2)
            return raw
        with patch.object(netem, 'PRESENTATION_READS', cache), \
                patch.object(netem, 'presentation_source', return_value='source'), \
                patch.object(netem, 'traffic_generator_empty_snapshot', return_value={'configured': True, 'connected': False, 'error': None}), \
                patch.object(netem, '_traffic_generator_snapshot', side_effect=stalled), \
                patch.object(netem, 'SCENARIO_STATE', {'active': False}):
            try:
                now[0] = 4
                status = netem.traffic_generator_snapshot(cached=True)
                self.assertTrue(status['connected'])
                self.assertEqual(status['connection_state'], 'refreshing')
                self.assertEqual(status['status']['status'], 'stopped')
                self.assertIsNone(status['status']['users'])
                self.assertIsNone(status['status']['dem']['experience_score'])
                now[0] = 16
                status = netem.traffic_generator_snapshot(cached=True)
                self.assertEqual(status['connection_state'], 'checking')
                self.assertFalse(status['connected'])
                self.assertIsNone(status.get('status'))
            finally:
                release.set()
                with cache.lock:
                    done = cache.entries['status']['done']
                self.assertTrue(done.wait(2))

    def test_expired_diagnosis_uses_independent_connection_state(self):
        with patch.object(netem.PRESENTATION_READS, 'read', return_value={'links': [], 'findings': [], 'poll': {'expired': True}}), \
                patch.object(netem, 'traffic_generator_snapshot', return_value={
                    'connected': True, 'connection_state': 'connected', 'status': {'status': 'stopped'}}):
            snapshot = netem.current_diagnosis(cached=True)
        self.assertTrue(snapshot['traffic_generator']['connected'])
        self.assertEqual(snapshot['traffic_generator']['status']['status'], 'stopped')

    def test_pending_measurements_render_and_inline_page_scripts_are_valid(self):
        import json
        import re
        import shutil
        import subprocess
        cfg = {'mgmt_interface': 'lo', 'wan_links': [{'id': 'wan1', 'inner': 'lo', 'outer': 'lo', 'preset': 'dia'}]}
        snapshot = {'configured': True, 'connected': True, 'connection_state': 'refreshing',
                    'integration': {'host': 'simulator.invalid', 'instance_name': 'Simulator'},
                    'status': {'status': 'stopped', 'users': None, 'dem': netem.pending_simulator_dem('status_refresh_pending')},
                    'catalog': None}
        with patch.object(netem, 'load_config', return_value=cfg), \
                patch.object(netem, 'traffic_generator_snapshot', return_value=snapshot), \
                patch.object(netem, 'traffic_path_readiness', return_value={}), \
                patch.object(netem, 'appliance_under_test', return_value={}):
            for route in ('/', '/tests'):
                with self.subTest(route=route):
                    response = netem.app.test_client().get(route)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn('>None<', response.text)
                    if shutil.which('node'):
                        scripts = re.findall(r'<script>(.*?)</script>', response.text, re.S)
                        check = subprocess.run(['node', '-e',
                            'for (const source of JSON.parse(require("fs").readFileSync(0,"utf8"))) new Function(source);'],
                            input=json.dumps(scripts), capture_output=True, text=True)
                        self.assertEqual(check.returncode, 0, check.stderr)

    def test_background_status_budget_exceeds_the_simulator_aggregation_wait(self):
        with patch.object(netem, 'traffic_generator_empty_snapshot', return_value={'configured': True}), \
                patch.object(netem, 'traffic_generator_request', return_value={'status': 'stopped'}) as api:
            snapshot = netem._traffic_generator_snapshot()
        self.assertTrue(snapshot['connected'])
        api.assert_called_once_with('/api/v1/status', timeout=7.0)
