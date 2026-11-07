"""Segmented plans retain current seat policy and native ownership protections."""
from collections import deque
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Submission
from local_app.recovery_policy import BUSY_REJECTION, OCCUPIED_REJECTION, clear_busy_failure
from local_app.scheduler import cohort_handled
import test_scheduled_segments as segment_fixture


class MainlineScheduledSegmentTests(unittest.TestCase):
    setUpClass = classmethod(segment_fixture.ScheduledSegmentFlowTests.setUpClass.__func__)
    tearDownClass = classmethod(segment_fixture.ScheduledSegmentFlowTests.tearDownClass.__func__)
    setUp = segment_fixture.ScheduledSegmentFlowTests.setUp
    tearDown = segment_fixture.ScheduledSegmentFlowTests.tearDown
    execute = segment_fixture.ScheduledSegmentFlowTests.execute

    def middle_replies(self, replies):
        queue = deque(replies)
        original, outer = self.accounts.client_factory, self
        class ReplySchool(original):
            def submit(self, run, seat):
                if run.begin == '12:00' and queue:
                    outer.calls.append((run.begin, seat))
                    pair = queue.popleft()
                    return Submission('rejected', True, pair[0], pair[1], True)
                return super().submit(run, seat)
        self.accounts.discard(self.account.id)
        self.accounts.client_factory = ReplySchool
        elapsed = [0.0]
        self.reservations.monotonic = lambda: elapsed[0]
        self.reservations.wait = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)

    def test_all_occupied_middle_candidates_continue_to_the_later_segment(self):
        self.middle_replies([OCCUPIED_REJECTION, OCCUPIED_REJECTION])
        self.assertEqual(self.execute(), ['success', 'failed', 'success'])
        self.assertEqual(self.calls, [('09:00', '1'), ('12:00', '2'), ('12:00', '1'), ('16:00', '1')])
        self.assertTrue(clear_busy_failure(self.store.get_run(self.runs[1].key)))

    def test_mixed_occupied_and_busy_exhaustion_keeps_dynamic_candidate_order(self):
        self.middle_replies([OCCUPIED_REJECTION, BUSY_REJECTION, BUSY_REJECTION])
        self.assertEqual(self.execute(), ['success', 'failed', 'success'])
        self.assertEqual([seat for begin, seat in self.calls if begin == '12:00'], ['2', '1', '1'])

    def test_partial_or_unsent_history_cannot_unlock_a_later_segment(self):
        attempt = dict(seat_id='2', outcome='rejected', retryable=True,
                       code=OCCUPIED_REJECTION[0], message=OCCUPIED_REJECTION[1], sent=True)
        row = dict(status='failed', spec=dict(seat_ids=['2', '1']))
        for attempts in ([attempt], [attempt, attempt | {'seat_id': '1', 'sent': False}],
                         [attempt, attempt | {'seat_id': '1', 'code': 429}],
                         [attempt | {'seat_id': '1'}, attempt]):
            with self.subTest(attempts=attempts):
                self.assertFalse(clear_busy_failure(row | dict(attempts=attempts)))

    def test_existing_scalar_native_registration_blocks_the_whole_segmented_cohort(self):
        scalar = replace(self.runs[0], operation_id=None)
        self.store.ensure_run(scalar)
        binding = dict(job='fixture-native', epoch=1, session=1)
        self.store.runs.update_one({'_id': scalar.key}, {'$set': {
            'status': 'preparing', 'native_binding': binding}})
        self.assertTrue(cohort_handled(self.store, self.runs[0]))
        self.assertEqual(self.execute(), ['skipped'] * 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get_run(scalar.key)['native_binding'], binding)
        self.assertEqual(self.store.get_run(scalar.key)['status'], 'preparing')

    def test_native_owned_segment_is_never_reclassified_by_python_skip(self):
        binding = dict(job='fixture-native', epoch=1, session=1)
        self.store.runs.update_one({'_id': self.runs[0].key}, {'$set': {
            'status': 'preparing', 'native_binding': binding}})
        self.assertEqual(self.execute(), ['preparing', 'skipped', 'skipped'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get_run(self.runs[0].key)['native_binding'], binding)

    def test_segmented_prepare_retry_reuses_login_and_executes_the_whole_cohort(self):
        original, attempts = self.accounts.client_factory, {'login': 0, 'prepare': 0}
        class PreflightSchool(original):
            def login(self):
                attempts['login'] += 1
                return super().login()
            def prepare_booking(self):
                attempts['prepare'] += 1
                if attempts['prepare'] == 1:
                    raise AppError('network_error', 'fixture read-only preflight failed', 503)
        self.accounts.discard(self.account.id)
        self.accounts.client_factory = PreflightSchool
        key = (self.runs[0].key, self.account.revision)
        self.scheduler.tick()
        self.scheduler.jobs[key]['prep'].result(timeout=3)
        self.scheduler.tick()
        self.now += 1
        self.scheduler.tick()
        self.scheduler.jobs[key]['prep'].result(timeout=3)
        self.assertEqual(attempts, {'login': 1, 'prepare': 2})
        self.now = segment_fixture.DUE - 1.5
        self.scheduler.tick()
        self.scheduler.jobs[key]['warm'].result(timeout=3)
        self.now = segment_fixture.DUE
        self.scheduler.tick()
        self.scheduler.jobs[key]['submit'].result(timeout=3)
        self.assertEqual([self.store.get_run(run.key)['status'] for run in self.runs], ['success'] * 3)
        self.assertEqual(len(self.calls), 3)


if __name__ == '__main__':
    unittest.main()
