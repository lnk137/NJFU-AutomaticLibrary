"""Actual native restart/classification, read-only recovery and clock boundaries."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from local_app.models import AppError
from local_app.native.protocol import ProtocolError
from support.native_runtime import NativeRuntimeCase


class NativeRecoveryTests(NativeRuntimeCase):
    def supported(self):
        self.assertTrue(callable(getattr(self.runtime.native,'recover_once',None)),
                        'Automatic native recovery is not implemented')
    def until(self,predicate,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Recovery condition not reached: '+str(self.runtime.scheduler.status()))

    def test_normal_sleep_advances_both_clocks_without_latching_and_requests_sync(self):
        self.supported();self.runtime.start()
        engine=self.runtime.native;wall=time.time();elapsed=engine.elapsed()
        engine.observe_clock(wall,elapsed)
        engine.observe_clock(wall+3600,elapsed+3600)
        status=self.runtime.scheduler.status()
        self.assertTrue(status['clock_ok']);self.assertTrue(status['allow_live'])
        self.assertEqual(status.get('last_wake_at'),wall+3600)
        self.assertEqual(self.fixture.posts(),[])

    def test_true_clock_step_blocks_until_trusted_and_never_catches_up(self):
        self.supported();engine=self.runtime.native;engine.clock_validator=lambda:False
        run=self.plan();self.runtime.start();self.wait_arm(run)
        self.runtime.scheduler.anchor_wall-=3;engine.dispatch.anchor_wall-=3
        self.assertEqual(self.wait(run.key,{'skipped','failed'},10)['status'],'skipped')
        self.assertFalse(self.runtime.scheduler.status()['allow_live'])
        self.assertEqual(self.fixture.posts(),[])
        # During the release quiet window the automatic public clock GET is
        # intentionally deferred. A local-root clock confirmation may recover
        # without network IO, while the already missed task remains skipped.
        def confirmed():
            try:engine.recover_once(trust_clock=True)
            except AppError as exc:
                if exc.code=='native_recovery_busy':return False
                raise
            return self.runtime.scheduler.status()['ready_to_submit']
        self.until(confirmed)
        self.assertIsNotNone(self.runtime.scheduler.status().get('last_recovery_at'))
        self.assertEqual(self.fixture.posts(),[])

    def test_trusted_clock_evidence_automatically_recovers_outside_release_window(self):
        self.supported();engine=self.runtime.native;engine.clock_validator=lambda:True
        self.runtime.start()
        self.runtime.scheduler.anchor_wall-=3;engine.dispatch.anchor_wall-=3
        engine.request_recovery()
        self.until(lambda:self.runtime.scheduler.status()['ready_to_submit'] and engine.last_recovery_at is not None)
        self.assertEqual(self.fixture.posts(),[])

    def test_native_crash_after_first_post_reconciles_without_replay(self):
        self.supported();engine=self.runtime.native
        run=self.plan();self.fixture.faults[(self.students[0],1)]={'delay_ms':600}
        self.runtime.start();self.wait_arm(run)
        old_epoch=engine.process.epoch
        self.until(lambda:bool(self.fixture.posts()))
        child=engine.process.process;child.kill();child.wait(timeout=3)
        row=self.wait(run.key,{'success','failed','skipped'},14)
        self.assertEqual(row['status'],'success',row)
        self.assertEqual(len(self.fixture.posts()),1)
        self.until(lambda:engine.process.epoch>old_epoch and self.runtime.scheduler.status()['ready_to_submit'])
        self.assertFalse(engine.bridge.outbox.failed)

    def test_native_crash_before_due_can_rebind_only_proven_unclaimed_future_work(self):
        self.supported();engine=self.runtime.native
        run=self.plan();self.runtime.start();bound=self.wait_arm(run)
        child=engine.process.process;child.kill();child.wait(timeout=3)
        row=self.wait(run.key,{'success','failed','skipped'},14)
        self.assertEqual(row['status'],'success',row)
        self.assertGreater(row['native_binding']['epoch'],bound['native_binding']['epoch'])
        self.assertEqual(len(self.fixture.posts()),1)
        self.assertTrue(all(value==1 for value in self.fixture.post_counts.values()))

    def test_operator_pause_survives_process_recovery_without_reenabling(self):
        self.supported();self.runtime.start();engine=self.runtime.native
        engine.pause();old_epoch=engine.process.epoch
        child=engine.process.process;child.kill();child.wait(timeout=3)
        self.until(lambda:engine.process.epoch>old_epoch and not engine.bridge.outbox.failed
                   and self.runtime.scheduler.status()['executor_state']=='paused')
        status=self.runtime.scheduler.status()
        self.assertFalse(status['allow_live']);self.assertEqual(status['executor_state'],'paused')
        self.assertEqual(self.fixture.posts(),[])

    def test_sync_notification_failure_cannot_kill_the_recovery_monitor(self):
        self.supported();engine=self.runtime.native
        def unavailable():raise OSError('synthetic sync notification failure')
        engine.sync_callback=unavailable
        engine._last_pair=(time.time()-3600,engine.elapsed()-3600)
        self.runtime.start();time.sleep(.3)
        self.assertTrue(engine.thread.is_alive())
        child=engine.process.process;epoch=engine.process.epoch
        child.kill();child.wait(timeout=3)
        self.until(lambda:engine.process.epoch>epoch and self.runtime.scheduler.status()['ready_to_submit'])
        self.assertEqual(self.fixture.posts(),[])

    def test_corrupt_persistent_ledger_stays_blocked_without_file_reset(self):
        self.supported();self.runtime.start();engine=self.runtime.native
        path=engine.process.state/'state.db';marker=engine.process.state/'identity'
        identity=marker.read_bytes()
        with engine.recovery_lock:
            child=engine.process.process;child.kill();child.wait(timeout=3)
            with path.open('r+b') as stream:stream.write(b'corrupt-ledger!!!');stream.flush();os.fsync(stream.fileno())
        self.until(lambda:self.runtime.scheduler.status().get('executor_state')=='blocked')
        self.assertTrue(path.read_bytes().startswith(b'corrupt-ledger!!!'))
        self.assertEqual(marker.read_bytes(),identity)
        self.assertFalse(self.runtime.scheduler.status()['allow_live'])
        self.assertEqual(self.fixture.posts(),[])

    def test_epoch_rollback_proof_is_hard_blocked_without_repeated_restarts(self):
        self.runtime.start();engine=self.runtime.native
        with engine.recovery_lock,engine.bridge.outbox.lock:
            with engine.bridge.outbox.transaction():engine.bridge.outbox._meta('epoch',str(engine.process.epoch+100))
            child=engine.process.process;child.kill();child.wait(timeout=3)
        self.until(lambda:self.runtime.scheduler.status()['executor_state']=='blocked',timeout=3)
        epoch=engine.process.epoch;time.sleep(1.3)
        self.assertEqual(engine.process.epoch,epoch)
        self.assertFalse(self.runtime.scheduler.status()['allow_live'])
        self.assertEqual(self.fixture.posts(),[])

    def test_root_recovery_and_restart_cannot_increment_away_rollback_proof(self):
        import sqlite3
        from local_app.runtime import create_runtime
        self.runtime.start();engine=self.runtime.native
        with engine.recovery_lock,engine.bridge.outbox.lock:
            with engine.bridge.outbox.transaction():engine.bridge.outbox._meta('epoch',str(engine.process.epoch+2))
            child=engine.process.process;child.kill();child.wait(timeout=3)
        self.until(lambda:self.runtime.scheduler.status()['executor_state']=='blocked',timeout=3)
        epoch=engine.process.epoch
        self.assertEqual(self.request('POST','/api/local/admin/executor',{'action':'recover'}).status_code,503)
        self.assertEqual(engine.process.epoch,epoch)
        self.runtime.close()
        path=self.directory/'native/ledger/state.db'
        def stored_epoch():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                return int(db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0])
        before=stored_epoch()
        runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=self.native_factory)
        self.addCleanup(runtime.close);runtime.start()
        self.assertEqual(runtime.scheduler.status()['executor_state'],'blocked')
        self.assertEqual(stored_epoch(),before)
        self.assertEqual(self.fixture.posts(),[])

    def test_lost_state_ack_recovers_readback_without_replaying_mongo_action(self):
        self.supported();self.runtime.start();engine=self.runtime.native
        old_epoch=engine.process.epoch
        old=self.runtime.store.get_account(self.account.id)
        actual=engine.process.state_update;injected=[]
        def lost(key,state):
            actual(key,state)
            if key==self.account.id and not injected:
                injected.append(True);raise ProtocolError('simulated ACK loss')
        with patch.object(engine.process,'state_update',lost):
            with self.assertRaises(AppError):
                self.runtime.accounts.save_plan(self.account.id,{'seat_ids':['2'],'enabled':True,
                    'begin':'09:00','end':'12:00','trigger':'07:00:00'},old['revision'])
        engine.request_recovery()
        self.until(lambda:not engine.bridge.outbox.failed and self.runtime.scheduler.status()['ready_to_submit'])
        current=self.runtime.store.get_account(self.account.id)
        self.assertEqual(current['revision'],old['revision']);self.assertEqual(current['plan'],old['plan'])
        self.assertFalse(engine.bridge.outbox.unresolved())
        self.assertGreater(engine.process.epoch,old_epoch)
        self.assertEqual(self.fixture.posts(),[])

    def test_contained_startup_failure_keeps_root_ui_but_never_python_sends(self):
        self.supported()
        from local_app.runtime import create_runtime
        self.runtime.close()
        def unavailable(directory):raise AppError('native_build_missing','Synthetic missing build')
        unavailable.isolated_fixture=True
        runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=unavailable)
        self.addCleanup(runtime.close);runtime.start()
        self.assertEqual(runtime.app.test_client().get('/health',base_url=self.origin).status_code,200)
        status=runtime.scheduler.status()
        self.assertEqual(status['executor'],'cpp');self.assertEqual(status['executor_state'],'blocked')
        self.assertFalse(status['allow_live']);self.assertFalse(status['ready_to_submit'])
        self.assertEqual(self.fixture.posts(),[])

    def test_transient_startup_readback_recovers_instead_of_permanent_unavailable(self):
        from pymongo.errors import ConnectionFailure
        from local_app.native.management import ManagementBridge
        from local_app.runtime import create_runtime
        self.runtime.close();actual=ManagementBridge.recover;calls=[]
        def transient(bridge):
            calls.append(True)
            if len(calls)==1:raise ConnectionFailure('synthetic startup readback failure')
            return actual(bridge)
        with patch.object(ManagementBridge,'recover',transient):
            runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=self.native_factory)
            self.addCleanup(runtime.close);runtime.start()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and not runtime.scheduler.status()['ready_to_submit']:time.sleep(.02)
            self.assertTrue(runtime.scheduler.status()['ready_to_submit'],runtime.scheduler.status())
            self.assertGreaterEqual(len(calls),2)
        self.assertEqual(self.fixture.posts(),[])

    def test_transient_initial_run_classification_recovers_without_stopping_ui(self):
        from local_app.native.dispatch import CampusDispatch
        from local_app.runtime import create_runtime
        self.runtime.close();actual=CampusDispatch.recover_runs;calls=[]
        def transient(dispatch):
            calls.append(True)
            if len(calls)==1:raise ProtocolError('synthetic initial readback failure')
            return actual(dispatch)
        with patch.object(CampusDispatch,'recover_runs',transient):
            runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=self.native_factory)
            self.addCleanup(runtime.close);runtime.start()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and not runtime.scheduler.status()['ready_to_submit']:time.sleep(.02)
            self.assertTrue(runtime.scheduler.status()['ready_to_submit'],runtime.scheduler.status())
            self.assertGreaterEqual(len(calls),2)
        self.assertEqual(self.fixture.posts(),[])

    def test_transient_heartbeat_clock_fault_recovers_only_after_verified_time(self):
        engine=self.runtime.native;verification_allowed=threading.Event();checks=[]
        def verify():
            checks.append(True)
            return verification_allowed.is_set()
        engine.clock_validator=verify
        self.runtime.start()
        self.until(lambda:self.runtime.scheduler.status()['ready_to_submit'])
        injected=threading.Event()
        def clock_ok():
            if threading.current_thread().name=='isolated-native-lease' and not injected.is_set():
                injected.set();return False
            return True
        with patch.object(engine.dispatch,'clock_ok',side_effect=clock_ok):
            self.assertTrue(injected.wait(3))
            self.until(lambda:(lambda state: state and not state['enabled'] and not state['plan'])(
                engine.bridge.outbox.applied('global')))
            self.until(lambda:bool(checks))
            status=self.runtime.scheduler.status()
            self.assertFalse(status['ready_to_submit'],status)
            self.assertNotEqual(status['executor_state'],'ready',status)
            verification_allowed.set()
            self.until(lambda:self.runtime.scheduler.status()['ready_to_submit'] and engine.last_recovery_at is not None)
        self.assertEqual(self.fixture.posts(),[])

    def test_transient_heartbeat_clock_recovery_does_not_undo_operator_pause(self):
        engine=self.runtime.native;engine.clock_validator=lambda:True
        self.runtime.start();engine.pause();injected=threading.Event()
        def clock_ok():
            if threading.current_thread().name=='isolated-native-lease' and not injected.is_set():
                injected.set();return False
            return True
        with patch.object(engine.dispatch,'clock_ok',side_effect=clock_ok):
            self.assertTrue(injected.wait(3))
            self.until(lambda:engine.last_recovery_at is not None and
                       self.runtime.scheduler.status()['executor_state']=='paused')
            status=self.runtime.scheduler.status()
            self.assertTrue(status['operator_paused'],status)
            self.assertFalse(status['allow_live'],status)
        self.assertEqual(self.fixture.posts(),[])
