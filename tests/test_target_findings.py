"""Target misses retain application evidence and conservative cause attribution."""
import unittest
from unittest.mock import patch

import app as netem
from experience_presentation import target_details


class TargetFindingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'wan_links': [{'id': 'wan1', 'appliance_addresses': ['192.0.2.1']},
                                  {'id': 'wan2', 'appliance_addresses': ['192.0.2.2']}]}
        self.item = {'requests': 20, 'target_violations': ['loss'], 'scoring_complete': True,
                     'quality': {'udp_loss_pct': 3}, 'experience_targets': {'loss_max_pct': 1}}
        self.dem = {'since': 100, 'applications': {'voice': self.item}}
        self.raw = {'findings': [{'id': 'target_violation', 'severity': 'warn', 'applications': ['voice'],
                                 'detail': 'loss; measured utility 97/100.', 'by_egress': {'unknown': 20}}],
                    'egress': {'192.0.2.1': {'applications': {'voice': {'requests': 20}}}}}
        self.signals = [{'link_id': 'wan1', 'label': 'WAN1', 'health': 'degraded',
                         'injected': {'loss_pct': 3, 'delay_ms': 40, 'jitter_ms': 5},
                         'directions': {'down': {'rate_mbps': 3}},
                         'causes': [{'kind': 'injected_loss', 'direction': 'down', 'severity': 'warn',
                                     'text': '3% random loss injected on download'},
                                    {'kind': 'injected_delay', 'direction': 'down', 'severity': 'info',
                                     'text': '40 ms ± 5 ms delay added to every round trip'}]}]
        self.links = {'wan1': {'name': 'WAN1', 'loss_pct': 3, 'delay_ms': 40}}
        self.runtime = patch.object(netem, 'RUNTIME_EFFECTIVE', {})
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def finding(self):
        return next(item for item in netem.correlate_findings(self.raw, self.signals, self.cfg, self.dem)
                    if item['id'] == 'target_violation')

    def test_application_value_target_and_matching_likely_contributor_reach_the_story(self):
        finding = self.finding()
        self.assertEqual(finding['detail'], '3% packet loss (target ≤ 1%)')
        impact = next(iter(netem.current_impacts({'findings': [finding]}, self.links, {}).values()))
        self.assertEqual(impact['impact'], 'Voice: 3% packet loss (target ≤ 1%)')
        self.assertEqual(impact['cause'], 'Likely contributor on WAN1: 3% random loss injected on download')
        # Two failing applications in the same traffic group must not overwrite one another.
        video = dict(finding, applications=['video'])
        self.assertEqual(len(netem.current_impacts({'findings': [finding, video]}, self.links, {})), 2)
        self.raw['egress'] = {}
        self.cfg['_direct_wan_id'] = 'wan1'
        self.assertEqual(self.finding()['wans'][0]['cause_kinds'], ['injected_loss'])
        self.assertFalse(self.finding()['path_inferred'])

    def test_mixed_unknown_invalid_or_changed_paths_do_not_claim_a_likely_cause(self):
        for case in ('mixed', 'unknown', 'partial', 'invalid', 'changed', 'unverified'):
            with self.subTest(case=case):
                raw = dict(self.raw, egress=dict(self.raw['egress']))
                dem = dict(self.dem)
                signals = [dict(self.signals[0])]
                if case == 'mixed':
                    raw['egress']['192.0.2.2'] = {'applications': {'voice': {'requests': 10}}}
                    signals.append(dict(signals[0], link_id='wan2', label='WAN2'))
                if case == 'unknown': raw['egress'] = {}
                if case == 'partial': raw['egress'] = {'192.0.2.1': {'applications': {'voice': {'requests': 5}}}}
                if case == 'invalid': dem['evidence_issues'] = ['dropped_transactions']
                if case == 'changed': netem.RUNTIME_EFFECTIVE['wan1'] = {'updated_at': 105}
                if case == 'unverified': signals[0]['health'] = 'unknown'
                findings = netem.correlate_findings(raw, signals, self.cfg, dem)
                finding = next(item for item in findings if item['id'] == 'target_violation')
                self.assertTrue(all(not wan['causes'] for wan in finding['wans']))
                impact = next(iter(netem.current_impacts({'findings': [finding]}, self.links, {}).values()))
                self.assertNotIn('Likely contributor', impact['cause'])
                netem.RUNTIME_EFFECTIVE.clear()

    def test_wrong_metric_direction_or_tiny_impairment_does_not_explain_the_miss(self):
        self.item.update(target_violations=['deadline'], p95_ms=800, quality={}, experience_targets={'deadline_ms': 400})
        self.assertEqual(self.finding()['wans'][0]['causes'], [])  # 40 ms cannot explain an 800 ms completion.
        self.signals[0]['injected']['delay_ms'] = 300
        self.assertEqual(self.finding()['wans'][0]['cause_kinds'], ['injected_delay'])
        self.item.update(target_violations=['throughput_up'], quality={'transfer_up_p05_mbps': 2},
                         experience_targets={'min_up_mbps': 5})
        self.assertEqual(self.finding()['detail'], '2 Mbit/s upload (target ≥ 5 Mbit/s)')
        self.assertEqual(self.finding()['wans'][0]['causes'], [])

    def test_evidence_only_fallback_and_missing_measurements_are_explicit(self):
        self.dem['applications'] = {}
        self.raw['findings'][0]['evidence'] = {'quality': {'udp_loss_pct': 3}, 'targets': {'loss_max_pct': 1}}
        self.assertEqual(self.finding()['detail'], '3% packet loss (target ≤ 1%)')
        self.assertEqual(target_details({'experience_targets': {'loss_max_pct': 1}}, ['loss']),
                         'Packet loss: measurement unavailable (target ≤ 1%)')
        self.assertIn('target unavailable', target_details({'quality': {'udp_loss_pct': 3}}, ['loss']))
        self.assertEqual(target_details({'quality': {'deadline_misses': 2, 'deadline_basis': 'received packets'},
            'experience_targets': {'hard_deadline': True, 'deadline_ms': 100}}, ['deadline']),
            '2 late packets (target 0; deadline 100 ms)')
