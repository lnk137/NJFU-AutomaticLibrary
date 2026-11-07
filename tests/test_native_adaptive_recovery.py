"""Full Runtime recovery with private Mongo/TLS/native; no production requests."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from local_app.models import AppError
from local_app.native import recovery
from support.native_runtime import NativeRuntimeCase


class NativeAdaptiveRecoveryTests(NativeRuntimeCase):
    def final_io_clock_case(self,where):
        engine=self.skewed();offset=[0];recovered=[False];injected=[False]
        engine.scheduler.wall_clock=lambda:time.time()+offset[0]
        engine.dispatch.wall_clock=lambda:time.time()+offset[0]
        clock=SimpleNamespace(time=lambda:time.time()+offset[0])
        original_recover=engine.scheduler.recover
        store=engine.dispatch.raw_accounts.store
        original_list=store.list_accounts
        original_control=engine.bridge.set_control
        def oscillate():
            injected[0]=True
            self.assertTrue(engine.dispatch.clock_fault.is_set())
            generation=engine._clock_generation;offset[0]=2
            self.until(lambda:engine._clock_generation>generation)
            generation=engine._clock_generation;offset[0]=0
            self.until(lambda:engine._clock_generation>generation)
        def recover():
            result=original_recover();recovered[0]=True;return result
        def listing():
            if where=='triggers' and recovered[0] and not injected[0]:oscillate()
            return original_list()
        def control(**values):
            result=original_control(**values)
            if where=='ack' and recovered[0] and values['live'] and not injected[0]:oscillate()
            return result
        with patch.object(recovery,'time',clock),patch.object(engine.scheduler,'recover',side_effect=recover),patch.object(store,'list_accounts',side_effect=listing),patch.object(engine.bridge,'set_control',side_effect=control):
            engine._start_clock_monitor();self.until(lambda:engine._last_pair is not None)
            with self.assertRaises(AppError):engine.recover_once()
        self.assertTrue(injected[0])
        self.assertFalse(engine.bridge.outbox.applied('global')['enabled'])
        self.assertTrue(engine.dispatch.clock_fault.is_set())
        self.assertEqual(self.fixture.posts(),[])

    def test_clock_monitor_runs_during_final_trigger_read(self):
        self.final_io_clock_case('triggers')

    def test_clock_monitor_runs_during_final_state_ack_and_revokes_again(self):
        self.final_io_clock_case('ack')

    def test_recovery_io_crossing_quiet_window_does_not_reenable(self):
        engine=self.skewed();original=engine.scheduler.recover
        with patch.object(recovery,'io_quiet',side_effect=[False,False,True]),patch.object(engine.scheduler,'recover',side_effect=original):
            with self.assertRaises(AppError):engine.recover_once()
        self.assertTrue(engine._clock_deferred)
        self.assertFalse(engine.bridge.outbox.applied('global')['enabled'])
        self.assertEqual(self.fixture.posts(),[])

    def test_blocked_school_dns_cannot_block_runtime_shutdown(self):
        engine=self.runtime.native
        engine.scheduler.anchor_wall-=3;engine.dispatch.anchor_wall-=3
        engine._stable_since-=1
        entered=threading.Event();release=threading.Event()
        def blocked():
            entered.set();release.wait(8)
            return {'ok':False,'reason':'school_unavailable'}
        try:
            with patch.object(recovery,'_school_clock_check',side_effect=blocked),patch.object(recovery,'ntp_clock_consensus',return_value={'ok':False,'source':'ntp_quorum','reason':'reference_unavailable','samples':[]}):
                self.runtime.start();self.assertTrue(entered.wait(2))
                started=time.monotonic();self.runtime.close()
                self.assertLess(time.monotonic()-started,3.5)
                self.assertFalse(engine.thread.is_alive())
        finally:
            release.set()
            recovery._school_probe.pending.result(timeout=2)
        self.assertEqual(self.fixture.posts(),[])

    def test_quiet_deferral_remains_clock_gated_on_the_next_attempt(self):
        engine=self.runtime.native;engine.scheduler.clock_ok=False
        engine.dispatch.clock_fault.clear();engine.clock_validator=lambda:True
        engine._stable_since-=1
        with patch.object(recovery,'io_quiet',side_effect=[False,False,True]):
            with self.assertRaises(AppError):engine.recover_once()
        self.assertFalse(engine.scheduler.clock_ok)
        self.assertTrue(engine.dispatch.clock_fault.is_set())
        with patch.object(recovery,'io_quiet',return_value=True),patch.object(engine,'clock_validator') as validate:
            with self.assertRaises(AppError):engine.recover_once()
            validate.assert_not_called()
        self.assertFalse(engine.bridge.outbox.applied('global')['enabled'])
        self.assertEqual(self.fixture.posts(),[])

    def until(self,predicate,timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Recovery condition not reached')

    def skewed(self):
        engine=self.runtime.native
        self.runtime.scheduler.anchor_wall-=3
        engine.dispatch.anchor_wall-=3
        engine.clock_validator=lambda:True
        engine._stable_since-=1
        return engine

    def test_clock_jump_during_ledger_recovery_requires_new_verification(self):
        engine=self.skewed();offset=[0]
        original=engine.scheduler.recover
        engine.scheduler.wall_clock=lambda:time.time()+offset[0]
        engine.dispatch.wall_clock=lambda:time.time()+offset[0]
        clock=SimpleNamespace(time=lambda:time.time()+offset[0])
        def changed():
            offset[0]+=2
            return original()
        with patch.object(recovery,'time',clock),patch.object(engine.scheduler,'recover',side_effect=changed):
            with self.assertRaises(AppError) as caught:engine.recover_once()
        self.assertEqual(caught.exception.code,'native_clock_pending')
        state=engine.bridge.outbox.applied('global')
        self.assertFalse(state['enabled'])
        self.assertFalse(engine.scheduler.status()['ready_to_submit'])
        self.assertEqual(self.fixture.posts(),[])

    def test_clock_jump_out_and_back_during_recovery_is_not_erased_by_reanchoring(self):
        engine=self.skewed();original=engine.scheduler.recover;offset=[0]
        engine.scheduler.wall_clock=lambda:time.time()+offset[0]
        engine.dispatch.wall_clock=lambda:time.time()+offset[0]
        clock=SimpleNamespace(time=lambda:time.time()+offset[0])
        def changed():
            generation=engine._clock_generation
            offset[0]=2
            self.until(lambda:engine._clock_generation>generation)
            generation=engine._clock_generation
            offset[0]=0
            self.until(lambda:engine._clock_generation>generation)
            return original()
        with patch.object(recovery,'time',clock),patch.object(engine.scheduler,'recover',side_effect=changed):
            engine._start_clock_monitor()
            self.until(lambda:engine._last_pair is not None)
            with self.assertRaises(AppError):engine.recover_once()
        self.assertFalse(engine.bridge.outbox.applied('global')['enabled'])
        self.assertEqual(self.fixture.posts(),[])

    def test_release_quiet_window_entered_during_validation_defers_before_reanchoring(self):
        engine=self.skewed()
        with patch.object(recovery,'io_quiet',side_effect=[False,True]),patch.object(engine.scheduler,'recover') as recover:
            with self.assertRaises(AppError):engine.recover_once()
            recover.assert_not_called()
        self.assertTrue(engine._clock_deferred)
        self.assertFalse(engine.bridge.outbox.applied('global')['enabled'])
        self.assertEqual(self.fixture.posts(),[])

    def test_stale_school_date_recovery_preserves_fourteen_future_plans(self):
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password') for student in self.students[1:14]]
        due=time.time()+600
        for account in accounts:self.plan(account,due=due)
        plans={a['_id']:(a.get('plan'),a['revision']) for a in self.db.campus_accounts.find()}
        engine=self.runtime.native;engine.scheduler.anchor_wall-=3;engine.dispatch.anchor_wall-=3
        engine._stable_since-=1
        # Use the private TLS transport, changing only the synthetic Date.
        from support.native_school import FixtureTransport
        session=FixtureTransport(self.fixture);get=session.get
        from email.utils import formatdate
        def stale(*args,**kwargs):
            response=get(*args,**kwargs);response.headers['Date']=formatdate(time.time()-5,usegmt=True);return response
        session.get=stale
        with patch.object(recovery.requests,'Session',return_value=session),patch.object(recovery,'ntp_clock_consensus',return_value={'ok':True,'source':'ntp_quorum','reason':'confirmed','samples':[]}):
            self.runtime.start()
            self.until(lambda:engine.last_recovery_at is not None and engine.scheduler.status()['ready_to_submit'])
        self.assertEqual(engine.last_clock_check['source'],'ntp_quorum')
        self.assertEqual(engine.scheduler.status()['capacity'],20)
        self.assertEqual(plans,{a['_id']:(a.get('plan'),a['revision']) for a in self.db.campus_accounts.find()})
        self.assertEqual(self.db.campus_runs.count_documents({}),0)
        self.assertEqual(self.fixture.posts(),[])

    def test_exhausted_burst_cools_down_then_recovers_automatically(self):
        engine=self.runtime.native;allowed=threading.Event();checks=[]
        def verify():checks.append(time.monotonic());return allowed.is_set()
        engine.clock_validator=verify;engine._stable_since-=1
        engine.recovery_budget.DELAYS=(0,0,0,0,0);engine.recovery_budget.COOLDOWN=.6
        engine.scheduler.anchor_wall-=3;engine.dispatch.anchor_wall-=3
        self.runtime.start()
        self.until(lambda:engine.recovery_budget.cooldown)
        self.assertEqual(len(checks),6)
        self.assertFalse(engine.scheduler.status()['ready_to_submit'])
        time.sleep(.15);self.assertEqual(len(checks),6)
        allowed.set()
        self.until(lambda:engine.scheduler.status()['ready_to_submit'])
        self.assertEqual(len(checks),7)
        self.assertEqual(engine.recovery_budget.attempts,0)
        self.assertIsNotNone(engine.last_clock_fault)
        self.assertEqual(self.fixture.posts(),[])
