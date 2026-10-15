"""Bounded timeout recovery must not hide stopped workloads or measured failures."""
import unittest
from unittest.mock import patch

import app as netem
from showcase_summary import session_facts


def timeout_error():
    error = RuntimeError('Traffic Simulator connection failed: read timed out')
    error.__cause__ = TimeoutError('read timed out')
    return error


class HeartbeatRecoveryTests(unittest.TestCase):
    def setUp(self):
        netem.SCENARIO_STOP.clear()
        self.addCleanup(netem.SCENARIO_STOP.clear)
        state = dict(active=True, workload_supervised=True, workload_run_id='run-a',
                     scenario_id='test-a', started_at=100, phase_index=1, phase='Recovery',
                     live_window_seconds=15, workload_checked=None)
        patcher = patch.dict(netem.SCENARIO_STATE, state, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        logger = patch.object(netem, 'log_event')
        logger.start()
        self.addCleanup(logger.stop)

    def running(self):
        return {'status': 'running', 'users': 2, 'run': {'run_id': 'run-a'},
                'dem': {'live_phase_id': netem.test_live_context()['phase_id']}}

    def test_periodic_check_retries_same_payload_once(self):
        with patch.object(netem, 'traffic_generator_request', side_effect=[timeout_error(), self.running()]) as api:
            netem.check_test_workload()
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0], api.call_args_list[1])
        self.assertEqual(api.call_args.kwargs['timeout'], 3.0)

    def test_retry_exhaustion_is_bounded(self):
        with patch.object(netem, 'traffic_generator_request', side_effect=timeout_error()) as api:
            with self.assertRaises(netem.SimulatorHeartbeatTimeout):
                netem.publish_test_live_context()
        self.assertEqual(api.call_count, 2)

    def test_confirmed_stop_replacement_and_zero_users_are_never_retried(self):
        for response in ({'status': 'stopped'}, dict(self.running(), run={'run_id': 'other'}),
                         dict(self.running(), users=0)):
            for check in (netem.publish_test_live_context, netem.check_test_workload):
                netem.SCENARIO_STATE['workload_checked'] = None
                with self.subTest(response=response, check=check.__name__), \
                     patch.object(netem, 'traffic_generator_request', return_value=response) as api:
                    with self.assertRaisesRegex(RuntimeError, 'stopped executing'):
                        check()
                    self.assertEqual(api.call_count, 1)

    def test_stop_after_timeout_does_not_retry(self):
        def request(*args, **kwargs):
            netem.SCENARIO_STOP.set()
            raise timeout_error()
        with patch.object(netem, 'traffic_generator_request', side_effect=request) as api:
            with self.assertRaises(InterruptedError):
                netem.publish_test_live_context()
        self.assertEqual(api.call_count, 1)

    def test_non_timeout_errors_are_never_retried(self):
        for error in (RuntimeError('HTTP 401'), RuntimeError('TLS fingerprint changed'),
                      RuntimeError('invalid JSON'), RuntimeError('connection refused')):
            with self.subTest(error=error), patch.object(netem, 'traffic_generator_request', side_effect=error) as api:
                with self.assertRaises(RuntimeError):
                    netem.publish_test_live_context()
                self.assertEqual(api.call_count, 1)

    def test_status_timeout_does_not_claim_workload_stopped(self):
        finding = netem.workload_finding(None, [], expected_run='run-a')
        self.assertEqual((finding['id'], finding['severity']), ('workload_unconfirmed', 'warn'))
        self.assertNotIn('not running', finding['title'])
        self.assertEqual(netem.workload_finding({'status': 'stopped'}, [], 'run-a')['id'], 'workload_missing')

    def test_interruption_keeps_measured_failures_but_cannot_confirm_recovery(self):
        for impact in (True, False):
            marks = [{'name': name, 'at': at} for name, at in (('Baseline', 100), ('Flapping', 160), ('Recovery', 300))]
            ends = [{'name': mark['name'], 'dem': {'requests': 100, 'experience': 100, 'success': 100,
                     'interactive_p95_ms': 50, 'scoring_complete': True, 'business_acceptance': 'pass'}} for mark in marks]
            if impact:
                ends[1]['dem'].update(experience=39, success=42, business_acceptance='fail',
                                      critical_alerts=[{'application': 'voice', 'severity': 'violation'}])
            record = {'name': 'Flaky underlay', 'started_at': 100, 'phases': [m['name'] for m in marks],
                      'phase_marks': marks, 'phase_ends': ends, 'samples': [], 'execution_result': 'interrupted'}
            summary = netem.summarize_test(record, 'failed', 'Simulator control timed out twice',
                targets={'experience_min': 80, 'success_min_pct': 99, 'interactive_p95_max_ms': 400}, ended_at=303)
            self.assertEqual(summary['execution_result'], 'interrupted')
            self.assertEqual(summary['experience_result'], 'fail' if impact else 'unknown')
            self.assertEqual(summary['phases'][-1]['experience_verdict'], 'unknown')
            self.assertIn('demonstration interrupted', summary['conclusion'][0])
            facts = session_facts([summary], str)
            self.assertEqual(facts['execution']['attention'], 1)
            self.assertEqual(facts['execution']['completed'], 0)
            self.assertEqual(facts['recovery']['unknown'], 1)
            public = netem.showroom_summary(summary)
            self.assertEqual(public['execution_result'], 'interrupted')
            with netem.app.test_request_context('/'):
                rendered = netem.app.jinja_env.get_template('_test_summary.html').module.test_summary(summary)
            self.assertIn('Demonstration interrupted', rendered)
            self.assertIn('Resilience requirements:', rendered)
