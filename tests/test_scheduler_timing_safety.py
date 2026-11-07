"""Timing diagnostics cannot change booking results or cross run revisions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import RunSpec, SchoolRecord, Submission, ZONE
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from support.account_fixture import School, services
from support.mongo_fixture import MongoFixture


class AcceptedSchool(School):
    active_statuses = frozenset({1027})
    posts = []
    def submit(self, run, seat):
        self.posts.append(run.key);self.accepted = (run, seat)
        return Submission('accepted', code=0, sent=True)
    def query(self, begin_date, end_date):
        run, seat = self.accepted
        return [SchoolRecord('fixture-record', self.student_id, seat,
            run.target_date+' '+run.begin+':00', run.target_date+' '+run.end+':00', 1027)]


class SchedulerTimingSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture();cls.addClassCleanup(cls.mongo.close)
    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.accounts.client_factory = AcceptedSchool;AcceptedSchool.posts = []
        account = self.accounts.verify_and_save('fixture01', 'pass', '', None)
        self.account = self.accounts.save_plan(account.id, {'seat_ids': ['1'], 'day_offset': 1,
            'mode': 'day', 'begin': '09:00', 'end': '12:00', 'trigger': '07:00:00', 'enabled': True}, account.revision)
        self.run = RunSpec(account.id, self.account.revision, '2026-10-22', '09:00', '12:00', ('1',), time.time()-.01)
        self.store.ensure_run(self.run)
        self.reservations = ReservationService(self.accounts, self.store, True)
        self.scheduler = LocalScheduler(self.accounts, self.reservations, self.store)
    def tearDown(self):
        self.scheduler.stop();self.accounts.close();self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def submit(self, *, pool=None):
        return self.scheduler._queue_stage({'run': self.run}, 'submit',
            pool or self.scheduler.submit_pool, self.scheduler._submit)

    def timings(self):
        until = time.monotonic()+2
        while time.monotonic() < until:
            row = self.store.get_run(self.run.key)
            if row.get('scheduler_timings'):return row['scheduler_timings']
            time.sleep(.002)
        self.fail('Completed task diagnostics were not persisted')

    def test_diagnostic_write_failure_keeps_accepted_record_and_single_post(self):
        with patch.object(self.store, 'save_scheduler_timings', side_effect=OSError('fixture diagnostics failure')):
            with self.assertLogs('local_app.scheduler', level='WARNING'):
                self.assertEqual(self.submit().result(timeout=3).status, 'success')
                self.scheduler.stop()
        self.assertEqual(self.store.get_run(self.run.key)['status'], 'success')
        self.assertEqual(self.reservations.execute(self.run).status, 'success')
        self.assertEqual(AcceptedSchool.posts, [self.run.key])

    def test_stale_preparation_cannot_overwrite_successful_submit_diagnostics(self):
        self.assertEqual(self.submit().result(timeout=3).status, 'success')
        before = self.timings()
        duplicate = self.scheduler._queue_stage({'run': self.run}, 'prepare',
            self.scheduler.prep_pool, self.scheduler._prepare)
        self.assertFalse(duplicate.result(timeout=3))
        self.assertEqual(self.store.get_run(self.run.key)['scheduler_timings'], before)
        self.assertEqual(AcceptedSchool.posts, [self.run.key])

    def test_updated_plan_drops_old_diagnostics_and_rejects_old_writer(self):
        self.assertTrue(self.store.save_scheduler_timings(self.run, {'warm': {'elapsed_ms': 123}}))
        newer = replace(self.run, revision=self.run.revision+1)
        self.store.ensure_run(newer)
        self.assertNotIn('scheduler_timings', self.store.get_run(self.run.key))
        self.assertFalse(self.store.save_scheduler_timings(self.run, {'warm': {'elapsed_ms': 456}}))
        self.assertNotIn('scheduler_timings', self.store.get_run(self.run.key))

    def test_unsubmitted_run_refreshes_effective_window_without_overwriting_success(self):
        normalized=replace(self.run,end='11:00')
        self.store.ensure_run(normalized)
        self.assertEqual(self.store.get_run(self.run.key)['spec']['end'],'11:00')
        self.assertFalse(self.store.save_scheduler_timings(self.run,{'submit':{'elapsed_ms':10}}))
        self.store.transition_run(self.run.key,('queued',),'success',{})
        self.store.ensure_run(self.run)
        self.assertEqual(self.store.get_run(self.run.key)['spec']['end'],'11:00')

    def test_timing_store_only_keeps_known_finite_numbers(self):
        self.store.save_scheduler_timings(self.run, {'warm': {'elapsed_ms': 12.5,
            'password': 'fixture-secret', 'finished_at': float('nan')}, 'secret': {'elapsed_ms': 30}})
        self.assertEqual(self.store.get_run(self.run.key)['scheduler_timings'], {'warm': {'elapsed_ms': 12.5}})

    def test_submit_lock_wait_is_distinguished_from_executor_queue(self):
        lock = self.accounts.account_lock(self.run.account_id)
        job = {'run': self.run}
        with lock:
            future = self.scheduler._queue_stage(job, 'submit', self.scheduler.submit_pool, self.scheduler._submit)
            until = time.monotonic()+1
            while 'started_at' not in job['timings']['submit'] and time.monotonic() < until:time.sleep(.001)
            self.assertIn('started_at', job['timings']['submit'])
            time.sleep(.03)
            self.assertFalse(future.done())
        self.assertEqual(future.result(timeout=3).status, 'success')
        phase = self.timings()['submit']
        self.assertGreaterEqual(phase['account_lock_wait_ms'], 25)
        self.assertGreaterEqual(phase['elapsed_ms'], phase['account_lock_wait_ms'])

    def test_executor_queue_wait_is_measured_before_submit_starts(self):
        release = threading.Event();started = threading.Event()
        def occupy():started.set();release.wait(timeout=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocker = pool.submit(occupy);self.assertTrue(started.wait(timeout=1))
            try:
                future = self.submit(pool=pool);time.sleep(.03)
                self.assertFalse(future.done())
            finally:release.set()
            self.assertEqual(future.result(timeout=3).status, 'success');blocker.result(timeout=1)
        self.assertGreaterEqual(self.timings()['submit']['queue_ms'], 25)

    def test_slow_diagnostics_do_not_hold_the_submit_worker(self):
        started, release = threading.Event(), threading.Event()
        original = self.store.save_scheduler_timings
        def slow_write(*args):
            started.set();release.wait(timeout=2)
            return original(*args)
        with patch.object(self.store, 'save_scheduler_timings', side_effect=slow_write):
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    first = self.submit(pool=pool)
                    self.assertTrue(started.wait(timeout=1))
                    until = time.monotonic()+.2
                    while not first.done() and time.monotonic() < until:time.sleep(.001)
                    self.assertTrue(first.done(), 'Diagnostic IO is still holding a submit worker')
                    self.assertEqual(self.submit(pool=pool).result(timeout=1).status, 'success')
                    self.assertEqual(AcceptedSchool.posts, [self.run.key])
                finally:release.set()
