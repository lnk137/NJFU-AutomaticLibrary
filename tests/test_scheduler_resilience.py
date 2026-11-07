"""Real storage/scheduler regression tests; school I/O is entirely fictional."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, SchoolRecord, Submission, ZONE
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from support.account_fixture import School, services
from support.mongo_fixture import MongoFixture


class SchedulerResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.due = datetime(2026, 10, 25, 7, tzinfo=ZONE).timestamp()
        self.now = self.due - 180
        self.posts, self.logins, self.failures = [], [], {}
        owner = self
        class FixtureSchool(School):
            active_statuses = frozenset({1027})
            def login(self):
                owner.logins.append(self.student_id)
                failures = owner.failures.get(self.student_id, [])
                if failures:
                    code = failures.pop(0)
                    if code:raise AppError(code, 'fixture school failure', 503)
                return super().login()
            def submit(self, run, seat):
                owner.posts.append(run.account_id);self.accepted = run, seat
                return Submission('accepted', code=0, sent=True)
            def query(self, begin, end):
                run, seat = self.accepted
                return [SchoolRecord('fixture', self.student_id, seat,
                    run.target_date+' '+run.begin+':00', run.target_date+' '+run.end+':00', 1027)]
        self.accounts.client_factory = FixtureSchool
        self.saved = []
        for number in (1, 2):
            account = self.accounts.verify_and_save(f'fixture{number:02}', 'pass', '', None)
            self.saved.append(self.accounts.save_plan(account.id, {'seat_ids': ['1'], 'day_offset': 1,
                'mode': 'day', 'begin': '09:00', 'end': '12:00', 'trigger': '07:00:00', 'enabled': True}, account.revision))
        self.logins.clear()
        self.scheduler = LocalScheduler(self.accounts, ReservationService(self.accounts, self.store, True,
            wall_clock=lambda:self.now), self.store, wall_clock=lambda:self.now, monotonic=lambda:self.now)

    def tearDown(self):
        self.scheduler.stop();self.accounts.close();self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def stage(self, name):
        for job in list(self.scheduler.jobs.values()):
            if job.get(name):job[name].result(timeout=3)

    def finish(self):
        self.now = self.due - 1.5;self.scheduler.tick();self.stage('warm')
        self.now = self.due;self.scheduler.tick();self.stage('submit')

    def test_candidate_recovers_after_three_temporary_prepare_failures(self):
        self.failures['fixture01'] = ['network_error', 'school_http', 'network_error', None]
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
        for delay in (1, 3, 10):
            self.scheduler.tick();self.now += delay;self.scheduler.tick();self.stage('prep')
        self.assertEqual(self.logins.count('fixture01'), 4)
        self.assertEqual(self.db.campus_runs.find_one({'account_id': self.saved[0].id})['status'], 'preparing')
        self.assertEqual(self.posts, [])
        self.finish()
        self.assertCountEqual(self.posts, [a.id for a in self.saved])

    def test_candidate_preflight_retry_reuses_successful_login(self):
        factory = self.accounts.client_factory
        remaining = {'fixture01': 1}
        class PreflightFailureSchool(factory):
            def prepare_booking(client):
                if remaining.get(client.student_id, 0):
                    remaining[client.student_id] -= 1
                    raise AppError('network_error', 'fixture public preflight failure', 503)
        self.accounts.client_factory = PreflightFailureSchool
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
        self.scheduler.tick();self.now += 1;self.scheduler.tick();self.stage('prep')
        self.assertEqual(self.logins.count('fixture01'), 1)
        self.assertEqual(self.db.campus_runs.find_one({'account_id':self.saved[0].id})['status'], 'preparing')
        self.finish()
        self.assertCountEqual(self.posts, [a.id for a in self.saved])

    def test_temporary_plan_poll_error_recovers_without_latching_clock(self):
        original = self.scheduler.refresh
        recovered = threading.Event()
        first = True
        def interrupted_poll():
            nonlocal first
            if first:
                first = False
                raise OSError('fixture transient database failure')
            original();recovered.set()
        self.scheduler.refresh = interrupted_poll
        self.scheduler.start()
        self.assertTrue(recovered.wait(2))
        status = self.scheduler.status()
        self.assertTrue(status['clock_ok'], status)
        self.assertIsNone(status['last_error'])
        self.assertIsNone(status.get('scheduler_warning'))
        self.assertTrue(status['ready_to_submit'])
        self.assertEqual(self.posts, [])

    def test_interval_error_cannot_kill_deadline_thread(self):
        original = self.scheduler._deadline_interval
        called = threading.Event();first = True
        def interrupted_interval():
            nonlocal first
            if first:
                first = False;raise OSError('fixture clock read failure')
            called.set();return original()
        self.scheduler._deadline_interval = interrupted_interval
        self.scheduler.start()
        self.assertTrue(called.wait(1), 'Deadline thread died instead of retrying')
        self.assertTrue(self.scheduler.status()['scheduler_running'])
        self.assertTrue(self.scheduler.clock_ok)

    def test_slow_login_does_not_block_refresh_or_drop_its_existing_job(self):
        self.scheduler.refresh();slow, fast = self.saved
        entered, release = threading.Event(), threading.Event()
        factory = self.accounts.client_factory
        class SlowSchool(factory):
            def login(client):
                if client.student_id == slow.student_id:
                    entered.set();release.wait(timeout=3)
                return super().login()
        self.accounts.client_factory = SlowSchool
        self.scheduler.tick();self.assertTrue(entered.wait(1))
        try:
            # Force the fast account to need discovery while the other logs in.
            fast_key = next(key for key, job in self.scheduler.jobs.items() if job['run'].account_id == fast.id)
            self.scheduler.jobs[fast_key]['prep'].result(timeout=1)
            del self.scheduler.jobs[fast_key]
            with ThreadPoolExecutor(max_workers=1) as pool:
                refresh = pool.submit(self.scheduler.refresh)
                done = threading.Event();refresh.add_done_callback(lambda _:done.set())
                returned = done.wait(.3)
                release.set();refresh.result(timeout=2)
            self.assertTrue(returned, 'Refresh waited for the slow account lock')
            self.assertIn(fast_key, self.scheduler.jobs)
            self.assertEqual(len(self.scheduler.jobs), 2)
            self.assertFalse(self.db.campus_runs.count_documents({'status': 'skipped'}))
        finally:release.set()

    def test_cleanup_write_does_not_hold_global_deadline_lock(self):
        self.scheduler.refresh();account = self.saved[0]
        self.accounts.save_plan(account.id, account.plan | {'enabled': False}, account.revision)
        entered, release = threading.Event(), threading.Event()
        original = self.store.transition_run
        def slow_cleanup(*args, **kwargs):
            if args[2] == 'skipped':entered.set();release.wait(timeout=3)
            return original(*args, **kwargs)
        with patch.object(self.store, 'transition_run', side_effect=slow_cleanup):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.scheduler.refresh);self.assertTrue(entered.wait(1))
                acquired = self.scheduler._lock.acquire(timeout=.2)
                if acquired:self.scheduler._lock.release()
                release.set();future.result(timeout=2)
        self.assertTrue(acquired, 'Cleanup database IO blocked the deadline lock')

    def test_invalid_legacy_account_cannot_hide_valid_account(self):
        bad, good = self.saved
        self.store.accounts.update_one({'_id':bad.id}, {'$set':{'plan.trigger':'invalid'}})
        self.scheduler.refresh()
        self.assertEqual({j['run'].account_id for j in self.scheduler.jobs.values()}, {good.id})
        self.assertTrue(self.scheduler.clock_ok)

    def test_invalid_legacy_plan_does_not_keep_its_old_discovered_job(self):
        self.scheduler.refresh();bad, good = self.saved
        self.store.accounts.update_one({'_id':bad.id}, {'$set':{'plan.begin':'19:00','plan.end':'22:00'}})
        self.scheduler.refresh()
        self.assertEqual({j['run'].account_id for j in self.scheduler.jobs.values()}, {good.id})
        self.assertEqual(self.db.campus_runs.find_one({'account_id':bad.id})['status'], 'skipped')

    def test_repeated_poll_failure_warns_but_does_not_pause_healthy_jobs(self):
        self.scheduler.refresh()
        failed = threading.Event()
        def broken_poll():
            failed.set();raise OSError('fixture repeated poll failure')
        self.scheduler.refresh = broken_poll
        self.scheduler.start();self.assertTrue(failed.wait(1))
        until = time.monotonic()+1
        while not self.scheduler.status().get('scheduler_warning') and time.monotonic()<until:time.sleep(.001)
        self.assertTrue(self.scheduler.status().get('scheduler_warning'))
        self.assertTrue(self.scheduler.clock_ok)
        self.assertIsNone(self.scheduler.last_error)
        until = time.monotonic()+2
        while not all(j.get('prep') and j['prep'].done() for j in self.scheduler.jobs.values()) and time.monotonic()<until:
            time.sleep(.001)
        self.stage('prep')
        self.finish()
        self.assertCountEqual(self.posts, [a.id for a in self.saved])

    def test_failed_prepare_future_does_not_block_other_accounts(self):
        self.scheduler.refresh();bad, good = self.saved
        for job in self.scheduler.jobs.values():
            future = Future()
            if job['run'].account_id == bad.id:future.set_exception(OSError('fixture worker failure'))
            else:future.set_result(True)
            ready = Future();ready.set_result(True)
            job.update(prep=future, warm=ready)
        self.now = self.due;self.scheduler.tick();self.stage('submit')
        self.assertEqual(self.posts, [good.id])
        self.assertTrue(self.scheduler.clock_ok)

    def test_transient_prelogin_retries_then_executes_once_per_account(self):
        self.failures['fixture01'] = ['network_error', None]
        projection = {'plan':1,'revision':1,'credential_revision':1,'secret':1}
        before = deepcopy(list(self.store.accounts.find({}, projection).sort('_id',1)))
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
        self.assertEqual(self.db.campus_runs.count_documents({'status':'failed'}), 0)
        self.scheduler.tick();self.now += 1;self.scheduler.tick();self.stage('prep')
        self.finish()
        self.assertCountEqual(self.posts, [a.id for a in self.saved])
        self.assertEqual(self.logins.count('fixture01'), 2)
        self.assertEqual(list(self.store.accounts.find({}, projection).sort('_id',1)), before)

    def test_prelogin_retry_budget_is_finite_and_failure_is_not_revived(self):
        self.failures['fixture01'] = ['school_http'] * 30
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
        key = next(k for k, job in self.scheduler.jobs.items() if job['run'].account_id == self.saved[0].id)
        for _ in range(11):
            self.scheduler.tick()
            job = self.scheduler.jobs.get(key)
            if not job:break
            self.now = job['prepare_retry_at']
            self.scheduler.tick();self.stage('prep')
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
        self.assertEqual(self.logins.count('fixture01'), 11)
        self.assertGreaterEqual(self.now, self.due - 5)
        failed = self.db.campus_runs.find_one({'account_id':self.saved[0].id})
        self.assertEqual(failed['status'], 'failed')
        self.finish();self.assertEqual(self.posts, [self.saved[1].id])

    def test_password_captcha_and_identity_errors_are_not_retried(self):
        for code in ('login_failed', 'verification_required', 'identity_mismatch', 'login_uncertain', 'school_protocol'):
            with self.subTest(code=code):
                account = self.saved[0]
                self.failures['fixture01'] = [code]
                self.db.campus_runs.delete_many({'account_id':account.id})
                self.scheduler.jobs.clear();self.scheduler.refresh();self.scheduler.tick();self.stage('prep')
                count = self.logins.count('fixture01')
                self.scheduler.tick();self.now += 5;self.scheduler.tick();self.stage('prep')
                self.assertEqual(self.logins.count('fixture01'), count)
                self.assertEqual(self.db.campus_runs.find_one({'account_id':account.id})['status'], 'failed')
        self.assertEqual(self.posts, [])

    def test_plan_change_during_retry_stops_old_task(self):
        self.failures['fixture01'] = ['network_error', None]
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep');self.scheduler.tick()
        account = self.saved[0]
        self.accounts.save_plan(account.id, account.plan | {'enabled':False}, account.revision)
        self.scheduler.refresh();self.now += 5;self.scheduler.tick();self.stage('prep')
        self.finish();self.assertEqual(self.posts, [self.saved[1].id])
        self.assertEqual(self.logins.count('fixture01'), 1)

    def test_expired_retry_never_posts_or_revives_old_failed_history(self):
        self.failures['fixture01'] = ['network_error', None]
        self.scheduler.refresh();self.scheduler.tick();self.stage('prep');self.scheduler.tick()
        self.now = self.due + 31;self.scheduler.tick();self.stage('prep');self.scheduler.refresh()
        self.assertEqual(self.logins.count('fixture01'), 1)
        self.assertEqual(self.posts, [])
