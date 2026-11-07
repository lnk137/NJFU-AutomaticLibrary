"""Private authorities and fake RPC: lifecycle, owner loss, stable signed binding."""
from pathlib import Path
import sys, copy, tempfile, unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
BACKEND=Path(__file__).resolve().parents[1]/'AL-BackEnd'
sys.path.insert(0,str(BACKEND))
import local_app
assert Path(local_app.__file__).resolve().is_relative_to(BACKEND)
from local_app.dual_node.coordinator import Coordinator
from local_app.dual_node.publisher import plan_tasks,same_task_content
from local_app.dual_node.result_bridge import ResultBridge,dual_power_row,run_spec
from local_app.models import ZONE,Seat
from local_app.power import PowerService
from local_app.store import LocalStore
from support.mongo_fixture import MongoFixture
import test_coordinator_ledger as fixtures
from test_coordinator_ledger import capability,prepared,report,task
import test_dual_control as control_fixtures
from test_result_bridge import status as completed_status

class PublisherLifecycleRegressions(unittest.TestCase):
    def setUp(self):
        self.h=control_fixtures.PublisherControlTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.now=fixtures.NOW;self.clock=[self.h.now]
        self.old=plan_tasks([self.row()],self.h.reg,self.h.runtime.accounts.catalog,now=self.h.now,days=2)[0]
        self.authority=Coordinator.initialize(self.h.root/'authority',clock=lambda:self.clock[0])
        self.addCleanup(self.authority.close)
        self.authority.enroll(self.old['account_ref'],active=True);self.authority.publish(self.old)
        def call(op,**body):
            self.h.operations.append((op,body))
            if op=='tasks':return self.authority.tasks(include_unmirrored=True)
            if op=='publish':return self.authority.publish(body['task'])
            if op=='cancel':return self.authority.cancel(body['task'],reason=body['reason'])
            raise AssertionError(op)
        self.h.client.call.side_effect=call
    def row(self):
        a=self.h.account
        return dict(_id=a.id,revision=a.revision,credential_revision=a.credential_revision,
                    plan=a.plan,date_exceptions=a.date_exceptions)
    def advance(self,now):self.h.now=self.clock[0]=now
    def issue(self):
        for node in ('mac','ubuntu'):
            self.authority.heartbeat(node,boot_id=node+'-boot-01',seq=1)
            self.authority.report(node,self.old,report(node,self.clock[0],100 if node=='mac' else 220),capability(node,self.clock[0]))
        self.authority.select(self.old);self.advance(self.old['release_at']-20)
        self.authority.heartbeat('mac',boot_id='mac-boot-01',seq=2)
        self.authority.ready('mac',self.old,prepared('mac',self.clock[0],self.old))
        return self.authority.authorize('mac',self.old)['grant']
    def cancellations(self):return [body['task'] for op,body in self.h.operations if op=='cancel']
    def test_crossing_release_and_midnight_preserves_inflight_grant(self):
        grant=self.issue();before=copy.deepcopy(self.row())
        for delta in (-1,0,1,30,31,86401):
            self.advance(self.old['release_at']+delta);self.h.sync()
            value=self.authority.inspect_status(self.old)
            self.assertEqual(value['phase'],'authorized');self.assertFalse(value['cancel_requested'])
            self.assertEqual(value['grant'],grant);self.assertNotIn(self.old,self.cancellations())
        self.assertEqual(before,self.row())
    def test_explicit_postrelease_pause_still_cancels(self):
        self.issue();self.h.account.plan['enabled']=False
        self.advance(self.old['release_at']+1);self.h.sync()
        self.assertIn(self.old,self.cancellations());self.assertTrue(self.authority.inspect_status(self.old)['cancel_requested'])
    def test_removed_registration_still_cancels_postrelease(self):
        self.issue();self.h.config['registrations']={};self.h.write_config()
        self.advance(self.old['release_at']+1);self.h.sync();self.assertIn(self.old,self.cancellations())
    def test_far_date_edit_keeps_grant_and_bridge_original_binding(self):
        grant=self.issue();self.h.account.revision+=1
        far=(datetime.fromisoformat(self.old['day'])+timedelta(days=2)).date().isoformat()
        self.h.account.date_exceptions={far:{'action':'skip'}}
        self.advance(self.old['release_at']-10);self.h.sync()
        current=self.authority.inspect_status(self.old)
        self.assertEqual(current['grant'],grant);self.assertFalse(current['cancel_requested'])
        self.assertNotIn(self.old,self.cancellations());self.assertEqual(self.h.loop.state['status'],'synchronized')
        self.assertTrue(any(call.args[0]==self.old for call in self.h.bridge.record_scope.call_args_list))
        self.assertTrue(all(body['task']['revision']==1 for op,body in self.h.operations if op=='publish' and body['task']['day']==self.old['day']))
    def test_equivalence_keeps_credential_and_effective_schedule_guards(self):
        newer=self.old|{'revision':self.old['revision']+1}
        self.assertTrue(same_task_content(self.old,newer));self.assertFalse(same_task_content(newer,self.old))
        for changes in ({'credential_revision':2},{'start_minute':600},{'end_minute':780},{'release_at':self.old['release_at']+1},{'seat_priorities':['2']}):
            with self.subTest(changes=changes):self.assertFalse(same_task_content(self.old,newer|changes))
    def test_actual_current_date_change_revokes_without_reissuing(self):
        grant=self.issue();self.h.account.revision+=1
        self.h.account.date_exceptions={self.old['day']:{'action':'override','begin':'14:00','end':'16:00','seat_ids':['1']}}
        self.advance(self.old['release_at']-10);self.h.sync();current=self.authority.inspect_status(self.old)
        self.assertTrue(current['cancel_requested']);self.assertEqual(current['grant'],grant)
        self.assertEqual(self.h.loop.state['status'],'blocked')

class OwnerLossRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=[fixtures.NOW];self.helper=fixtures.LedgerTests()
        self.authority=self.helper.initial(Path(self.tmp.name).resolve()/'authority',self.clock)
        self.addCleanup(self.authority.close);self.helper.reports(self.authority);self.authority.select(task())
    def heartbeat(self,node,seq=2,boot=None):self.authority.heartbeat(node,boot_id=boot or node+'-boot-01',seq=seq)
    def test_owner_disconnection_hands_off_once_and_rejects_late_owner(self):
        self.clock[0]+=46;self.heartbeat('ubuntu');moved=self.authority.node_status('ubuntu',task())
        self.assertEqual((moved['owner'],moved['phase']),('ubuntu','selected'));self.assertIsNone(moved['grant'])
        self.clock[0]=task()['release_at']-5;self.heartbeat('ubuntu',3);self.heartbeat('mac')
        with self.assertRaises(ValueError):self.authority.ready('mac',task(),prepared('mac',self.clock[0]))
        self.authority.ready('ubuntu',task(),prepared('ubuntu',self.clock[0],seq=3))
        grant=self.authority.authorize('ubuntu',task())['grant']
        with self.assertRaises(ValueError):self.authority.authorize('mac',task())
        self.assertEqual(self.authority.status(task())['grant'],grant)
    def test_owner_restart_revokes_old_boot(self):
        self.clock[0]+=1;self.heartbeat('mac',1,'mac-new-boot')
        self.assertEqual(self.authority.status(task())['owner'],'ubuntu')
        with self.assertRaises(ValueError):self.authority.authorize('mac',task())
    def test_ready_owner_loss_requires_new_preparation(self):
        self.clock[0]=task()['release_at']-80;self.heartbeat('mac');self.heartbeat('ubuntu')
        self.authority.ready('mac',task(),prepared('mac',self.clock[0]))
        self.clock[0]+=46;self.heartbeat('ubuntu',3)
        self.assertEqual(self.authority.status(task())['phase'],'selected')
        with self.assertRaises(ValueError):self.authority.authorize('ubuntu',task())
    def test_issued_grant_never_fails_over_after_owner_disconnection(self):
        self.helper.ready(self.authority,'mac',self.clock);grant=self.authority.authorize('mac',task())['grant']
        self.clock[0]+=46;self.heartbeat('ubuntu');result=self.authority.status(task())
        self.assertEqual(result['owner'],'mac');self.assertEqual(result['grant'],grant)
        with self.assertRaises(ValueError):self.authority.authorize('ubuntu',task())
    def test_second_loss_does_not_cycle_back(self):
        self.clock[0]+=46;self.heartbeat('ubuntu');self.authority.status(task())
        self.clock[0]+=46;self.heartbeat('mac');result=self.authority.status(task())
        self.assertEqual(result['phase'],'expired_unissued');self.assertIsNone(result['grant'])
        with self.assertRaises(ValueError):self.authority.authorize('mac',task())
    def test_missing_standby_is_fail_closed(self):
        self.clock[0]+=46;result=self.authority.status(task())
        self.assertEqual(result['phase'],'expired_unissued');self.assertIsNone(result['grant'])
    def test_missed_deadline_is_fail_closed(self):
        self.clock[0]=task()['release_at']-2;self.heartbeat('ubuntu');result=self.authority.status(task())
        self.assertEqual(result['phase'],'expired_unissued');self.assertIsNone(result['grant'])

class PreservedBindingProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def test_old_revision_stays_source_bound_after_unrelated_edit(self):
        db=self.mongo.database();self.addCleanup(self.mongo.client.drop_database,db.name)
        store=LocalStore(db);value=task()|{'day':'2026-11-08'}
        plan=dict(seat_ids=['3422','3423'],day_offset=1,mode='day',begin='08:00',end='22:00',trigger='07:00:00',enabled=True)
        store.accounts.insert_one(dict(_id='local-one',student_id='fixture-student',alias='Fixture',revision=2,credential_revision=1,verified_at='fixture',plan=plan,date_exceptions={'2026-11-10':{'action':'skip'}}))
        gate=Mock();gate.is_managed.return_value=True;store.managed_gate=gate
        bridge=ResultBridge(store,{'local-one':dict(account_ref=value['account_ref'],credential_revision=1)},gate)
        response=completed_status(value);response['mode']='production';response['grant']['domain']='njfu-dual-node-production-v1'
        bridge.apply(value,response);mirrored=store.get_run(run_spec(value,'local-one').key)
        current=replace(run_spec(value,'local-one'),revision=2,operation_id=None,expires_at=None)
        self.assertEqual(dual_power_row([mirrored],current,credential_revision=1),mirrored)
        for run,credential in ((replace(current,begin='09:00'),1),(replace(current,seat_ids=('3423',)),1),(current,2)):
            self.assertIsNone(dual_power_row([mirrored],run,credential_revision=credential))
        with tempfile.TemporaryDirectory() as tmp:
            scheduler=Mock();scheduler.status.return_value={}
            service=PowerService(SimpleNamespace(data_dir=Path(tmp)),SimpleNamespace(busy={}),store,scheduler,{seat:Seat(seat,seat,'fixture') for seat in value['seat_priorities']})
            try:
                snapshot=service.snapshot(value['release_at']+360,100.)
                self.assertIsNone(snapshot['error']);self.assertEqual(snapshot['expected'],['success'])
                self.assertEqual(store.get_run(mirrored['_id']),mirrored)
            finally:service.close()

if __name__=='__main__':unittest.main()
