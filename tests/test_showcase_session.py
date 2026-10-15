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
        self.assertEqual(facts['execution'], {'completed': 3, 'attention': 0, 'stopped': 1})

    def test_user_impact_requires_measured_application_misses_and_survives_summary(self):
        dem = {'applications': {
            'voice': {'class': 'realtime', 'requests': 30, 'scoring_complete': True, 'target_violations': ['loss']},
            'erp': {'class': 'interactive', 'requests': 30, 'scoring_complete': True, 'target_violations': ['success']},
            'backup': {'class': 'bulk', 'requests': 30, 'scoring_complete': True, 'target_violations': ['throughput_up']}}}
        messages = netem.showcase_summary.application_impact(dem)
        self.assertEqual(len(messages), 3)
        self.assertIn('calls may break up', messages[0])
        self.assertIn('retry a task', messages[1])
        self.assertIn('uploads and downloads', messages[2])
        outcome = netem.showroom_outcome({'traffic_generator': {'connected': True, 'status': {'dem': dem}}}, None)
        self.assertEqual(outcome['experience']['user_impact'], ' '.join(messages))
        phase_dem = dict(dem, requests=90, experience=39, success=42, interactive_p95_ms=50, scoring_complete=True)
        recorded = netem.summarize_test({'name': 'Brownout', 'started_at': 1, 'phases': ['Flapping'],
            'phase_marks': [{'name': 'Flapping', 'at': 1}],
            'phase_ends': [{'name': 'Flapping', 'dem': phase_dem}]}, 'passed', None, ended_at=20)
        self.assertEqual(recorded['user_impact'], ' '.join(messages))
        self.assertEqual(netem.showroom_summary(recorded)['user_impact'], ' '.join(messages))
        summary = {'name': 'Brownout', 'experience_result': 'fail', 'result': 'passed', 'ended_at': 10,
                   'phases': [{'name': 'Brownout', 'reached': True, 'user_impact': messages},
                              {'name': 'Recovery', 'reached': True, 'experience_verdict': 'unknown'}]}
        facts = session_facts([summary], str)
        self.assertEqual(facts['latest']['user_impact'], ' '.join(messages))
        self.assertEqual(facts['recovery']['unknown'], 1)
        self.assertIn('retry a task', facts['demonstration_results'][0]['user_impact'])
        for invalid in ('row_limit', 'configuration_changed_in_window', 'persistence_unhealthy'):
            self.assertEqual(netem.showcase_summary.application_impact(dict(dem, evidence_issues=[invalid])), [])
        self.assertEqual(netem.showcase_summary.application_impact(dict(dem, truncated=True)), [])
        for item in dem['applications'].values():
            item['scoring_complete'] = False
        self.assertEqual(netem.showcase_summary.application_impact(dem), [])

    def test_demonstration_completion_impact_and_recovery_are_independent(self):
        summary = {'name': 'Brownout', 'execution_result': 'passed', 'experience_result': 'fail',
                   'ended_at': 10, 'phases': [
                       {'name': 'Severe brownout', 'experience_score': 0, 'note': 'Incomplete evidence',
                        'target_failures': ['Voice: 3% loss (target ≤ 1%)']},
                       {'name': 'Recovery', 'reached': True, 'experience_score': 100, 'experience_verdict': 'pass'}],
                   'conclusion': ['Execution completed.', 'Recovered.', 'Address 192.0.2.1']}
        facts = session_facts([summary], netem.showroom_text)
        self.assertEqual(facts['execution']['completed'], 1)
        self.assertEqual(facts['outcomes']['fail'], 1)
        self.assertEqual(facts['recovery']['pass'], 1)
        self.assertEqual(facts['latest']['recovery'], 'pass')
        self.assertIn('Voice: 3% loss', facts['demonstration_results'][0]['unmet_requirements'][0])
        self.assertTrue(facts['lowest_is_provisional'])
        self.assertNotIn('192.0.2.1', str(facts))
        # Old or incomplete phase records cannot prove recovery from a high score alone.
        summary['phases'][-1].pop('experience_verdict')
        self.assertEqual(session_facts([summary], str)['recovery']['unknown'], 1)
        summary['phases'][-1]['experience_verdict'] = 'fail'
        summary['execution_result'] = 'failed'
        facts = session_facts([summary], str)
        self.assertEqual(facts['execution']['completed'], 0)
        self.assertEqual(facts['execution']['attention'], 1)
        self.assertEqual(facts['recovery']['fail'], 1)

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
