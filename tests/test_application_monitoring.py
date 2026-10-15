import unittest
from unittest.mock import patch

import app as netem
from application_monitoring import CAPABILITY, presentation, start_payload


class ApplicationMonitoringTests(unittest.TestCase):
    def test_small_site_enables_checks_with_capability_without_changing_population(self):
        catalog = {'engine_capabilities': [CAPABILITY]}
        payload = {'users': 2, 'personas': {'knowledge_worker': 2}}
        result = start_payload(payload, catalog)
        self.assertTrue(result['application_checks'])
        self.assertEqual(result['users'], 2)
        self.assertEqual(result['personas'], payload['personas'])
        self.assertNotIn('application_checks', payload)
        for raw in ({'users': 100}, {'users': 2, 'generation_mode': 'rate'}, {'users': 2, 'paced_mbps': 5}):
            self.assertNotIn('application_checks', start_payload(raw, catalog))
        self.assertFalse(start_payload({'users': 2, 'application_checks': False}, catalog)['application_checks'])
        self.assertNotIn('application_checks', start_payload({'users': 2, 'application_checks': True}, {}))

    def test_baseline_is_bounded_and_cannot_hide_failure(self):
        run = {'application_checks': True, 'started_at': 100,
               'manifest': {'application_shares': {'web_saas': 1, 'voice': 1}}}
        dem = {'applications': {'web_saas': {'requests': 3}}, 'scoring_complete': False,
               'measurement_sources': {'application_checks': 3, 'user_transactions': 2}}
        result = presentation(run, dem, 105)
        self.assertTrue(result['preparing'])
        self.assertEqual((result['measured_apps'], result['expected_apps'], result['checks']), (1, 2, 3))
        self.assertFalse(presentation(run, dem, 121)['preparing'])
        self.assertFalse(presentation(run, dict(dem, business_acceptance='fail'), 105)['preparing'])
        self.assertFalse(presentation(run, dict(dem, scoring_complete=True), 105)['preparing'])

    def test_showroom_publishes_counts_without_raw_execution_details(self):
        run = {'application_checks': True, 'started_at': 100, 'target': 'https://secret',
               'traffic_path': {'source': '192.0.2.3'}, 'application_check_errors': {'web_saas': 'private error'}}
        dem = {'requests': 20, 'measurement_sources': {'application_checks': 18, 'user_transactions': 2}}
        diagnosis = {'traffic_generator': {'connected': True, 'status': {'users': 2, 'run': run, 'dem': dem}}}
        with patch.object(netem.time, 'time', return_value=110):
            result = netem.showroom_outcome(diagnosis, None)['traffic']
        self.assertEqual(result['monitoring']['checks'], 18)
        self.assertEqual(result['monitoring']['worker_errors'], 1)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('192.0.2.3', str(result))
        self.assertNotIn('private error', str(result))
