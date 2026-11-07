"""Same-day batches preserve the current candidate policy and never replay."""
import time
import unittest
from unittest.mock import patch

from pymongo.errors import ConnectionFailure

from local_app.models import Submission
from local_app.recovery_policy import BUSY_REJECTION, OCCUPIED_REJECTION
import test_today_batches as batch_fixture


class MainlineBatchTests(unittest.TestCase):
    setUpClass = classmethod(batch_fixture.TodayBatchApiTests.setUpClass.__func__)
    tearDownClass = classmethod(batch_fixture.TodayBatchApiTests.tearDownClass.__func__)
    setUp = batch_fixture.TodayBatchApiTests.setUp
    tearDown = batch_fixture.TodayBatchApiTests.tearDown
    path = batch_fixture.TodayBatchApiTests.path
    post = batch_fixture.TodayBatchApiTests.post
    segments = batch_fixture.TodayBatchApiTests.segments
    preview = batch_fixture.TodayBatchApiTests.preview
    submit = batch_fixture.TodayBatchApiTests.submit
    get = batch_fixture.TodayBatchApiTests.get
    finished = batch_fixture.TodayBatchApiTests.finished

    @staticmethod
    def rejection(pair):
        return Submission('rejected', retryable=True, code=pair[0], message=pair[1], sent=True)

    def test_occupied_middle_segment_exhausts_dynamic_candidates_and_continues(self):
        self.school.replies = [None, self.rejection(OCCUPIED_REJECTION),
                               self.rejection(OCCUPIED_REJECTION), None]
        result = self.finished(self.submit(self.preview()))
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'failed', 'success'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.school.posts, [self.seats[i] for i in (0, 1, 2, 3)])

    def test_mixed_occupied_and_busy_exhaustion_keeps_the_later_segment(self):
        self.school.replies = [None, self.rejection(OCCUPIED_REJECTION),
                               self.rejection(BUSY_REJECTION), self.rejection(BUSY_REJECTION), None]
        result = self.finished(self.submit(self.preview()))
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'failed', 'success'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.school.posts, [self.seats[i] for i in (0, 1, 2, 2, 3)])

    def test_unknown_middle_rejection_still_stops_the_remaining_segment(self):
        self.school.replies = [None, Submission('rejected', code=429, message='fixture rate limit', sent=True)]
        result = self.finished(self.submit(self.preview()))
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'failed', 'not_submitted'])
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(self.school.posts, self.seats[:2])

    def test_failure_classifier_requires_sent_ordered_exhausted_attempts(self):
        result = self.finished(self.submit(self.preview([self.segments()[0]])))
        key = result['segments'][0]['run_id']
        busy = dict(outcome='rejected', retryable=True, code=BUSY_REJECTION[0],
                    message=BUSY_REJECTION[1], sent=True, seat_id=self.seats[0])
        cases = [[], [busy], [busy | {'sent': False}, busy],
                 [busy | {'seat_id': self.seats[1]}, busy],
                 [busy, busy | {'code': 429, 'message': 'fixture limited'}]]
        for attempts in cases:
            with self.subTest(attempts=attempts):
                self.db.campus_runs.update_one({'_id': key}, {'$set': {'status': 'failed', 'attempts': attempts}})
                self.assertFalse(self.runtime.today.batches._clear_failure(key))
        self.db.campus_runs.update_one({'_id': key}, {'$set': {'status': 'failed', 'attempts': [busy, busy]}})
        self.assertTrue(self.runtime.today.batches._clear_failure(key))

    def _wait_stopped(self, batch):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            batch = self.get(batch['batch_id'])
            if batch['state'] != 'running':
                return batch
            time.sleep(.02)
        self.fail('Interrupted batch stayed running after database recovery')

    def test_execution_and_cleanup_database_errors_recover_without_dispatch(self):
        service = self.runtime.today.batches
        original, calls = service._interrupt, []

        def transient_interrupt(*args):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionFailure('fixture cleanup connection loss')
            return original(*args)

        preview = self.preview()
        with patch.object(service, '_execute', side_effect=ConnectionFailure('fixture execution loss')) as execute, \
                patch.object(service, '_interrupt', side_effect=transient_interrupt):
            result = self._wait_stopped(self.submit(preview))
            self.assertEqual(execute.call_count, 1)
        self.assertEqual([s['status'] for s in result['segments']], ['not_submitted'] * 3)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.school.posts, [])

    def test_lost_batch_result_write_recovers_receipt_without_continuing(self):
        service = self.runtime.today.batches
        segment, interrupt = service._segment, service._interrupt
        segment_faults, cleanup_calls = [], []

        def transient_segment(batch_id, index, fields):
            if not segment_faults and fields.get('status') == 'success':
                segment_faults.append(1)
                raise ConnectionFailure('fixture result connection loss')
            return segment(batch_id, index, fields)

        def transient_interrupt(*args):
            cleanup_calls.append(1)
            if len(cleanup_calls) == 1:
                raise ConnectionFailure('fixture cleanup connection loss')
            return interrupt(*args)

        with patch.object(service, '_segment', side_effect=transient_segment), \
                patch.object(service, '_interrupt', side_effect=transient_interrupt), \
                patch.object(service, '_execute', wraps=service._execute) as execute:
            result = self._wait_stopped(self.submit(self.preview()))
            self.assertEqual(execute.call_count, 1)
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'not_submitted', 'not_submitted'])
        self.assertEqual(self.school.posts, self.seats[:1])
        self.assertEqual(self.db.campus_runs.count_documents({}), 1)
