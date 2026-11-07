"""HTTP timing excludes ownership and permission waits without moving the guards."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Identity, RunSpec
from local_app.school import CampusClient


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def read(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class OwnershipGuard:
    def __init__(self, clock, *, denied=False):
        self.clock, self.denied, self.active = clock, denied, False

    def __call__(self):
        return True

    @contextmanager
    def post(self, run):
        self.clock.advance(.2)
        if self.denied:
            raise AppError('dual_ownership_denied', 'Fixture ownership denied', 409)
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.clock.advance(.3)


class TimedTransport:
    def __init__(self, clock, ownership, *, fails=False):
        self.clock, self.ownership, self.fails = clock, ownership, fails
        self.headers, self.calls = {}, []

    def request(self, method, url, **kwargs):
        if self.ownership is not None and not self.ownership.active:
            raise AssertionError('HTTP must stay inside the ownership scope')
        self.calls.append((method, self.clock.read()))
        self.clock.advance(.01)
        if self.fails:
            raise requests.Timeout('Fixture timeout')
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":0}'
        return response


class HttpTimingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for name in ('perf_counter', 'time'):
            patched = patch('local_app.school.time.' + name, self.clock.read)
            patched.start()
            self.addCleanup(patched.stop)

    def submit(self, *, final_allowed=True, ownership_denied=False, network_fails=False):
        ownership = OwnershipGuard(self.clock, denied=ownership_denied)
        transport = TimedTransport(self.clock, ownership, fails=network_fails)
        client = CampusClient('fixture01', 'pass', transport=transport, allow_live=True)
        client.identity = Identity('fixture01', 'Fixture', 123)
        client.ownership_guard = ownership
        client._take_prepared = lambda run, seat_id: {'fixture': True}
        checks = []

        def submission_guard():
            checks.append(ownership.active)
            self.clock.advance(.05 if len(checks) == 1 else .07)
            return len(checks) == 1 or final_allowed

        client.submission_guard = submission_guard
        run = RunSpec('a', 1, '2026-11-08', '09:00', '12:00', ('1',), 0)
        result = client.submit(run, '1')
        return result, transport, checks

    def assert_no_http_timing(self, result):
        for field in ('request_started_at', 'request_finished_at', 'request_ms'):
            self.assertNotIn(field, result.timings)

    def submit_without_ownership(self, *, permitted=True, deadline=None, network_fails=False):
        transport = TimedTransport(self.clock, None, fails=network_fails)
        client = CampusClient('fixture01', 'pass', transport=transport, allow_live=True)
        client.identity = Identity('fixture01', 'Fixture', 123)
        client._take_prepared = lambda run, seat_id: {'fixture': True}
        checks = []

        def submission_guard():
            checks.append(self.clock.read())
            self.clock.advance(.06)
            return permitted and (deadline is None or self.clock.read() <= deadline)

        client.submission_guard = submission_guard
        run = RunSpec('a', 1, '2026-11-08', '09:00', '12:00', ('1',), 0)
        return client.submit(run, '1'), transport, checks

    def test_unowned_post_has_one_final_guard_and_excludes_its_wait(self):
        result, transport, checks = self.submit_without_ownership()
        self.assertEqual(result.outcome, 'accepted')
        self.assertIs(result.sent, True)
        self.assertEqual(checks, [1000.0])
        self.assertEqual(len(transport.calls), 1)
        self.assertAlmostEqual(transport.calls[0][1], 1000.06)
        self.assertEqual(result.timings['request_started_at'], transport.calls[0][1])
        self.assertEqual(result.timings['guard_ms'], 60)
        self.assertEqual(result.timings['request_ms'], 10)

    def test_unowned_final_guard_refusal_never_posts(self):
        result, transport, checks = self.submit_without_ownership(permitted=False)
        self.assertEqual(result.outcome, 'rejected')
        self.assertIs(result.sent, False)
        self.assertEqual(checks, [1000.0])
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.timings['guard_ms'], 60)
        self.assert_no_http_timing(result)

    def test_unowned_deadline_crossed_during_final_guard_never_posts(self):
        result, transport, checks = self.submit_without_ownership(deadline=1000.05)
        self.assertEqual(result.outcome, 'rejected')
        self.assertIs(result.sent, False)
        self.assertEqual(checks, [1000.0])
        self.assertEqual(transport.calls, [])
        self.assert_no_http_timing(result)

    def test_unowned_transport_timeout_is_not_retried_or_timed_as_guard_wait(self):
        result, transport, checks = self.submit_without_ownership(network_fails=True)
        self.assertEqual(result.outcome, 'uncertain')
        self.assertEqual(checks, [1000.0])
        self.assertEqual(len(transport.calls), 1)
        self.assertAlmostEqual(result.timings['request_started_at'], 1000.06)
        self.assertAlmostEqual(result.timings['request_finished_at'], 1000.07)
        self.assertEqual(result.timings['guard_ms'], 60)
        self.assertEqual(result.timings['request_ms'], 10)

    def test_http_only_covers_transport_inside_ownership_scope(self):
        result, transport, checks = self.submit()
        self.assertEqual(result.outcome, 'accepted')
        self.assertIs(result.sent, True)
        self.assertEqual(checks, [False, True])
        self.assertEqual(len(transport.calls), 1)
        self.assertAlmostEqual(result.timings['request_started_at'], 1000.32)
        self.assertEqual(result.timings['request_started_at'], transport.calls[0][1])
        self.assertAlmostEqual(result.timings['request_finished_at'], 1000.33)
        self.assertEqual(result.timings['request_ms'], 10)
        self.assertEqual(result.timings['guard_ms'], 320)
        self.assertAlmostEqual(self.clock.read(), 1000.63)

    def test_final_guard_refusal_never_posts_or_fabricates_http_timing(self):
        result, transport, checks = self.submit(final_allowed=False)
        self.assertEqual(result.outcome, 'rejected')
        self.assertIs(result.sent, False)
        self.assertEqual(checks, [False, True])
        self.assertEqual(transport.calls, [])
        self.assert_no_http_timing(result)
        self.assertEqual(result.timings['guard_ms'], 320)

    def test_ownership_refusal_never_posts_or_fabricates_http_timing(self):
        result, transport, checks = self.submit(ownership_denied=True)
        self.assertEqual(result.outcome, 'rejected')
        self.assertIs(result.sent, False)
        self.assertEqual(checks, [False])
        self.assertEqual(transport.calls, [])
        self.assert_no_http_timing(result)
        self.assertEqual(result.timings['guard_ms'], 250)

    def test_transport_timeout_records_only_http_time_and_does_not_retry(self):
        result, transport, checks = self.submit(network_fails=True)
        self.assertEqual(result.outcome, 'uncertain')
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(checks, [False, True])
        self.assertAlmostEqual(result.timings['request_started_at'], 1000.32)
        self.assertAlmostEqual(result.timings['request_finished_at'], 1000.33)
        self.assertEqual(result.timings['request_ms'], 10)
        self.assertEqual(result.timings['guard_ms'], 320)


if __name__ == '__main__':
    unittest.main()
