"""Failed batch dispatch is terminalized without replay, including lost write acks."""
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from pymongo.errors import ConnectionFailure, DuplicateKeyError

import test_today_batches as fixture


class BatchClaimCleanupTests(unittest.TestCase):
    setUpClass = classmethod(fixture.TodayBatchApiTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.TodayBatchApiTests.tearDownClass.__func__)
    setUp = fixture.TodayBatchApiTests.setUp
    tearDown = fixture.TodayBatchApiTests.tearDown
    path = fixture.TodayBatchApiTests.path
    post = fixture.TodayBatchApiTests.post
    segments = fixture.TodayBatchApiTests.segments
    preview = fixture.TodayBatchApiTests.preview

    def stopped(self, preview):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            row = self.db.campus_today_batches.find_one({'_id': preview['batch_id']})
            if row and row['state'] == 'stopped':
                self.assertEqual([s['status'] for s in row['segments']], ['not_submitted'] * 3)
                self.assertEqual(self.school.posts, [])
                self.assertEqual(self.db.campus_runs.count_documents({}), 0)
                return row
            time.sleep(.01)
        self.fail('Failed dispatch did not reach a terminal state')

    def failed_submit(self, preview):
        response = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 503, response.json)

    def assert_no_replay(self, preview):
        response = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['state'], 'stopped')
        self.assertEqual(self.school.posts, [])
        # Only the failed claim releases its slot, even if a queued wrapper existed.
        service = self.runtime.today.batches
        for _ in range(service.MAX_PENDING):
            self.assertTrue(service._slots.acquire(blocking=False))
        self.assertFalse(service._slots.acquire(blocking=False))
        for _ in range(service.MAX_PENDING):
            service._slots.release()

    def test_committed_insert_with_lost_ack_stops_and_releases_account(self):
        preview = self.preview()
        batches = self.runtime.today.batches.batches
        insert = batches.insert_one
        def lose_ack(document):
            insert(document)
            raise ConnectionFailure('fixture: committed, acknowledgement lost')
        with patch.object(batches, 'insert_one', side_effect=lose_ack):
            self.failed_submit(preview)
        self.stopped(preview)
        self.assert_no_replay(preview)
        self.preview()

    def test_cleanup_retries_unavailable_read_after_lost_insert_ack(self):
        preview = self.preview()
        batches = self.runtime.today.batches.batches
        insert, find = batches.insert_one, batches.find_one
        failures = []
        def lose_ack(document):
            insert(document)
            raise ConnectionFailure('fixture: committed, acknowledgement lost')
        def unavailable_read(*args, **kwargs):
            if threading.current_thread().name == 'today-batch-cleanup' and len(failures) < 2:
                failures.append(1)
                raise ConnectionFailure('fixture: cleanup read unavailable')
            return find(*args, **kwargs)
        with patch.object(batches, 'insert_one', side_effect=lose_ack), \
                patch.object(batches, 'find_one', side_effect=unavailable_read):
            self.failed_submit(preview)
            self.stopped(preview)
        self.assertEqual(len(failures), 2)
        self.assert_no_replay(preview)

    def test_tombstone_prevents_late_insert_after_initial_missing_read(self):
        preview = self.preview()
        batches = self.runtime.today.batches.batches
        insert, delayed = batches.insert_one, []
        def delay_insert(document):
            if document['state'] == 'running':
                delayed.append(deepcopy(document))
                raise ConnectionFailure('fixture: write outcome not yet visible')
            return insert(document)
        with patch.object(batches, 'insert_one', side_effect=delay_insert):
            self.failed_submit(preview)
            self.stopped(preview)
        self.assertEqual(len(delayed), 1)
        with self.assertRaises(DuplicateKeyError):
            insert(delayed[0])
        self.assert_no_replay(preview)

    def test_duplicate_missing_read_during_cleanup_never_starts_another_claim(self):
        preview = self.preview()
        service = self.runtime.today.batches
        batches = service.batches
        insert, delayed = batches.insert_one, []
        def delay_first_insert(document):
            if document['state'] == 'running' and not delayed:
                delayed.append(deepcopy(document))
                raise ConnectionFailure('fixture: insert not visible')
            return insert(document)
        with patch.object(batches, 'insert_one', side_effect=delay_first_insert), \
                patch.object(service, '_stop_unstarted_claim', side_effect=ConnectionFailure('fixture: cleanup unavailable')):
            self.failed_submit(preview)
            self.failed_submit(preview)
            self.assertEqual(self.school.posts, [])
            self.assertEqual(batches.count_documents({}), 0)
        self.stopped(preview)
        self.assert_no_replay(preview)

    def test_worker_start_failure_retries_failed_terminal_write(self):
        preview = self.preview()
        service = self.runtime.today.batches
        update = service.batches.update_one
        failures = []
        def fail_cleanup(*args, **kwargs):
            if (args[0].get('_id') == preview['batch_id']
                    and args[1].get('$set', {}).get('state') == 'stopped' and len(failures) < 2):
                failures.append(1)
                raise ConnectionFailure('fixture: terminal write unavailable')
            return update(*args, **kwargs)
        with patch.object(service.pool, 'submit', side_effect=RuntimeError('fixture: worker unavailable')), \
                patch.object(service.batches, 'update_one', side_effect=fail_cleanup):
            self.failed_submit(preview)
            self.stopped(preview)
        self.assertEqual(len(failures), 2)
        self.assert_no_replay(preview)

    def test_enqueued_worker_does_not_execute_when_submit_later_raises(self):
        preview = self.preview()
        service = self.runtime.today.batches
        submit, queued = service.pool.submit, []
        def enqueue_then_fail(*args, **kwargs):
            queued.append(submit(*args, **kwargs))
            raise RuntimeError('fixture: worker enqueued before submit error')
        with patch.object(service.pool, 'submit', side_effect=enqueue_then_fail):
            self.failed_submit(preview)
        self.stopped(preview)
        queued[0].result(timeout=1)
        self.assert_no_replay(preview)

    def preserve_foreign_claim(self, field, value):
        preview = self.preview()
        batches = self.runtime.today.batches.batches
        insert, foreign = batches.insert_one, []
        def other_claim(document):
            row = deepcopy(document)
            row[field] = value
            insert(row)
            foreign.append(deepcopy(row))
            raise ConnectionFailure('fixture: foreign claim became visible')
        with patch.object(batches, 'insert_one', side_effect=other_claim):
            self.failed_submit(preview)
        time.sleep(.6)
        self.assertEqual(batches.find_one({'_id': preview['batch_id']}), foreign[0])
        self.assertEqual(self.school.posts, [])

    def test_failed_insert_cleanup_preserves_a_foreign_digest(self):
        self.preserve_foreign_claim('claim_digest', 'another-authority')

    def test_failed_insert_cleanup_preserves_a_foreign_account(self):
        self.preserve_foreign_claim('account_id', 'another-account')

    def test_failed_insert_cleanup_preserves_a_foreign_claim_owner(self):
        self.preserve_foreign_claim('claim_owner', 'another-dispatch-owner')

    def test_failed_insert_cleanup_preserves_changed_execution_evidence(self):
        preview = self.preview()
        batches = self.runtime.today.batches.batches
        insert, active = batches.insert_one, []
        def active_claim(document):
            row = deepcopy(document)
            row['segments'][0].update(status='running', run_id='fixture-active-run')
            insert(row)
            active.append(deepcopy(row))
            raise ConnectionFailure('fixture: execution evidence became visible')
        with patch.object(batches, 'insert_one', side_effect=active_claim):
            self.failed_submit(preview)
        time.sleep(.6)
        self.assertEqual(batches.find_one({'_id': preview['batch_id']}), active[0])
        self.assertEqual(self.school.posts, [])
