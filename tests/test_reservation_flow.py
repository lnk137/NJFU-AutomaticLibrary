import sys
import time
import unittest
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.school_fixture import Transport


class ReservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import base64
        from Crypto.PublicKey import RSA
        cls.mongo=MongoFixture()
        cls.public_key=base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        from support.account_fixture import services
        self.db=self.mongo.database();self.temp,self.store,self.accounts,self.catalog=services(self.db)
        a=self.accounts.verify_and_save('fixture01','pass','Alice',None)
        p=dict(seat_ids=['1','2'],day_offset=1,mode='day',begin='09:00',end='12:00',trigger='20:00:00',enabled=True)
        self.account=self.accounts.save_plan(a.id,p,a.revision)
    def tearDown(self):self.accounts.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def run_spec(self):
        from local_app.models import RunSpec
        return RunSpec(self.account.id,self.account.revision,'2026-10-20','09:00','12:00',('1','2'),time.time())

    def test_school_ambiguous_reply_stops_retries(self):
        from local_app.school import CampusClient
        from local_app.models import Identity
        import requests
        for response in ({},{'code':False},{'code':'0'},'<html>error</html>',requests.Timeout()):
            t=Transport([
                ('GET','/ic-web/sysConfig/public',{'code':0,'data':[{'sysKey':'resvCode','sysValue':'0','sysKind':3}]},200,{}),
                ('GET','/ic-web/login/publicKey',{'code':0,'data':{'publicKey':self.public_key}},200,{}),
                ('POST','/ic-web/reserve/seat',response,200,{})])
            c=CampusClient('fixture01','pass',transport=t,allow_live=True);c.identity=Identity('fixture01','Alice',123)
            result=c.submit(self.run_spec(),'1')
            self.assertEqual(result.outcome,'uncertain');self.assertFalse(result.retryable)
            self.assertEqual(sum(call[0]=='POST' for call in t.calls),1)

    def test_exact_active_record_required(self):
        from local_app.reservations import records_match
        from local_app.models import SchoolRecord,Identity
        r=self.run_spec();record=SchoolRecord('rec','fixture01','1','2026-10-20 09:00:00','2026-10-20 12:00:00',1027)
        identity=Identity('fixture01','Alice',123)
        self.assertTrue(records_match(r,'1',[record],identity,active_statuses={1027}))
        self.assertFalse(records_match(r,'1',[record],identity))
        for bad in (replace(record,student_id='other'),replace(record,seat_id='2'),replace(record,status=5313),replace(record,begin='2026-10-21 09:00:00')):
            self.assertFalse(records_match(r,'1',[bad],identity,active_statuses={1027}))

    def test_fallback_then_verified_and_duplicate_execution(self):
        from local_app.reservations import ReservationService
        from local_app.models import Submission,SchoolRecord
        c=self.accounts.client(self.account.id);calls=[];c.active_statuses={1027}
        def submit(run,seat):calls.append(seat);return Submission('rejected',True,9,'占用') if seat=='1' else Submission('accepted')
        c.submit=submit;c.query=lambda *args:([SchoolRecord('r','fixture01','2','2026-10-20 09:00:00','2026-10-20 12:00:00',1027)]
                                            if calls and calls[-1]=='2' else [])
        s=ReservationService(self.accounts,self.store,True);run=self.run_spec()
        self.assertEqual(s.execute(run).status,'success');s.execute(run)
        self.assertEqual(calls,['1','2'])

    def test_uncertain_and_closed_gate_never_try_second_seat(self):
        from local_app.reservations import ReservationService
        from local_app.models import Submission
        c=self.accounts.client(self.account.id);calls=[]
        c.submit=lambda run,seat:(calls.append(seat) or Submission('uncertain'))
        run=self.run_spec()
        self.assertEqual(ReservationService(self.accounts,self.store,False).execute(run).status,'skipped')
        self.assertEqual(calls,[])
        self.assertEqual(ReservationService(self.accounts,self.store,True).execute(run).status,'uncertain')
        self.assertEqual(calls,['1'])

    def test_rejected_school_code_and_reason_survive_in_run_history(self):
        from local_app.reservations import ReservationService
        from local_app.models import Submission
        client=self.accounts.client(self.account.id);calls=[]
        client.submit=lambda run,seat:(calls.append(seat) or Submission('rejected',False,54321,'学校明确拒绝原因'))
        run=self.run_spec()
        result=ReservationService(self.accounts,self.store,True).execute(run)
        self.assertEqual(result.status,'failed')
        self.assertIn('54321',result.message)
        self.assertIn('学校明确拒绝原因',result.message)
        row=self.store.get_run(run.key)
        self.assertEqual(row['attempts'][0]['code'],54321)
        self.assertEqual(row['attempts'][0]['message'],'学校明确拒绝原因')
        self.assertEqual(row['attempts'][0]['seat_id'],'1')
        self.assertEqual(calls,['1'])
        self.assertEqual(self.store.list_runs(None,None).items[0]['attempts'],row['attempts'])

    def test_delayed_login_rechecks_expiry_before_post(self):
        from local_app.reservations import ReservationService
        from local_app.models import Submission
        from unittest.mock import patch
        run=self.run_spec();clock=[run.due_at];c=self.accounts.client(self.account.id);calls=[]
        c.submit=lambda *args:(calls.append(True) or Submission('accepted'))
        def slow_client(*args,**kwargs):clock[0]=run.due_at+31;return c
        self.accounts.client=slow_client
        with patch('local_app.reservations.time.time',lambda:clock[0]):
            ReservationService(self.accounts,self.store,True).execute(run)
        self.assertEqual(calls,[])

    def test_friday_after_closing_is_rejected_before_school_submission(self):
        from local_app.reservations import ReservationService
        from dataclasses import replace
        c=self.accounts.client(self.account.id);calls=[]
        c.submit=lambda *args:calls.append(True)
        run=replace(self.run_spec(),target_date='2026-10-23',end='22:00')
        result=ReservationService(self.accounts,self.store,True).execute(run)
        self.assertEqual(result.status,'failed');self.assertEqual(calls,[])

    def test_legacy_two_day_plan_cannot_submit_under_new_school_rules(self):
        from local_app.reservations import ReservationService
        c=self.accounts.client(self.account.id);calls=[]
        c.submit=lambda *args:calls.append(True)
        self.store.accounts.update_one({'_id':self.account.id},{'$set':{'plan.day_offset':2}})
        result=ReservationService(self.accounts,self.store,True).execute(self.run_spec())
        self.assertEqual(result.status,'failed');self.assertEqual(calls,[])

    def test_each_fallback_rechecks_guard_and_conditional_write(self):
        from local_app.reservations import ReservationService
        from local_app.models import Submission
        from unittest.mock import patch
        from dataclasses import replace
        c=self.accounts.client(self.account.id)
        for condition in ('guard','expired','gate','write'):
            calls=[];run=replace(self.run_spec(),target_date='2026-10-'+{'guard':'20','expired':'21','gate':'22','write':'23'}[condition])
            clock=[run.due_at];service=ReservationService(self.accounts,self.store,True)
            original=self.store.transition_run
            def transition(*args,**kwargs):
                if condition=='write' and calls and args[2]=='submitting':return False
                return original(*args,**kwargs)
            def submit(r,seat):
                calls.append(seat)
                if condition=='guard':service.guard=lambda:False
                if condition=='expired':clock[0]=run.due_at+31
                if condition=='gate':service.allow_live=False
                return Submission('rejected',True,9,'fixture rejection')
            c.submit=submit
            with patch('local_app.reservations.time.time',lambda:clock[0]),patch.object(self.store,'transition_run',transition):
                service.execute(run)
            self.assertEqual(calls,['1'],condition)

    def test_manual_query_confirms_late_record_without_another_post(self):
        from local_app.reservations import ReservationService
        from local_app.models import SchoolRecord,Submission
        c=self.accounts.client(self.account.id);calls=[];c.active_statuses={1027}
        c.submit=lambda r,seat:(calls.append(seat) or Submission('accepted'))
        c.query=lambda *args:[]
        service=ReservationService(self.accounts,self.store,True)
        run=self.run_spec();service.execute(run)
        self.store.runs.update_one({'_id':run.key},{'$set':{'reconcile_count':3}})
        c.query=lambda *args:[SchoolRecord('late','fixture01','1','2026-10-20 09:00:00','2026-10-20 12:00:00',1027)]
        self.accounts.after_query=service.reconcile_account
        self.accounts.query(self.account.id)
        self.assertEqual(self.store.get_run(run.key)['status'],'success')
        self.assertEqual(calls,['1'])
