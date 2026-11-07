from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Durable operator control with local authorities and fake publisher transports."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from local_app.dual_node.coordinator import Coordinator
from local_app.dual_node.fence import ManagedAccountGate,NativeFenceAck
from local_app.dual_node.publisher import PlanPublisherLoop,plan_tasks
from local_app.dual_node.rpc import dispatch
from local_app.models import AppError,Seat,ZONE
import test_coordinator_ledger as fixtures
from test_coordinator_ledger import NOW,prepared,task


class AuthorityControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='njfu-dual-control-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'authority';self.clock=[NOW]
        self.helper=fixtures.LedgerTests()
        self.authority=self.helper.initial(self.root,self.clock);self.addCleanup(lambda:self.authority.close())

    def send(self,op,body,*,role='publisher',node=None,request_id=None):
        return dispatch(self.authority,{'schema':1,'request_id':request_id or uuid.uuid4().hex,
            'op':op,'body':body},role=role,node_id=node)

    def issue(self):
        self.helper.reports(self.authority);self.authority.select(task())
        self.helper.ready(self.authority,'mac',self.clock)
        request_id=uuid.uuid4().hex
        issued=self.send('authorize',{'task':task()},role='node',node='mac',request_id=request_id)
        self.assertTrue(issued['ok'],issued)
        return request_id,issued['result']['grant']

    def test_fixture_default_unpaused_and_pause_survives_reopen(self):
        self.assertFalse(self.authority.tasks()['operator_paused'])
        paused=self.authority.control(action='pause')
        self.assertTrue(paused['operator_paused']);self.assertEqual(paused['pending_owners'],0)
        self.authority.close();self.clock[0]+=1
        self.authority=Coordinator(self.root,clock=lambda:self.clock[0])
        self.assertTrue(self.authority.tasks()['operator_paused'])
        self.assertFalse(self.authority.control(action='resume')['operator_paused'])

    def test_pause_keeps_issued_owner_cancels_and_blocks_cached_authorization(self):
        request_id,grant=self.issue()
        result=self.authority.control(action='pause')
        self.assertEqual(result,{'operator_paused':True,'pending_owners':1})
        status=self.authority.inspect_status(task())
        self.assertEqual(status['owner'],'mac');self.assertEqual(status['grant'],grant)
        self.assertTrue(status['cancel_requested']);self.assertEqual(status['phase'],'cancel_requested')
        replay=self.send('authorize',{'task':task()},role='node',node='mac',request_id=request_id)
        self.assertFalse(replay['ok']);self.assertEqual(replay['code'],'operator_paused')
        self.assertFalse(self.authority.control(action='resume')['operator_paused'])
        self.assertRaises(ValueError,self.authority.authorize,'ubuntu',task())
        self.assertRaises(ValueError,self.authority.authorize,'mac',task())
        self.authority.result('mac',task(),grant_id=grant['payload']['grant_id'],status='not_sent')
        self.assertEqual(self.authority.control(action='pause')['pending_owners'],0)
        self.assertEqual(self.authority.inspect_status(task())['owner'],'mac')

    def test_paused_server_rejects_ready_and_node_cannot_control(self):
        self.helper.reports(self.authority);self.authority.select(task())
        self.clock[0]=task()['release_at']-5
        self.authority.heartbeat('mac',boot_id='mac-boot-01',seq=2)
        self.authority.control(action='pause')
        with self.assertRaises(ValueError):self.authority.ready('mac',task(),prepared('mac',self.clock[0]))
        forbidden=self.send('control',{'action':'resume'},role='node',node='mac')
        self.assertFalse(forbidden['ok']);self.assertEqual(forbidden['code'],'operation_forbidden')
        allowed=self.send('control',{'action':'resume'})
        self.assertTrue(allowed['ok']);self.assertFalse(allowed['result']['operator_paused'])

    def test_production_starts_paused_and_requires_current_clock_health_to_resume(self):
        root=Path(self.temp.name).resolve()/'production';key=Path(self.temp.name).resolve()/'fixture-control-signing'
        subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],
                       check=True,capture_output=True,timeout=10)
        health=Mock(return_value={'synchronized':False,'checked_at':time.time(),'uncertainty_ms':1})
        with Coordinator.initialize(root,signing_key=key,mode='production',time_health=health) as authority:
            self.assertTrue(authority.tasks()['operator_paused'])
            with self.assertRaises(ValueError):authority.control(action='resume')
            self.assertTrue(authority.tasks()['operator_paused'])
            health.side_effect=lambda:{'synchronized':True,'checked_at':time.time(),'uncertainty_ms':1}
            self.assertFalse(authority.control(action='resume')['operator_paused'])
        with Coordinator(root,mode='production',time_health=health) as resumed:
            self.assertFalse(resumed.tasks()['operator_paused'])


class PublisherControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='njfu-publisher-control-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.path=self.root/'publisher.json'
        self.reg={'local-one':{'account_ref':'fixture-one','credential_revision':1}}
        self.config={'schema':1,'enabled':True,'ssh_config':'/private/fixture-ssh','ssh_host':'fixture',
                     'registrations':self.reg,'days':2}
        self.write_config()
        self.now=datetime(2026,11,6,6,50,tzinfo=ZONE).timestamp()
        self.plan={'seat_ids':['1'],'day_offset':1,'mode':'day','begin':'09:00','end':'12:00',
                   'trigger':'07:00:00','enabled':True}
        self.account=SimpleNamespace(id='local-one',student_id='Fixture01',revision=1,credential_revision=1,
                                     plan=self.plan,date_exceptions={})
        gate=Mock();gate.is_managed.return_value=True;gate.has_managed_scope.return_value=True
        self.runtime=SimpleNamespace(settings=SimpleNamespace(allow_live=True),
            native=SimpleNamespace(operator_paused=False),store=Mock(),
            accounts=SimpleNamespace(catalog={'1':Seat('1','A1','2F')}),auth=Mock(),managed_execution=gate)
        self.runtime.auth.is_allowed.return_value=True
        self.runtime.store.list_accounts.return_value=[self.account]
        self.loop=PlanPublisherLoop(self.runtime,self.path)
        self.server_paused=False;self.pending=0;self.known=[];self.operations=[]
        self.client=Mock();self.client.call.side_effect=self.call
        self.loop.client=self.client;self.loop.client_identity=('/private/fixture-ssh','fixture')
        self.bridge=Mock();self.bridge.local_account.return_value='local-one'
        self.bridge.sync.return_value={'blocked':0,'mirrored':0,'acknowledged':0}
        self.loop.result_bridge=self.bridge

    def write_config(self):
        self.path.write_text(json.dumps(self.config));self.path.chmod(0o600)

    def call(self,op,**body):
        self.operations.append((op,body))
        if op=='control':
            self.server_paused=body['action']=='pause'
            return {'operator_paused':self.server_paused,'pending_owners':self.pending}
        if op=='tasks':return {'tasks':self.known,'next_cursor':None}
        return {}

    def sync(self):
        with patch('time.time',return_value=self.now):self.loop.sync_once()

    def test_explicit_pause_pending_409_keeps_server_paused(self):
        self.pending=1
        with self.assertRaises(AppError) as caught:self.loop.control('pause')
        self.assertEqual(caught.exception.code,'dual_pause_pending');self.assertEqual(caught.exception.http_status,409)
        self.assertTrue(self.server_paused)
        self.assertEqual([body['action'] for op,body in self.operations if op=='control'],['pause'])

    def test_control_requires_matching_boolean_and_explicit_pending_owner_count(self):
        for missing in (False,True):
            if missing:self.path.unlink()
            for reply in ({},{'operator_paused':False,'pending_owners':0},
                          {'operator_paused':True,'pending_owners':'0'},
                          {'operator_paused':True,'pending_owners':-1}):
                with self.subTest(reply=reply,missing_config=missing):
                    self.client.call.side_effect=None;self.client.call.return_value=reply
                    with self.assertRaises(AppError):self.loop.control('pause')
        self.write_config();self.client.call.return_value={'operator_paused':True,'pending_owners':0}
        with self.assertRaises(AppError):self.loop.control('resume')

    def test_resume_checks_both_local_gates_before_any_rpc(self):
        for allow_live,paused in ((False,False),(True,True)):
            self.runtime.settings.allow_live=allow_live;self.runtime.native.operator_paused=paused
            with self.assertRaises(AppError):self.loop.control('resume')
        self.assertEqual(self.operations,[])
        self.runtime.settings.allow_live=True;self.runtime.native.operator_paused=False
        self.assertFalse(self.loop.control('resume')['operator_paused'])

    def test_local_pause_auto_pauses_but_never_auto_resumes_and_still_mirrors(self):
        self.runtime.native.operator_paused=True;self.sync()
        self.assertTrue(self.server_paused);self.bridge.sync.assert_called_once()
        self.runtime.native.operator_paused=False;self.sync()
        self.assertTrue(self.server_paused);self.assertEqual(self.bridge.sync.call_count,2)
        self.assertEqual([body['action'] for op,body in self.operations if op=='control'],['pause'])

    def test_allow_live_false_also_auto_pauses_and_does_not_skip_result_collection(self):
        self.runtime.settings.allow_live=False;self.sync()
        self.assertTrue(self.server_paused);self.bridge.sync.assert_called_once()

    def test_auto_pause_without_matching_confirmation_is_blocked_but_still_mirrors(self):
        self.runtime.native.operator_paused=True
        original=self.client.call.side_effect
        self.client.call.side_effect=lambda op,**body: {} if op=='control' else original(op,**body)
        self.sync()
        self.assertEqual(self.loop.state['status'],'blocked');self.bridge.sync.assert_called_once()

    def test_revoked_access_cancels_old_task_and_never_publishes(self):
        row={'_id':self.account.id,'revision':1,'credential_revision':1,'plan':self.plan,'date_exceptions':{}}
        self.known=plan_tasks([row],self.reg,self.runtime.accounts.catalog,now=self.now,days=2)
        self.assertGreaterEqual(len(self.known),1)
        self.runtime.auth.is_allowed.return_value=False;self.sync()
        self.assertIn('cancel',[op for op,_ in self.operations])
        self.assertNotIn('publish',[op for op,_ in self.operations]);self.bridge.sync.assert_called_once()

    def test_remember_auth_window_must_finish_before_publish(self):
        events=[]
        self.runtime.managed_execution.remember_auth_window.side_effect=lambda *a:events.append('remember')
        original=self.client.call.side_effect
        def call(op,**body):
            if op=='publish':events.append('publish')
            return original(op,**body)
        self.client.call.side_effect=call;self.sync()
        self.assertTrue(events)
        self.assertEqual(events,['remember','publish']*(len(events)//2))
        self.operations=[]
        self.runtime.managed_execution.remember_auth_window.side_effect=AppError('fixture_block','blocked',409)
        self.sync();self.assertNotIn('publish',[op for op,_ in self.operations])
        self.assertEqual(self.loop.state['status'],'blocked')

    def test_disabled_without_managed_scope_does_not_open_placeholder_ssh(self):
        self.config['enabled']=False;self.write_config()
        self.runtime.managed_execution.has_managed_scope.return_value=False
        self.runtime.settings.allow_live=False;self.runtime.native=None
        self.loop.client=None;self.loop.client_identity=None
        with patch('local_app.dual_node.transport.SSHTransport',side_effect=AssertionError('unexpected SSH')):
            for action in ('pause','resume'):self.assertIsInstance(self.loop.control(action),dict)

    def test_disabled_or_missing_managed_configuration_never_claims_clean_noop(self):
        self.config['enabled']=False;self.write_config()
        self.client.call.side_effect=OSError('fixture connection unavailable')
        with self.assertRaises(AppError):self.loop.control('pause')
        with self.assertRaises(AppError):self.loop.control('resume')
        self.path.unlink()
        with self.assertRaises(AppError):self.loop.control('pause')


class ScopeInventoryTests(unittest.TestCase):
    def test_inventory_is_readonly_and_corrupt_partial_scope_remains_error(self):
        with TemporaryDirectory(prefix='njfu-control-scope-') as temporary:
            root=Path(temporary).resolve();gate=ManagedAccountGate(root)
            self.assertFalse(gate.has_managed_scope());self.assertEqual(list(root.iterdir()),[])
            gate.stage('fixture-one',native_disable=lambda:NativeFenceAck('fixture-one','a'*64,1,False,False,True))
            self.assertTrue(gate.has_managed_scope())
            gate.registry_path.unlink()
            with self.assertRaises(AppError):gate.has_managed_scope()


if __name__=='__main__':unittest.main()
