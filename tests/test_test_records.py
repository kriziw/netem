import io
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app as netem
import test_records


class TestRecordTests(unittest.TestCase):
    def test_record_is_durable_redacted_and_downloadable_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = 'a' * 32
            test_records.write(root, identity, 'configuration.json', {'started_at': 1, 'scenario': {'name': 'Brownout'},
                'api_key': 'hidden', 'target': 'https://user:pass@target/path?token=hidden'})
            test_records.append(root, identity, 'measurement', {'score': 98, 'error': 'Bearer hidden'})
            with patch.object(netem, 'RUNTIME_DIR', root):
                response = netem.app.test_client().get(f'/tests/records/{identity}/download')
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment', response.headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
                contents = '\n'.join(bundle.read(name).decode() for name in bundle.namelist())
                self.assertNotIn('hidden', contents)
                self.assertNotIn('user:pass', contents)
                self.assertIn('98', contents)
                self.assertFalse(json.loads(bundle.read('bundle-manifest.json'))['complete'])
                for name, digest in json.loads(bundle.read('bundle-manifest.json'))['sha256'].items():
                    self.assertEqual(hashlib.sha256(bundle.read(name)).hexdigest(), digest)
            response.close()
            self.assertEqual(test_records.history(root)[0]['result'], 'unfinished')
            self.assertEqual(netem.app.test_client().get('/tests/records/not-an-id/download').status_code, 404)

    def test_agent_export_keeps_pages_and_marks_unavailable_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, identity = Path(temporary), 'b' * 32
            def fetch(path, **kwargs):
                from urllib.parse import urlsplit, parse_qs
                query = parse_qs(urlsplit(path).query)
                kind = query['kind'][0]
                if kind == 'operations':
                    raise RuntimeError('Agent not available')
                cursor = int(query['cursor'][0])
                return {'run_id': 'run', 'kind': kind, 'ceiling': 2, 'items': [{'id': cursor+1}],
                        'next_cursor': 1 if cursor == 0 else None, 'complete': cursor > 0}
            result = test_records.collect_agent(root, identity, ['run'], 1, 2, fetch)
            self.assertFalse(result['complete'])
            self.assertEqual(result['runs']['run']['transactions']['rows'], 2)
            self.assertIn('Agent not available', str(result['errors']))
            self.assertTrue((test_records.directory(root, identity) / 'agent-0-transactions-001.json').exists())

    def test_finished_test_archives_full_record_and_has_download_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(netem, 'RUNTIME_DIR', root), \
                 patch.object(netem, 'TELEMETRY_DB_PATH', root / 'telemetry.db'), \
                 patch.object(netem, 'LAST_TEST_SUMMARY_PATH', root / 'last-summary.json'), \
                 patch.object(netem, 'load_config', return_value={}), \
                 patch.object(netem, 'active_site_plan', return_value=None), \
                 patch.dict(netem.TEST_RECORDER, {}, clear=True), \
                 patch.dict(netem.SCENARIO_STATE, {'workload_run_id': 'run'}, clear=True), \
                 patch.object(netem, 'traffic_generator_request', side_effect=RuntimeError('Unavailable')):
                netem.init_telemetry_db()
                netem.recorder_start({'id': 'test', 'name': 'Test'}, 'wan1', [])
                netem.recorder_note('assertions', {'label': 'A check', 'passed': True})
                summary = netem.recorder_finish('passed', None)
                self.assertFalse(summary['record_complete'])
                folder = test_records.directory(root, summary['test_id'])
                with zipfile.ZipFile(folder / 'record.zip') as bundle:
                    self.assertIn('record.json', bundle.namelist())
                    self.assertIn('telemetry_samples.json', bundle.namelist())
                    self.assertIn('A check', bundle.read('timeline.jsonl').decode())
                    self.assertFalse(json.loads(bundle.read('agent-export-status.json'))['complete'])

    def test_small_sample_has_metrics_without_a_target_pass(self):
        item = netem._phase_application('erp', {'requests': 2, 'experience': 50, 'p95_ms': 800,
            'success': 100, 'scoring_complete': False, 'sample_confidence': 'limited_sample'})
        self.assertEqual(item['experience'], 50)
        self.assertFalse(item['scoring_complete'])
        result = netem.attach_experience_groups({'applications': {'erp': {
            'class': 'interactive', 'requests': 2, 'experience_score': 50, 'scoring_complete': False,
            'sample_confidence': 'limited_sample'}}})['experience_groups'][1]
        self.assertIn('limited sample', result['text'])
        self.assertEqual(result['score'], 50)
