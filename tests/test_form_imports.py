import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.models import AppError, SchoolRecord, Submission, ZONE
from local_app.auth import AuthService
from local_app.reservations import ReservationService
from local_app.today import TodayBookingService
from support.account_fixture import School, services
from support.auth_fixture import grant, root_session
from support.mongo_fixture import MongoFixture
from test_form_files import ROW, csv_file, duplicate_applicant_sheet


class ImportSchool(School):
    records={};posts=[];logins=[]
    active_statuses=frozenset({1027})
    def login(self):
        self.logins.append(self.student_id)
        return super().login()
    def query(self,*args):return list(self.records.get(self.student_id,[]))
    def submit(self,run,seat):
        assert self.allow_live
        self.posts.append((self.student_id,seat))
        self.records[self.student_id]=[SchoolRecord('receipt',self.student_id,seat,f'{run.target_date} {run.begin}:00',f'{run.target_date} {run.end}:00',1027)]
        return Submission('accepted',code=0,sent=True)


class FormImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        try:from local_app.form_imports import FormImportService
        except ModuleNotFoundError:self.fail('Reviewed form imports are not implemented')
        self.db=self.mongo.database();self.temp,self.store,self.accounts,self.catalog=services(self.db)
        self.accounts.client_factory=ImportSchool;ImportSchool.records={};ImportSchool.posts=[];ImportSchool.logins=[]
        self.auth=AuthService(self.store,self.accounts);grant(self.auth,['99000001','99000002'])
        self.accounts.is_allowed=self.auth.is_allowed
        self.now=datetime(2026,10,23,8,10,tzinfo=ZONE).timestamp()
        self.accounts.wall_clock=lambda:self.now
        self.reservations=ReservationService(self.accounts,self.store,True,wall_clock=lambda:self.now)
        self.today=TodayBookingService(self.accounts,self.reservations,self.store)
        self.addCleanup(self.today.close)
        self.forms=FormImportService(self.accounts,self.store,self.auth,self.today,clock=lambda:self.now)
    def tearDown(self):
        if not hasattr(self,'forms'):return
        self.forms.close();self.accounts.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def wait(self,batch):
        end=time.monotonic()+5
        while True:
            state=self.forms.get(batch['id'])
            if state['state'] not in ('validating','confirming'):return state
            if time.monotonic()>end:self.fail('Import worker did not finish')
            time.sleep(.01)
    def create(self,rows=None,key=None):return self.wait(self.forms.create('requests.csv',csv_file(rows or [ROW]),key or uuid.uuid4().hex))
    def existing(self):
        account=self.accounts.verify_and_save('99000001','old-password','Original')
        return self.accounts.save_plan(account.id,{'seat_ids':['2'],'begin':'14:00','end':'17:00','enabled':True,'trigger':'07:00:00'},account.revision)
    def test_valid_new_account_is_formal_but_plan_waits_for_one_file_confirmation(self):
        row=ROW.copy();row[1]='99000003'
        batch=self.create([row]);account=self.store.find_account_by_student('99000003')
        self.assertEqual(batch['state'],'ready');self.assertEqual(batch['ready_count'],1)
        self.assertIsNotNone(account);self.assertIsNone(account['plan']);self.assertTrue(self.auth.is_allowed('99000003'))
        self.assertEqual(self.accounts.vault.unseal(account['_id'],account['secret']),'synthetic-password')
        result=self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(result['rows'][0]['status'],'done')
        self.assertTrue(self.store.get_account(account['_id'])['plan']['enabled'])
        self.assertEqual(ImportSchool.posts,[])
    def test_multi_person_file_is_rejected_before_login_or_database_writes(self):
        other=ROW.copy();other[1]='99000002'
        for filename,content in [('roster.csv',csv_file([ROW,other])),('visible.xlsx',duplicate_applicant_sheet()),('hidden.xlsx',duplicate_applicant_sheet(True))]:
            with self.subTest(filename=filename),self.assertRaisesRegex(AppError,'一人|一位|工作表'):
                self.forms.create(filename,content,uuid.uuid4().hex)
        self.assertEqual(ImportSchool.logins,[])
        self.assertEqual(self.store.accounts.count_documents({}),0)
        self.assertEqual(self.forms.batches.count_documents({}),0)
    def test_existing_account_and_prepared_history_stay_byte_identical_before_confirmation(self):
        account=self.existing();before=self.store.get_account(account.id)
        batch=self.create();self.assertEqual(batch['ready_count'],1)
        self.assertEqual(self.store.get_account(account.id),before)
        self.assertEqual(self.store.runs.count_documents({}),0)
        self.assertNotIn('synthetic-password',json.dumps(batch));self.assertNotIn('old-password',json.dumps(batch))
        saved=self.wait(self.forms.confirm(batch['id']));after=self.store.get_account(account.id)
        self.assertEqual(saved['rows'][0]['status'],'done');self.assertEqual(after['plan']['seat_ids'],['1','2'])
    def test_invalid_login_and_revoked_access_never_create_or_reenable_accounts(self):
        row=ROW.copy();row[2]='wrong'
        batch=self.create([row]);self.assertEqual(batch['ready_count'],0)
        self.assertIsNone(self.store.find_account_by_student('99000001'))
        self.auth.access.update_one({'_id':'99000001'},{'$set':{'enabled':False}})
        batch=self.create();self.assertEqual(batch['ready_count'],0)
        self.assertFalse(self.auth.is_allowed('99000001'))
    def test_changed_config_after_review_is_not_overwritten(self):
        account=self.existing();batch=self.create()
        self.accounts.save_plan(account.id,{'seat_ids':['2'],'begin':'15:00','end':'18:00','enabled':True,'trigger':'07:00:00'},account.revision)
        before=self.store.get_account(account.id)
        result=self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(result['rows'][0]['status'],'error');self.assertEqual(self.store.get_account(account.id),before)
    def test_today_confirmation_keeps_the_original_schedule_and_submits_only_once(self):
        account=self.existing();plan=self.store.get_account(account.id)['plan']
        row=ROW.copy();row[3]='当天一次预约';row[8:11]=['2026-10-23','','']
        key=uuid.uuid4().hex;batch=self.create([row],key);self.assertEqual(ImportSchool.posts,[])
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.forms.confirm(batch['id']),range(2)))
        result=self.wait(batch);self.assertEqual(result['rows'][0]['status'],'done')
        self.forms.confirm(batch['id']);self.assertEqual(len(ImportSchool.posts),1)
        self.assertEqual(self.store.get_account(account.id)['plan'],plan)
        self.assertEqual(self.create([row],key)['id'],batch['id']);self.assertEqual(len(ImportSchool.posts),1)
    def test_closed_live_gate_cannot_submit_or_replace_existing_credentials(self):
        account=self.existing();row=ROW.copy();row[3]='当天一次预约';row[8:11]=['2026-10-23','','']
        batch=self.create([row]);before=self.store.get_account(account.id)
        self.reservations.allow_live=False;result=self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(result['rows'][0]['status'],'error');self.assertEqual(ImportSchool.posts,[])
        self.assertEqual(self.store.get_account(account.id),before)
    def test_expiry_and_release_window_stop_before_side_effects(self):
        account=self.existing();batch=self.create();before=self.store.get_account(account.id)
        self.now+=901
        with self.assertRaises(AppError):self.forms.confirm(batch['id'])
        self.assertEqual(self.store.get_account(account.id),before)
        self.now=datetime(2026,10,24,6,59,tzinfo=ZONE).timestamp();calls=len(ImportSchool.logins)
        blocked=self.create();self.assertEqual(blocked['ready_count'],0)
        self.assertEqual(len(ImportSchool.logins),calls)
    def test_payload_is_encrypted_and_removed_after_confirmation(self):
        batch=self.create();raw=self.db.campus_form_imports.find_one({'_id':batch['id']})
        self.assertNotIn('synthetic-password',json.dumps(raw,default=str));self.assertIn('payload',raw)
        self.wait(self.forms.confirm(batch['id']))
        self.assertNotIn('payload',self.db.campus_form_imports.find_one({'_id':batch['id']}))
    def test_import_routes_are_local_root_only_and_require_csrf(self):
        from local_app.config import load_settings
        from local_app.web import create_app
        app=create_app(load_settings(Path(__file__).resolve().parents[1]),self.accounts,self.store,self.catalog,lambda:{},auth=self.auth,today=self.today,form_imports=self.forms)
        client=app.test_client();root_session(self.auth,client);origin='http://127.0.0.1:5003'
        csrf=client.get('/api/local/bootstrap',base_url=origin).json['csrf']
        body={'filename':'requests.csv','content':base64.b64encode(csv_file([ROW])).decode(),'request_id':uuid.uuid4().hex}
        response=client.post('/api/local/admin/form-imports',json=body,base_url=origin,headers={'Origin':origin,'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,200);self.assertNotIn('synthetic-password',response.get_data(as_text=True))
        self.assertEqual(client.post('/api/local/admin/form-imports',json=body,base_url=origin,headers={'Origin':origin}).status_code,403)
        self.wait(response.json)
        account=self.store.find_account_by_student('99000001')
        self.assertIsNotNone(account)
        client.set_cookie('njfu_autolib_session',self.auth._issue('student',account_id=account['_id'],student_id='99000001'),domain='127.0.0.1')
        self.assertEqual(client.get('/api/local/admin/form-imports',base_url=origin).status_code,403)
    def test_catalog_change_during_review_cannot_apply_an_invalid_plan(self):
        account=self.existing();batch=self.create();before=self.store.get_account(account.id)
        self.catalog.pop('1')
        result=self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(result['rows'][0]['status'],'error')
        self.assertEqual(self.store.get_account(account.id),before)
    def test_failed_payload_read_ends_validation_instead_of_spinning_forever(self):
        with patch.object(self.accounts.vault,'unseal',side_effect=AppError('credential_unavailable','不可读取')):
            result=self.create()
        self.assertEqual(result['state'],'failed');self.assertEqual(ImportSchool.logins,[])
    def test_revoked_access_during_login_does_not_create_a_formal_account(self):
        original=ImportSchool.login
        def revoke(client):
            identity=original(client)
            self.auth.access.update_one({'_id':client.student_id},{'$set':{'enabled':False}})
            return identity
        with patch.object(ImportSchool,'login',revoke):result=self.create()
        self.assertEqual(result['ready_count'],0)
        self.assertIsNone(self.store.find_account_by_student('99000001'))
    def test_quiet_window_protects_custom_trigger_across_midnight(self):
        account=self.existing()
        self.store.accounts.update_one({'_id':account.id},{'$set':{'plan.trigger':'00:00:00'}})
        self.now=datetime(2026,10,23,23,59,tzinfo=ZONE).timestamp();before=len(ImportSchool.logins)
        result=self.create();self.assertEqual(result['ready_count'],0)
        self.assertEqual(len(ImportSchool.logins),before)
    def test_restart_marks_unfinished_import_interrupted_without_replaying(self):
        batch=self.create();self.forms.close()
        self.db.campus_form_imports.update_one({'_id':batch['id']},{'$set':{'state':'confirming'}})
        from local_app.form_imports import FormImportService
        self.forms=FormImportService(self.accounts,self.store,self.auth,self.today,clock=lambda:self.now)
        state=self.forms.confirm(batch['id'])
        self.assertEqual(state['state'],'interrupted');self.assertEqual(ImportSchool.posts,[])
    def test_authenticated_student_public_root_and_anonymous_cannot_get_imports_or_template(self):
        from local_app.config import load_settings
        from local_app.web import create_app
        account=self.existing();origin='https://library-fixture.example:45581';relay='f'*64
        app=create_app(load_settings(Path(__file__).resolve().parents[1],{'public_origin':origin,'relay_token':relay}),self.accounts,self.store,self.catalog,lambda:{},auth=self.auth,today=self.today,form_imports=self.forms)
        client=app.test_client();local='http://127.0.0.1:5003'
        for path in ('/api/local/admin/form-imports','/api/local/admin/form-template'):
            self.assertEqual(client.get(path,base_url=local).status_code,401)
            client.set_cookie('njfu_autolib_session',self.auth._issue('student',account_id=account.id,student_id='99000001'),domain='127.0.0.1')
            self.assertEqual(client.get(path,base_url=local).status_code,403)
            client.delete_cookie('njfu_autolib_session',domain='127.0.0.1')
            client.set_cookie('njfu_autolib_session',self.auth._issue('root'),domain='library-fixture.example')
            self.assertEqual(client.get(path,base_url=origin,headers={'X-NJFU-Relay-Token':relay,'X-NJFU-Relay-HTTPS':'1','X-NJFU-Client-IP':'198.51.100.2'}).status_code,403)
    def manual_row(self):
        row=ROW.copy();row[3]='当天一次预约';row[8:11]=['2026-10-23','',''];return row
    def test_import_confirmation_auth_and_query_leave_the_account_lock_available(self):
        account=self.existing();batch=self.create([self.manual_row()]);available=[]
        original_login=ImportSchool.login;original_query=ImportSchool.query
        def probe():
            def acquire():
                lock=self.accounts.account_lock(account.id);ok=lock.acquire(blocking=False)
                if ok:lock.release()
                return ok
            with ThreadPoolExecutor(max_workers=1) as pool:available.append(pool.submit(acquire).result())
        def login(client):probe();return original_login(client)
        def query(client,*args):probe();return original_query(client,*args)
        with patch.object(ImportSchool,'login',login),patch.object(ImportSchool,'query',query):self.wait(self.forms.confirm(batch['id']))
        self.assertTrue(available);self.assertTrue(all(available));self.assertEqual(len(ImportSchool.posts),1)
    def test_slow_confirmation_io_cannot_outlive_file_expiry_or_enter_release_window(self):
        account=self.existing()
        for phase in ('login','query'):
            for boundary in ('expiry','release'):
                with self.subTest(phase=phase,boundary=boundary):
                    self.now=datetime(2026,10,23,8,10,56,tzinfo=ZONE).timestamp()
                    self.store.accounts.update_one({'_id':account.id},{'$set':{'plan.trigger':'08:14:00'}})
                    batch=self.create([self.manual_row()]);self.assertEqual(batch['ready_count'],1)
                    if boundary=='expiry':self.db.campus_form_imports.update_one({'_id':batch['id']},{'$set':{'expires_at':self.now+1}})
                    original=getattr(ImportSchool,phase);posts=len(ImportSchool.posts)
                    def slow(client,*args):self.now+=2;return original(client,*args)
                    with patch.object(ImportSchool,phase,slow):result=self.wait(self.forms.confirm(batch['id']))
                    self.assertEqual(len(ImportSchool.posts),posts);self.assertEqual(result['rows'][0]['status'],'error')
    def test_auth_observer_is_unchanged_before_review_and_clears_block_after_commit(self):
        from local_app.inbox import InboxEngine
        account=self.existing();engine=InboxEngine(self.accounts,self.store,self.auth)
        self.accounts.auth_observer=engine.note_auth
        for password in ('old-password','synthetic-password'):
            with self.subTest(password_changed=password!='old-password'):
                current=self.store.get_account(account.id);access=self.auth.access.find_one({'_id':'99000001'})
                engine.verifier(current,access);engine.note_auth(account.id,'login_failed')
                before=engine.verifiers.find_one({'_id':account.id})
                row=ROW.copy();row[2]=password;batch=self.create([row])
                self.assertEqual(engine.verifiers.find_one({'_id':account.id}),before)
                self.wait(self.forms.confirm(batch['id']))
                state=engine.verifier(self.store.get_account(account.id),access)
                self.assertIsNone(state['blocked'])
    def test_last_run_read_crossing_the_quiet_boundary_stops_before_post(self):
        account=self.existing();self.now=datetime(2026,10,23,8,10,56,tzinfo=ZONE).timestamp()
        self.store.accounts.update_one({'_id':account.id},{'$set':{'plan.trigger':'08:14:00'}})
        batch=self.create([self.manual_row()]);original=self.store.get_run;advanced=[]
        def delayed(key):
            value=original(key)
            if value and value['status']=='submitting' and not advanced:self.now+=2;advanced.append(True)
            return value
        with patch.object(self.store,'get_run',delayed):self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(ImportSchool.posts,[])
    def test_imported_manual_retry_keeps_the_existing_thirty_second_attempt_budget(self):
        self.existing();batch=self.create([self.manual_row()]);original=ImportSchool.submit;first=[]
        def slow_busy(client,run,seat):
            if not first:
                first.append(True);ImportSchool.posts.append((client.student_id,seat));self.now+=31
                return Submission('rejected',retryable=True,code=1,sent=True)
            return original(client,run,seat)
        with patch.object(ImportSchool,'submit',slow_busy):self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(len(ImportSchool.posts),1)

    def paced_import(self, *, post_ms=112, late_receipt=False, stop=None):
        """Virtual-time fake school enforces both user-selected retry floors."""
        self.existing();batch=self.create([self.manual_row()])
        elapsed=[0.0];starts=[];waits=[];first=[]
        original_submit,original_query=ImportSchool.submit,ImportSchool.query
        def advance(seconds):elapsed[0]+=seconds;self.now+=seconds
        def wait(seconds):
            waits.append(seconds);advance(seconds)
            if stop=='live':self.reservations.allow_live=False
            if stop=='expiry':advance(31)
        def query(client,*args):
            advance(.01)
            if late_receipt and first and elapsed[0]-starts[0]>=.5:
                run,seat=first[0]
                ImportSchool.records[client.student_id]=[SchoolRecord('late-receipt',client.student_id,seat,
                    f'{run.target_date} {run.begin}:00',f'{run.target_date} {run.end}:00',1027)]
            return original_query(client,*args)
        def submit(client,run,seat):
            starts.append(elapsed[0]);advance(post_ms/1000)
            if len(starts)==1:
                first.append((run,seat));ImportSchool.posts.append((client.student_id,seat))
                return Submission('rejected',retryable=True,code=1,sent=True,timings={'request_ms':post_ms})
            gap=starts[-1]-starts[-2]
            if gap<1.2 or gap-post_ms/1000<1:
                ImportSchool.posts.append((client.student_id,seat))
                return Submission('rejected',message='fixture rate limit',code=1,sent=True)
            return original_submit(client,run,seat)
        self.reservations.monotonic=lambda:elapsed[0];self.reservations.wait=wait
        with patch.object(ImportSchool,'submit',submit),patch.object(ImportSchool,'query',query):
            result=self.wait(self.forms.confirm(batch['id']))
        return result,starts,waits

    def test_form_today_busy_retry_observes_mainline_spacing_before_readback(self):
        result,starts,waits=self.paced_import()
        self.assertEqual(result['rows'][0]['result'],'success')
        self.assertEqual(len(starts),2);self.assertGreaterEqual(starts[1]-starts[0],1.2)
        self.assertGreaterEqual(starts[1]-starts[0]-.112,1)
        self.assertLess(starts[0],.05,'The first submission must not wait')
        self.assertAlmostEqual(sum(waits),1.088,places=6)
        run=self.store.get_run(result['rows'][0]['run_id'])
        self.assertEqual(run['attempts'][0]['retry_check']['spacing_ms'],1200)
        self.assertEqual(run['attempts'][0]['retry_check']['response_cooldown_ms'],1000)
        self.assertEqual(run['attempts'][0]['retry_check']['previous_request_ms'],112)

    def test_form_today_slow_busy_response_keeps_response_cooldown(self):
        result,starts,waits=self.paced_import(post_ms=650)
        self.assertEqual(result['rows'][0]['result'],'success')
        self.assertEqual(len(starts),2);self.assertGreaterEqual(starts[1]-starts[0],1.65)
        self.assertAlmostEqual(sum(waits),1,places=6,msg='A slow response cannot consume the response cooldown')

    def test_form_today_late_receipt_after_wait_prevents_another_submission(self):
        result,starts,waits=self.paced_import(late_receipt=True)
        self.assertEqual(result['rows'][0]['result'],'success')
        self.assertEqual(len(starts),1);self.assertTrue(waits)

    def test_form_today_stops_if_live_gate_changes_during_wait(self):
        result,starts,waits=self.paced_import(stop='live')
        self.assertEqual(len(starts),1);self.assertTrue(waits)
        self.assertEqual(result['rows'][0]['status'],'error')
        run=self.store.runs.find_one({'spec.kind':'manual'})
        self.assertEqual(run['attempts'][0]['retry_check']['state'],'stopped')

    def test_form_today_stops_when_attempt_budget_expires_during_wait(self):
        result,starts,waits=self.paced_import(stop='expiry')
        self.assertEqual(len(starts),1);self.assertTrue(waits)
        self.assertEqual(result['rows'][0]['status'],'error')
        run=self.store.runs.find_one({'spec.kind':'manual'})
        self.assertEqual(run['attempts'][0]['retry_check']['state'],'stopped')


if __name__=='__main__':unittest.main()
