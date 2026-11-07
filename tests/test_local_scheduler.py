import sys
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        from support.account_fixture import services
        from local_app.reservations import ReservationService
        self.db=self.mongo.database();self.temp,self.store,self.accounts,_=services(self.db)
        self.reservations=ReservationService(self.accounts,self.store,True)
        self.sched=None
    def tearDown(self):
        if self.sched:self.sched.stop()
        self.accounts.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)

    def seed(self,n,due):
        from local_app.models import ZONE
        from support.account_fixture import School
        calls=[];logins=[]
        class TimedSchool(School):
            def login(self):logins.append(time.time());return super().login()
        self.accounts.client_factory=TimedSchool
        for i in range(n):
            a=self.accounts.verify_and_save(f'fixture{i:02}','pass',str(i),None)
            self.accounts.save_plan(a.id,dict(seat_ids=['1'],day_offset=1,mode='day',begin='09:00',end='12:00',trigger=datetime.fromtimestamp(due,ZONE).strftime('%H:%M:%S'),enabled=True),a.revision)
        logins.clear()
        def execute(run):
            calls.append(time.time())
            from local_app.models import RunResult
            self.store.transition_run(run.key,('queued','preparing'),'success',{})
            return RunResult('success','fixture',[])
        self.reservations.execute=execute
        return calls,logins

    def test_prelogin_and_deadline_not_blocked_by_plan_poll(self):
        from local_app.scheduler import LocalScheduler
        due=int(time.time())+2
        calls,logins=self.seed(8,due)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.sched.refresh();self.sched.start()
        limit=time.monotonic()+1.5
        while len(logins)<8 and time.monotonic()<limit:time.sleep(.01)
        self.assertEqual(len(logins),8);self.assertTrue(all(t<due for t in logins))
        self.sched.refresh=lambda:time.sleep(.8)
        until=time.monotonic()+4
        while len(calls)<8 and time.monotonic()<until:time.sleep(.005)
        self.assertEqual(len(calls),8)
        self.assertTrue(all(due<=t<due+.3 for t in calls),[round((t-due)*1000) for t in calls])
        print('simulated_deadline_ms:',{'accounts':8,'min':round((min(calls)-due)*1000,2),
            'max':round((max(calls)-due)*1000,2),'school_network':False})

    def test_closed_gate_and_clock_jump(self):
        from local_app.scheduler import LocalScheduler
        self.seed(1,int(time.time())+1)
        self.reservations.allow_live=False
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.sched.refresh();self.sched.tick()
        self.assertEqual(self.db.campus_runs.count_documents({}),0)
        self.sched.wall_clock=lambda:time.time()+5;self.sched.tick()
        self.assertFalse(self.sched.status()['clock_ok'])

    def test_booking_preflight_failure_stops_the_prepared_run(self):
        from local_app.scheduler import LocalScheduler
        from local_app.models import AppError,resolve_run,validate_plan,ZONE
        from support.account_fixture import School
        self.seed(1,int(time.time())+10)
        class CaptchaSchool(School):
            def prepare_booking(self):
                raise AppError('verification_required','预约要求人工验证码',401)
        self.accounts.client_factory=CaptchaSchool
        account=self.store.list_accounts()[0]
        plan=validate_plan(account.plan,self.accounts.catalog)
        run=resolve_run(account.id,account.revision,plan,datetime.now(ZONE).date())
        self.store.ensure_run(run)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.assertFalse(self.sched._prepare(run))
        self.assertEqual(self.store.get_run(run.key)['status'],'failed')
        self.assertIn('人工验证码',self.store.get_run(run.key)['message'])

    def test_late_and_disabled_plan_do_not_submit(self):
        from local_app.scheduler import LocalScheduler
        calls,_=self.seed(1,int(time.time())-31)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.sched.refresh();self.sched.tick()
        self.assertEqual(calls,[])

    def test_read_only_warmup_failure_keeps_the_regular_submit_path_available(self):
        from local_app.scheduler import LocalScheduler
        from local_app.models import AppError,resolve_run,validate_plan,ZONE
        from support.account_fixture import School
        self.seed(1,int(time.time())+10)
        class FailedWarmup(School):
            def prepare_submission(self, run, seat):
                raise AppError('network_error','fixture warmup failed',503)
        self.accounts.client_factory=FailedWarmup
        account=self.store.list_accounts()[0]
        run=resolve_run(account.id,account.revision,validate_plan(account.plan,self.accounts.catalog),datetime.now(ZONE).date())
        self.store.ensure_run(run)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.assertTrue(self.sched._prepare(run))
        self.assertTrue(self.sched._warm(run))
        self.assertEqual(self.store.get_run(run.key)['status'],'preparing')

    def test_plan_disabled_after_login_cannot_prepare_a_payload(self):
        from local_app.scheduler import LocalScheduler
        from local_app.models import resolve_run,validate_plan,ZONE
        from support.account_fixture import School
        self.seed(1,int(time.time())+10);calls=[]
        class WarmSchool(School):
            def prepare_submission(self, run, seat):calls.append(seat)
        self.accounts.client_factory=WarmSchool
        account=self.store.list_accounts()[0]
        run=resolve_run(account.id,account.revision,validate_plan(account.plan,self.accounts.catalog),datetime.now(ZONE).date())
        self.store.ensure_run(run)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.assertTrue(self.sched._prepare(run))
        self.accounts.save_plan(account.id,account.plan|{'enabled':False},account.revision)
        self.assertFalse(self.sched._warm(run))
        self.assertEqual(calls,[])

    def test_changed_plan_replaces_unsubmitted_revision(self):
        from local_app.models import RunSpec
        from dataclasses import replace
        self.seed(1,int(time.time())+10);account=self.store.list_accounts()[0]
        r=RunSpec(account.id,account.revision,'2026-10-20','09:00','12:00',('1',),time.time()+10)
        self.store.ensure_run(r)
        updated=replace(r,revision=r.revision+1,seat_ids=('2',))
        self.store.ensure_run(updated)
        self.assertEqual(self.store.get_run(r.key)['spec']['revision'],updated.revision)

    def test_midnight_refresh_preserves_previous_release_window(self):
        from local_app.scheduler import LocalScheduler
        from local_app.models import RunSpec,ZONE
        from concurrent.futures import Future
        due=datetime(2026,10,19,23,59,50,tzinfo=ZONE).timestamp()
        self.seed(1,due);a=self.store.list_accounts()[0]
        run=RunSpec(a.id,a.revision,'2026-10-20','09:00','12:00',('1',),due)
        self.store.ensure_run(run)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:due+15)
        ready=Future();ready.set_result(True)
        self.sched.jobs[(run.key,run.revision)]={'run':run,'prep':ready,'submit':None}
        self.sched.refresh()
        self.assertIn((run.key,run.revision),self.sched.jobs)
        self.assertEqual(self.store.get_run(run.key)['status'],'queued')

    def test_16_accounts_are_not_dropped_by_capacity(self):
        from local_app.scheduler import LocalScheduler
        due=int(time.time())+2;calls,_=self.seed(16,due)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store)
        self.sched.start();until=time.monotonic()+5
        while len(calls)<16 and time.monotonic()<until:time.sleep(.01)
        self.assertEqual(len(calls),16)

    def observed_deadline_wait(self, *, near_release):
        from concurrent.futures import Future
        from local_app.scheduler import LocalScheduler
        from local_app.models import ZONE
        due = datetime(2026,10,22,7,tzinfo=ZONE).timestamp()
        now = due-.005 if near_release else due-120
        calls,_ = self.seed(1,due)
        self.sched=LocalScheduler(self.accounts,self.reservations,self.store,
                                  wall_clock=lambda:now,monotonic=lambda:now)
        self.sched.refresh()
        ready=Future();ready.set_result(True)
        for job in self.sched.jobs.values():job.update(prep=ready,warm=ready)
        observed=[];event=threading.Event()
        class ObserveStop:
            def set(self):event.set()
            def is_set(self):return event.is_set()
            def wait(self,seconds):
                if threading.current_thread().name=='njfu-deadline':
                    observed.append(seconds);event.set();return True
                return event.wait(seconds)
        self.sched.stopping=ObserveStop()
        self.sched.start();self.assertTrue(event.wait(1))
        self.sched.stop()
        self.assertEqual(calls,[], 'Observing a near-release wake must not submit early')
        return observed

    def test_near_release_wait_cannot_sleep_past_the_deadline(self):
        delays=self.observed_deadline_wait(near_release=True)
        self.assertTrue(delays and 0<delays[0]<=.0015,delays)

    def test_fine_deadline_polling_is_not_used_minutes_before_release(self):
        delays=self.observed_deadline_wait(near_release=False)
        self.assertTrue(delays and delays[0]>=.01,delays)
