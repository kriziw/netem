import unittest
from unittest.mock import patch

import app as netem
from showcase_summary import briefing, session_facts


class SessionShowcaseTests(unittest.TestCase):
    def test_session_counts_use_experience_acceptance_not_execution_status(self):
        summaries = [
            {'name': 'Baseline', 'result': 'passed', 'experience_result': 'pass', 'ended_at': 10,
             'phases': [{'experience_score': 100, 'interactive_p95_ms': 50}]},
            {'name': 'Brownout', 'result': 'passed', 'experience_result': 'fail', 'ended_at': 20,
             'phases': [{'experience_score': 30, 'interactive_p95_ms': 800}]},
            {'name': 'Sparse', 'result': 'passed', 'experience_result': 'unknown', 'ended_at': 30,
             'phases': [{'experience_score': 20, 'note': 'Limited sample', 'interactive_p95_ms': 1000}],
             'conclusion': ['Observed at 192.0.2.1']},
            {'name': 'Stopped', 'result': 'stopped', 'ended_at': 25},
        ]
        facts = session_facts(summaries, netem.showroom_text)
        self.assertEqual(facts['tests_completed'], 3)
        self.assertEqual(facts['tests_stopped'], 1)
        self.assertEqual(facts['outcomes'], {'pass': 1, 'fail': 1, 'unknown': 2})
        self.assertEqual(facts['lowest_experience'], 20)
        self.assertTrue(facts['lowest_is_provisional'])
        self.assertEqual(facts['slowest_phase_completion_p95_ms'], 1000)
        self.assertNotIn('192.0.2.1', str(facts))
        self.assertEqual(facts['latest']['outcome'], 'unknown')

    def test_only_current_session_history_is_counted_and_repeated_runs_survive(self):
        def event(session_id, start, end):
            return {'kind': 'test-summary', 'details': {'session_id': session_id, 'summary': {
                'scenario_id': 'same-test', 'name': 'Repeated test', 'started_at': start,
                'ended_at': end, 'result': 'passed', 'experience_result': 'unknown'}}}
        events = [event('current', 60, 70), event('current', 80, 90), event('other', 80, 90), event('current', 1, 2)]
        session = {'active': True, 'id': 'current', 'started_at': 50}
        with patch.object(netem, 'read_session_events', return_value=events) as read, \
             patch.object(netem, 'EVENT_LOG', [events[1]]), \
             patch.object(netem, 'LAST_TEST_SUMMARY', {}), \
             patch.object(netem.time, 'time', return_value=100), \
             patch.dict(netem.SHOWROOM_SESSION_PROGRESS, {}, clear=True):
            first = netem.showroom_session_progress(session)
            self.assertEqual(first['tests_completed'], 2)  # Disk/memory duplicate isn't a third run.
            self.assertEqual(netem.showroom_session_progress(session), first)
            read.assert_called_once()
            self.assertIsNone(netem.showroom_session_progress({'active': False}))
            self.assertEqual(netem.showroom_session_progress(dict(session, id='new', started_at=95))['tests_completed'], 0)

    def test_briefing_explains_measurements_impairment_recovery_and_sample_window(self):
        brief = briefing({'steps': [{'action': 'fault', 'value': 'blackhole'},
                                   {'action': 'fault', 'value': 'normal', 'phase': 'Recovery'}]},
                         {'experience_min': 80, 'success_min_pct': 99, 'interactive_p95_max_ms': 400,
                          'steering_max_s': 30}, 15)
        self.assertIn('≥ 80/100', str(brief['measurements']))
        self.assertIn('≤ 400 ms', str(brief['measurements']))
        self.assertIn('RTD/RTT', str(brief['measurements']))
        self.assertIn('During impairment', str(brief['watch']))
        self.assertIn('In recovery', str(brief['watch']))
        self.assertIn('15-second', str(brief['watch']))
        self.assertEqual(netem.TEST_INTRO_S, 30)
