"""Customer implications must be based on application evidence, never an injected fault."""
import unittest
from showcase_summary import user_effects


def application(kind='interactive', **extra):
    return dict({'class': kind, 'requests': 20, 'scoring_complete': True,
                 'target_violations': [], 'target_observations': []}, **extra)


class UserEffectsTests(unittest.TestCase):
    def test_three_groups_and_no_impact_claim_without_measurements(self):
        effects = user_effects({})
        self.assertEqual([item['key'] for item in effects], ['realtime', 'interactive', 'bulk'])
        self.assertTrue(all(item['state'] == 'unknown' for item in effects))

    def test_confirmed_failures_describe_consequences_for_each_group(self):
        effects = user_effects({'applications': {
            'voice': application('realtime', target_violations=['loss']),
            'web': application(target_violations=['success']),
            'files': application('bulk', target_violations=['deadline']),
        }})
        self.assertIn('calls may break up', effects[0]['text'])
        self.assertIn('retry a task', effects[1]['text'])
        self.assertIn('uploads and downloads may take longer', effects[2]['text'])
        self.assertTrue(all(item['state'] == 'bad' for item in effects))

    def test_partial_evidence_cannot_imply_a_good_user_experience(self):
        for extra in ({'scoring_complete': False}, {'evidence_issues': ['application_check_generator_lag']},
                      {'target_observations': ['deadline']}, {'requests': 0}):
            effect = user_effects({'applications': {'web': application(**extra)}})[1]
            self.assertEqual(effect['state'], 'unknown')

    def test_valid_complete_group_is_good_without_borrowing_another_group(self):
        effects = user_effects({'applications': {'web': application()}})
        self.assertEqual(effects[1]['state'], 'good')
        self.assertEqual(effects[0]['state'], 'unknown')
        self.assertEqual(effects[2]['state'], 'unknown')

    def test_pending_operations_and_global_evidence_issues_cannot_earn_a_green_status(self):
        for extra in ({'at_risk': True}, {'evidence_issues': ['application_check_worker_error']}):
            self.assertEqual(user_effects({**extra, 'applications': {'web': application()}})[1]['state'], 'unknown')

    def test_pause_and_invalid_evidence_do_not_reuse_healthy_or_failed_effects(self):
        for state in ({'live_paused': True}, {'truncated': True}, {'evidence_issues': ['clock_discontinuity']}):
            for violations in ([], ['success']):
                effects = user_effects({**state, 'applications': {'web': application(target_violations=violations)}})
                self.assertTrue(all(item['state'] == 'unknown' for item in effects))
