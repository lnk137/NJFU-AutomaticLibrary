"""Cross-day wake recovery with real scheduler/storage and no school network."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.models import RunSpec, SchoolRecord, Submission, ZONE
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from support.account_fixture import School, services
from support.mongo_fixture import MongoFixture


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 10, 24, 10, tzinfo=ZONE).timestamp()
        self.elapsed = 1000.
    def advance(self, seconds):
        self.wall += seconds; self.elapsed += seconds
    def until(self, when):self.advance(when-self.wall)


class SleepRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.clock, self.posts = Clock(), []
        posts = self.posts
        class FixtureSchool(School):
            active_statuses = frozenset({1027})
            def prepare_booking(self):pass
            def prepare_submission(self, run, seat):pass
            def submit(self, run, seat):
                posts.append(run.key);self.accepted=(run,seat)
                return Submission('accepted', code=0, sent=True)
            def query(self, begin, end):
                run,seat=self.accepted
                return [SchoolRecord('fixture',self.student_id,seat,
                    run.target_date+' '+run.begin+':00',run.target_date+' '+run.end+':00',1027)]
        self.accounts.client_factory=FixtureSchool
        for i in range(9):
            a=self.accounts.verify_and_save(f'fixture{i:02}','pass','',None)
            self.accounts.save_plan(a.id,{'seat_ids':['1'],'day_offset':1,'mode':'day',
                'begin':'09:00','end':'12:00','trigger':'07:00:00','enabled':True},a.revision)
        # Patch only the OS elapsed boundary; production chooses its default clock.
        with patch('local_app.scheduler.elapsed_time',lambda:self.clock.elapsed,create=True):
            self.scheduler=LocalScheduler(self.accounts,ReservationService(self.accounts,self.store,True,
                wall_clock=lambda:self.clock.wall),self.store,wall_clock=lambda:self.clock.wall)
    def tearDown(self):
        self.scheduler.stop();self.accounts.close();self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)
    def stage_done(self, stage):
        for job in list(self.scheduler.jobs.values()):
            if job.get(stage):job[stage].result(timeout=5)

    def test_sleep_then_next_day_nine_tasks_execute_without_replaying_skipped_history(self):
        yesterday=[]
        for a in self.store.list_accounts():
            run=RunSpec(a.id,a.revision,'2026-10-25','09:00','12:00',('1',),
                        datetime(2026,10,24,7,tzinfo=ZONE).timestamp())
            self.store.ensure_run(run);self.store.transition_run(run.key,('queued',),'skipped',{'message':'fixture expired'})
            yesterday.append(deepcopy(self.store.get_run(run.key)))
        plans=[asdict(a) for a in self.store.list_accounts()]
        self.clock.advance(5400)  # Wall and sleep-inclusive elapsed both advance during suspend.
        self.scheduler.tick()
        self.assertTrue(self.scheduler.clock_ok,'Normal wake must not latch the wall-clock guard')
        due=datetime(2026,10,25,7,tzinfo=ZONE).timestamp()
        self.clock.until(due-180);self.scheduler.refresh();self.scheduler.tick();self.stage_done('prep')
        self.clock.until(due-1.5);self.scheduler.tick();self.stage_done('warm')
        self.clock.until(due-.001);self.scheduler.tick();self.assertEqual(self.posts,[])
        self.clock.until(due);self.scheduler.tick();self.stage_done('submit');self.scheduler.stop()
        self.assertEqual(len(self.posts),9);self.assertEqual(len(set(self.posts)),9)
        self.assertEqual(self.db.campus_runs.count_documents({'target_date':'2026-10-26','status':'success'}),9)
        self.assertEqual([self.store.get_run(r['_id']) for r in yesterday],yesterday)
        self.assertEqual([asdict(a) for a in self.store.list_accounts()],plans)

    def test_real_forward_or_backward_wall_steps_remain_latched(self):
        for step in (-5,5):
            with self.subTest(step=step):
                self.clock.wall+=step;self.scheduler.tick()
                self.assertFalse(self.scheduler.clock_ok)
                self.clock.wall-=step;self.scheduler.tick()
                self.assertFalse(self.scheduler.clock_ok,'A true time step still requires operator review')
                self.assertEqual(self.posts,[])

    def test_sleep_past_release_window_cannot_submit_late(self):
        due=datetime(2026,10,25,7,tzinfo=ZONE).timestamp()
        self.clock.until(due-180);self.scheduler.refresh()
        self.clock.until(due+31);self.scheduler.tick();self.scheduler.refresh()
        self.assertTrue(self.scheduler.clock_ok)
        self.assertEqual(self.posts,[])


class NativeElapsedClockTests(unittest.TestCase):
    def test_os_clock_is_monotonic_and_sleep_aware_on_this_mac(self):
        from local_app.elapsed_clock import elapsed_time, CLOCK_SOURCE
        self.assertLessEqual(elapsed_time(),elapsed_time())
        if sys.platform=='darwin':self.assertEqual(CLOCK_SOURCE,'mach_continuous_time')
