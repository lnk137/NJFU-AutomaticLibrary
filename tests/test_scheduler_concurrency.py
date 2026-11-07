"""Application scheduling fans out to 20 accounts; the 21st waits."""
from datetime import datetime
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import SchoolRecord, Submission, ZONE
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from support.account_fixture import School, services
from support.mongo_fixture import MongoFixture


class SchedulerConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.addClassCleanup(cls.mongo.close)

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.due = datetime(2026, 10, 25, 7, tzinfo=ZONE).timestamp()
        self.now = self.due - 60
        self.release, self.saturated, self.overflow = threading.Event(), threading.Event(), threading.Event()
        self.guard = threading.Lock()
        self.block_stage, self.entered, self.active, self.peak = None, [], 0, 0
        owner = self

        class FixtureSchool(School):
            active_statuses = frozenset({1027})

            def prepare_submission(client, run, seat):
                if owner.block_stage == 'warm':owner.hold(client.student_id)

            def submit(client, run, seat):
                assert client.allow_live and client.submission_guard()
                if owner.block_stage == 'submit':owner.hold(client.student_id)
                client.accepted = run, seat
                return Submission('accepted', code=0, sent=True)

            def query(client, begin, end):
                run, seat = client.accepted
                return [SchoolRecord('fixture', client.student_id, seat,
                    run.target_date+' '+run.begin+':00', run.target_date+' '+run.end+':00', 1027)]

        self.accounts.client_factory = FixtureSchool
        for i in range(21):
            account = self.accounts.verify_and_save(f'fixture{i:02}', 'fixture-password', '', None)
            self.accounts.save_plan(account.id, {'mode':'day','seat_ids':['1'],'day_offset':1,
                'begin':'09:00','end':'12:00','trigger':'07:00:00','enabled':True}, account.revision)
        self.scheduler = LocalScheduler(self.accounts,
            ReservationService(self.accounts, self.store, True, wall_clock=lambda:self.now),
            self.store, wall_clock=lambda:self.now, monotonic=lambda:self.now)
        self.scheduler.refresh()
        self.scheduler.tick()
        self.finish_stage('prep')

    def tearDown(self):
        self.release.set()
        self.scheduler.stop()
        self.accounts.close()
        self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def hold(self, student):
        with self.guard:
            self.entered.append(student)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if len(self.entered) == 20:self.saturated.set()
            if len(self.entered) == 21:self.overflow.set()
        try:
            if not self.release.wait(10):raise TimeoutError('Fixture barrier was not released')
        finally:
            with self.guard:self.active -= 1

    def finish_stage(self, name):
        for job in list(self.scheduler.jobs.values()):
            if job.get(name):job[name].result(timeout=5)

    def check_fanout(self, stage):
        self.assertTrue(self.saturated.wait(3), f'Only {len(self.entered)} accounts entered {stage} before earlier accounts completed')
        self.assertFalse(self.overflow.wait(.03), 'The 21st account exceeded the configured ceiling')
        with self.guard:
            self.assertEqual(self.active, 20)
            self.assertEqual(self.peak, 20)
        self.assertEqual(self.scheduler.status()['capacity'], 20)
        self.release.set()
        self.finish_stage(stage)
        self.assertEqual(len(self.entered), 21)
        self.assertEqual(len(set(self.entered)), 21)
        self.assertEqual(self.peak, 20)

    def test_20_warmups_enter_before_any_finishes_and_21st_waits(self):
        self.block_stage = 'warm'
        self.now = self.due - 1.5
        self.scheduler.tick()
        self.check_fanout('warm')
        self.assertEqual(self.db.campus_runs.count_documents({'status':'submitting'}), 0)
        self.assertEqual(self.db.campus_runs.count_documents({'status':'success'}), 0)

    def test_20_submissions_enter_before_any_finishes_and_21st_waits(self):
        self.now = self.due - 1.5
        self.scheduler.tick()
        self.finish_stage('warm')
        self.block_stage = 'submit'
        self.now = self.due
        self.scheduler.tick()
        self.check_fanout('submit')
        self.assertEqual(self.db.campus_runs.count_documents({'status':'success'}), 21)
