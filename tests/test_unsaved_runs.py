import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as netem


class UnsavedRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for mock in (patch.object(netem, 'RUNTIME_DIR', root),
                     patch.object(netem, 'TELEMETRY_DB_PATH', root/'telemetry.db'),
                     patch.object(netem, 'EVENT_LOG_PATH', root/'events.jsonl'),
                     patch.object(netem, 'LAST_TEST_SUMMARY_PATH', root/'last-test-summary.json'),
                     patch.dict(netem.SCENARIO_STATE, active=True, unsaved=True),
                     patch.dict(netem.TEST_RECORDER, {'active': False}, clear=True),
                     patch.object(netem, 'EVENT_LOG', [])):
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(netem.end_unsaved_telemetry)
        self.root = root

    def test_unsaved_recorder_does_not_create_configuration_or_summary(self):
        with patch.object(netem.test_records, 'write') as write, \
             patch.object(netem.test_records, 'collect_agent') as collect, \
             patch.object(netem.test_records, 'archive') as archive:
            netem.recorder_start({'id': 'trial', 'name': 'Trial'}, 'wan1', [{'name': 'Baseline'}])
            netem.record_test_sample({})
            result = netem.recorder_finish('passed', None)
        self.assertIsNone(result)
        self.assertFalse(netem.TEST_RECORDER['active'])
        write.assert_not_called()
        collect.assert_not_called()
        archive.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_events_remain_live_only_and_are_discarded_after_the_run(self):
        netem.log_event('scenario', 'Temporary trial result')
        self.assertEqual(netem.EVENT_LOG[-1]['message'], 'Temporary trial result')
        self.assertFalse((self.root/'events.jsonl').exists())
        netem.end_unsaved_telemetry()
        self.assertEqual(netem.EVENT_LOG, [])

    def test_live_telemetry_uses_memory_and_does_not_touch_saved_history(self):
        with patch.dict(netem.SCENARIO_STATE, active=False, unsaved=False):
            netem.init_telemetry_db()
        saved = (self.root/'telemetry.db').read_bytes()
        netem.begin_unsaved_telemetry()
        with netem.telemetry_connect() as conn:
            conn.execute("INSERT INTO probe_samples(timestamp,probe_id,link_id,kind,target,success) VALUES(1,'probe','wan1','http','temporary-target',1)")
        with netem.telemetry_connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM probe_samples').fetchone()[0], 1)
        self.assertEqual((self.root/'telemetry.db').read_bytes(), saved)
        netem.end_unsaved_telemetry()
        with netem.telemetry_connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM probe_samples').fetchone()[0], 0)

    def test_starting_a_session_cannot_attach_an_in_progress_unsaved_test(self):
        cfg = {'site_profile': {'industry': 'manufacturing', 'sub_industry': 'automotive',
                'function': 'plant', 'size': 'large', 'criticality': 'mission_critical'}}
        with patch.object(netem, 'load_config', return_value=cfg), \
             patch.object(netem, 'session_appliance_snapshot', return_value={}), \
             patch.object(netem, 'save_session_history') as save:
            response = netem.app.test_client().post('/sessions/start')
        self.assertEqual(response.status_code, 302)
        save.assert_not_called()

    def test_showcase_returns_to_waiting_when_trial_ends(self):
        with patch.object(netem, 'session_snapshot', return_value={'active': False}), \
             patch.object(netem, 'expire_session_if_due'), \
             patch.object(netem, 'showcase_video_public', return_value=None):
            netem.SCENARIO_STATE['active'] = False
            state = netem.showroom_snapshot()
        self.assertEqual(state['links'], [])
        self.assertEqual(state['scenario'], {'active': False})
        self.assertIsNone(state['last_test'])


if __name__ == '__main__':
    unittest.main()
