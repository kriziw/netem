"""Slow simulator reads must neither block screens nor stampede the simulator."""
import concurrent.futures
import threading
import unittest

from presentation_cache import PresentationCache


class CacheTests(unittest.TestCase):
    def test_cold_read_returns_without_waiting_and_many_screens_share_one_load(self):
        cache = PresentationCache()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow():
            calls.append(1)
            entered.set()
            release.wait(2)
            return {'connected': True, 'dem': {'score': 90}}
        try:
            value = cache.read('status', 'server-a', slow, {'connected': False})
            self.assertFalse(value['connected'])
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                self.assertFalse(cache.read('status', 'server-a', slow, {'connected': False})['connected'])
            # A real control read coalesces with the already-running request, but waits.
            with concurrent.futures.ThreadPoolExecutor() as pool:
                fresh = pool.submit(cache.fresh, 'status', 'server-a', slow)
                self.assertFalse(fresh.done())
                release.set()
                self.assertTrue(fresh.result(1)['connected'])
            value = cache.read('status', 'server-a', slow, {})
            self.assertTrue(value['connected'])
            value['dem']['score'] = 0
            self.assertEqual(cache.read('status', 'server-a', slow, {})['dem']['score'], 90)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()

    def test_expired_evidence_is_unavailable_and_other_endpoint_cannot_reuse_it(self):
        now = [0]
        cache = PresentationCache(clock=lambda: now[0])
        cache.fresh('status', 'server-a', lambda: {'connected': True})
        now[0] = 4
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return {'connected': True}
        try:
            self.assertFalse(cache.read('status', 'server-a', slow, {'connected': False}, max_stale=3)['connected'])
            self.assertTrue(entered.wait(1))
            self.assertFalse(cache.read('status', 'server-b', slow, {'connected': False})['connected'])
        finally:
            release.set()

    def test_background_capacity_is_bounded_and_failures_back_off(self):
        cache = PresentationCache(workers=1)
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return {}
        try:
            cache.read('a', 'server', slow, {})
            self.assertTrue(entered.wait(1))
            calls = []
            cache.read('b', 'server', lambda: calls.append(1), {})
            self.assertFalse(calls)
        finally:
            release.set()
        # fresh() propagates errors; presentation reads retain no expired success.
        failed = PresentationCache(clock=lambda: 10)
        def error():
            raise RuntimeError('offline')
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            failed.fresh('status', 'server', error)
        calls = []
        self.assertFalse(failed.read('status', 'server', lambda: calls.append(1), {'connected': False})['connected'])
        self.assertFalse(calls)
