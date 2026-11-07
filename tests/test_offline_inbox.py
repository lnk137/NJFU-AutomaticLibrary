"""Local inbox tests use isolated Mongo and a fake school boundary only."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import sys
import threading
import unittest
import uuid
from werkzeug.security import check_password_hash

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.account_fixture import services
from support.auth_fixture import grant
from local_app.auth import AuthService
from local_app.models import ZONE
from local_app.inbox import InboxEngine, apply_quiet, io_quiet
from local_app.inbox import InboxWorker, configured_worker

PLAN={'seat_ids':['1'],'mode':'day','time_mode':'full_day','day_offset':1,'begin':'07:30',
      'end':'22:00','trigger':'07:00:00','enabled':True}


class InboxEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        self.db=self.mongo.database();self.temp,self.store,self.accounts,self.catalog=services(self.db)
        self.auth=AuthService(self.store,self.accounts);self.accounts.is_allowed=self.auth.is_allowed
        grant(self.auth,['2310000001','2310000002'])
        self.a=self.accounts.verify_and_save('2310000001','pass','A')
        self.b=self.accounts.verify_and_save('2310000002','pass','B')
        self.now=datetime(2026,10,24,17,tzinfo=ZONE).timestamp()
        self.engine=InboxEngine(self.accounts,self.store,self.auth,clock=lambda:self.now)
        self.packet=self.engine.snapshot({'allow_live':True,'clock_ok':True})
    def tearDown(self):
        self.accounts.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def job(self,account=None,**changes):
        account=account or self.a
        item=next(x for x in self.packet['accounts'] if x['student_id']==account.student_id)
        return {'request_id':str(uuid.uuid4()),'student_id':account.student_id,'version':1,
                'generation':item['auth']['generation'],'expected_revision':account.revision,
                'plan':dict(PLAN),'created_at':self.now-1,'expires_at':self.now+86400,'attempt':1,**changes}

    def test_verifier_export_preserves_encrypted_password_and_existing_collections(self):
        row=self.store.get_account(self.a.id)
        item=next(x for x in self.packet['accounts'] if x['student_id']==self.a.student_id)
        self.assertTrue(check_password_hash(item['auth']['password_hash'],'pass'))
        self.assertNotIn('secret',str(self.packet));self.assertNotIn('password\':',str(self.packet))
        self.assertEqual(self.accounts.vault.unseal(self.a.id,row['secret']),'pass')
        self.assertEqual(row,self.store.get_account(self.a.id))

    def test_initial_verifier_respects_known_school_rejection_newer_than_last_success(self):
        from datetime import timedelta
        row=self.store.get_account(self.a.id)
        failure=(datetime.fromisoformat(row['verified_at'])+timedelta(seconds=1)).isoformat()
        self.engine.verifiers.delete_one({'_id':self.a.id})
        self.store.accounts.update_one({'_id':self.a.id},{'$set':{'query_error':'login_failed','query_error_at':failure}})
        item=next(x for x in self.engine.snapshot({})['accounts'] if x['student_id']==self.a.student_id)
        self.assertEqual(item['auth']['blocked'],'login_failed')

    def test_atomic_apply_and_replay_increment_revision_once(self):
        job=self.job();first=self.engine.apply(job);second=self.engine.apply(job)
        self.assertEqual(first['state'],'applied');self.assertEqual(first,second)
        self.assertEqual(self.store.get_account(self.a.id)['revision'],2)
        self.assertEqual(self.store.get_account(self.a.id)['plan']['seat_ids'],['1'])

    def test_crash_after_commit_before_receipt_recovers_without_clobbering_later_edit(self):
        job=self.job();result=self.engine.apply(job)
        self.db.campus_inbox_receipts.delete_one({'_id':job['request_id']})
        row=self.store.get_account(self.a.id)
        self.accounts.save_plan(self.a.id,{**PLAN,'seat_ids':['2']},row['revision'])
        self.assertEqual(self.engine.apply(job),result)
        self.assertEqual(self.store.get_account(self.a.id)['plan']['seat_ids'],['2'])
        self.assertEqual(self.store.get_account(self.a.id)['revision'],3)

    def test_conflict_expiry_generation_and_revocation_never_change_plan(self):
        for change in ({'expected_revision':99},{'expires_at':self.now-1},{'generation':'stale'}):
            before=self.store.get_account(self.a.id)
            result=self.engine.apply(self.job(**change))
            self.assertEqual(result['state'],'failed');self.assertEqual(self.store.get_account(self.a.id),before)
        job=self.job();self.auth.access.update_one({'_id':self.a.student_id},{'$set':{'enabled':False}})
        self.assertEqual(self.engine.apply(job)['state'],'failed')
        self.assertIsNone(self.store.get_account(self.a.id)['plan'])

    def test_failed_account_does_not_prevent_another_account_applying(self):
        self.engine.note_auth(self.a.id,'login_failed')
        self.assertEqual(self.engine.apply(self.job())['state'],'failed')
        self.assertEqual(self.engine.apply(self.job(self.b))['state'],'applied')

    def test_busy_account_lock_defers_without_blocking_another_account(self):
        acquired=threading.Event();release=threading.Event()
        def holder():
            with self.accounts.account_lock(self.a.id):acquired.set();release.wait(5)
        thread=threading.Thread(target=holder);thread.start();acquired.wait(2)
        try:
            self.assertEqual(self.engine.apply(self.job())['state'],'deferred')
            self.assertEqual(self.engine.apply(self.job(self.b))['state'],'applied')
        finally:release.set();thread.join()

    def test_school_rejection_and_friday_short_window_do_not_commit(self):
        from local_app.models import AppError
        client=self.accounts.client(self.a.id)
        original=self.accounts.client
        def rejected(account_id,**kwargs):raise AppError('login_failed','fixture rejected',401)
        self.accounts.client=rejected
        try:self.assertEqual(self.engine.apply(self.job())['state'],'failed')
        finally:self.accounts.client=original
        job=self.job(plan={**PLAN,'time_mode':'custom','begin':'19:00','end':'22:00'})
        self.assertEqual(self.engine.apply(job)['state'],'failed')
        self.assertIsNone(self.store.get_account(self.a.id)['plan'])

    def test_prepare_window_defers_config_and_has_no_write_to_original_account(self):
        self.now=datetime(2026,10,25,6,59,45,tzinfo=ZONE).timestamp()
        before=self.store.get_account(self.a.id)
        self.assertEqual(self.engine.apply(self.job())['state'],'deferred')
        self.assertEqual(self.store.get_account(self.a.id),before)

    def test_authentication_block_requires_new_school_success(self):
        self.engine.note_auth(self.a.id,'verification_required')
        blocked=self.engine.snapshot({})['accounts'][0]['auth']
        self.assertEqual(blocked['blocked'],'verification_required')
        self.engine.note_auth(self.a.id,None)
        cleared=self.engine.snapshot({})['accounts'][0]['auth']
        self.assertIsNone(cleared['blocked']);self.assertNotEqual(blocked['generation'],cleared['generation'])

    def test_real_authentication_observer_tracks_saved_credential_rejection_and_success(self):
        from local_app.models import AppError
        from support.account_fixture import School
        from unittest.mock import patch
        self.accounts.auth_observer=self.engine.note_auth
        with patch.object(School, 'login', side_effect=AppError('login_failed', 'fixture saved password rejected', 401)):
            with self.assertRaises(AppError):
                self.accounts.verify_and_save(self.a.student_id,'pass','A',self.a.revision)
        self.assertEqual(self.engine.verifiers.find_one({'_id':self.a.id})['blocked'],'login_failed')
        self.accounts.verify_and_save(self.a.student_id,'pass','A',self.a.revision)
        self.assertIsNone(self.engine.verifiers.find_one({'_id':self.a.id})['blocked'])

    def test_bad_cloud_ack_does_not_block_other_account_or_lease_renewal(self):
        from concurrent.futures import Future
        from types import SimpleNamespace
        sys.path.insert(0,str(ROOT))
        from deploy.inbox_store import InboxStore
        from local_app.models import AppError
        cloud=InboxStore(Path(self.temp.name)/'cloud/inbox.sqlite',clock=lambda:self.now)
        cloud.publish(self.packet)
        for account in [self.a,self.b]:
            token=cloud.issue(cloud.authenticate(account.student_id,'pass',account.student_id))
            cloud.enqueue(account.student_id,{'request_id':str(uuid.uuid4()),'expected_revision':account.revision,'plan':PLAN},token)
        jobs=cloud.poll();scheduler=SimpleNamespace(status=lambda:{'clock_ok':True,'allow_live':True})
        worker=InboxWorker(SimpleNamespace(),self.accounts,self.store,self.auth,scheduler,{})
        worker.engine=self.engine
        for job in jobs:
            future=Future();future.set_result(self.engine.apply(job));worker.jobs[job['request_id']]=(job,future)
        bad=jobs[0]['request_id']
        def transport(path,data,**kwargs):
            if path=='ack':
                if data['request_id']==bad:raise AppError('lease_changed','fixture lease changed',409)
                cloud.ack(data['request_id'],data['lease'],data['result']);return {'ok':True}
            if path=='snapshot':cloud.publish(data);return {'ok':True}
            return {'jobs':cloud.poll(),'summary':cloud.summary()}
        worker.request=transport
        try:
            worker.tick()
            import time
            deadline=time.monotonic()+2
            while cloud.receipt(jobs[1]['student_id'],jobs[1]['request_id'])['state']!='applied' and time.monotonic()<deadline:
                time.sleep(.01);worker.tick()
            self.assertEqual(cloud.receipt(jobs[1]['student_id'],jobs[1]['request_id'])['state'],'applied')
            self.assertIsNotNone(worker.status()['last_version_check_at'])
            worker._status['local_accounts']={'private-other':{'student_id':'private-other'}}
            self.assertNotIn('private-other',str(worker.status(self.a.student_id)))
        finally:worker.close()

    def test_invalid_optional_inbox_config_does_not_disable_existing_runtime(self):
        from types import SimpleNamespace
        folder=Path(self.temp.name)/'inbox';folder.mkdir(mode=0o700)
        path=folder/'config.json';path.write_text('{}');path.chmod(0o600)
        worker=configured_worker(SimpleNamespace(data_dir=Path(self.temp.name)),self.accounts,self.store,self.auth,None)
        self.assertTrue(worker.status()['configured']);self.assertFalse(worker.status()['connected'])
        self.assertEqual(self.store.get_account(self.a.id)['revision'],1)

    def test_local_root_monitor_and_student_summary_preserve_role_boundaries(self):
        from types import SimpleNamespace
        from local_app.config import load_settings
        from local_app.web import create_app
        from support.auth_fixture import root_session
        worker=InboxWorker(SimpleNamespace(),self.accounts,self.store,self.auth,SimpleNamespace(status=lambda:{}),{})
        worker._status['accounts']=[{'student_id':self.a.student_id,'name':'A'},{'student_id':self.b.student_id,'name':'B'}]
        app=create_app(load_settings(ROOT),self.accounts,self.store,self.catalog,lambda:{},auth=self.auth,inbox=worker)
        client=app.test_client();origin='http://127.0.0.1:5003';root_session(self.auth,client)
        try:
            self.assertEqual(len(client.get('/api/local/admin/inbox',base_url=origin).json['accounts']),2)
            client.set_cookie('njfu_autolib_session',self.auth._issue('student',account_id=self.a.id,student_id=self.a.student_id),domain='127.0.0.1')
            self.assertEqual(client.get('/api/local/admin/inbox',base_url=origin).status_code,403)
            result=client.get('/api/local/inbox',base_url=origin)
            self.assertEqual([a['student_id'] for a in result.json['accounts']],[self.a.student_id])
            self.assertNotIn('password_hash',result.get_data(as_text=True))
        finally:worker.close()

    def test_cloud_commit_crash_new_login_and_restart_recovers_original_confirmation(self):
        sys.path.insert(0,str(ROOT));from deploy.inbox_store import InboxStore
        cloud=InboxStore(Path(self.temp.name)/'recovery/inbox.sqlite',clock=lambda:self.now)
        cloud.publish(self.packet)
        request_id=str(uuid.uuid4())
        token=cloud.issue(cloud.authenticate(self.a.student_id,'pass','fixture'))
        cloud.enqueue(self.a.student_id,{'request_id':request_id,'expected_revision':1,'plan':PLAN},token)
        job=cloud.poll()[0];result=self.engine.apply(job)
        self.assertEqual(result['state'],'applied')
        self.db.campus_inbox_receipts.delete_one({'_id':request_id})
        self.accounts.verify_and_save(self.a.student_id,'pass','A',2)
        cloud.publish(self.engine.snapshot({}));self.now+=61
        recovered=cloud.poll()
        self.assertEqual(len(recovered),1)
        original=self.accounts.client
        self.accounts.client=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('Recovery must not authenticate again'))
        try:confirmed=self.engine.apply(recovered[0])
        finally:self.accounts.client=original
        cloud.ack(request_id,recovered[0]['lease'],confirmed)
        self.assertEqual(cloud.receipt(self.a.student_id,request_id)['state'],'applied')
        self.assertEqual(self.store.get_account(self.a.id)['revision'],3)

    def test_single_account_export_failure_does_not_stop_publishing_or_polling(self):
        from types import SimpleNamespace
        worker=InboxWorker(SimpleNamespace(),self.accounts,self.store,self.auth,SimpleNamespace(status=lambda:{'clock_ok':True}),{})
        original=self.accounts.snapshot
        self.accounts.snapshot=lambda account_id:(_ for _ in ()).throw(RuntimeError('one bad snapshot')) if account_id==self.a.id else original(account_id)
        calls=[]
        def transport(path,data,**kwargs):
            calls.append(path)
            if path=='poll':return {'jobs':[],'summary':{'pending':0,'failed':0,'heartbeat_at':self.now,'quiet_until':0,'accounts':[]}}
            return {'ok':True}
        worker.request=transport
        try:
            import time
            deadline=time.monotonic()+2
            while len(worker.status()['accounts'])<2 and time.monotonic()<deadline:
                worker.tick();time.sleep(.01)
            self.assertIn('poll',calls)
            rows=worker.status()['accounts']
            self.assertTrue(next(a for a in rows if a['student_id']==self.a.student_id).get('sync_error'))
            self.assertTrue(any(a['student_id']==self.b.student_id for a in rows))
        finally:self.accounts.snapshot=original;worker.close()

    def test_each_network_operation_rechecks_the_quiet_boundary(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        worker=InboxWorker(SimpleNamespace(),self.accounts,self.store,self.auth,SimpleNamespace(status=lambda:{'clock_ok':True}),{})
        clock=[datetime(2026,10,25,6,59,44,tzinfo=ZONE).timestamp()];calls=[]
        def transport(path,data,**kwargs):
            calls.append((path,clock[0]));clock[0]+=2
            return {'ok':True,'jobs':[],'summary':{'pending':0,'failed':0,'heartbeat_at':clock[0],'quiet_until':0,'accounts':[]}}
        worker.request=transport
        try:
            with patch('local_app.inbox.time.time',lambda:clock[0]):
                try:worker.tick()
                except Exception as exc:self.assertEqual(type(exc).__name__,'SyncWindow')
            self.assertEqual(len(calls),1)
            self.assertLess(calls[0][1],datetime(2026,10,25,6,59,45,tzinfo=ZONE).timestamp())
        finally:worker.close()

    def test_slow_ack_does_not_block_version_poll_for_other_accounts(self):
        from types import SimpleNamespace
        from concurrent.futures import Future
        worker=InboxWorker(SimpleNamespace(),self.accounts,self.store,self.auth,SimpleNamespace(status=lambda:{'clock_ok':True}),{})
        entered=threading.Event();release=threading.Event();polled=threading.Event()
        future=Future();future.set_result({'state':'failed','code':'fixture','message':'fixture'})
        worker.jobs['fixture-id']=({'lease':'fixture-lease'},future)
        def transport(path,data,**kwargs):
            if path=='ack':entered.set();release.wait(2);return {'ok':True}
            if path=='poll':
                polled.set();return {'jobs':[],'summary':{'pending':0,'failed':0,'heartbeat_at':self.now,'quiet_until':0,'accounts':[]}}
            return {'ok':True}
        worker.request=transport
        try:
            worker.tick();self.assertTrue(entered.wait(.5));self.assertTrue(polled.is_set())
            self.assertFalse(release.is_set());self.assertIsNotNone(worker.status()['last_version_check_at'])
        finally:release.set();worker.close()


class InboxWindowTests(unittest.TestCase):
    def test_config_deferral_and_io_quiet_boundaries(self):
        for text,apply,io in [('06:59:39',False,False),('06:59:40',True,False),
                              ('06:59:55',True,True),('07:00:40',True,True),('07:00:41',False,False)]:
            now=datetime.fromisoformat('2026-10-25T'+text+'+08:00').timestamp()
            self.assertEqual(apply_quiet(now),apply);self.assertEqual(io_quiet(now),io)


if __name__=='__main__':unittest.main()
