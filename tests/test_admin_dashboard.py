"""Dashboard uses real isolated storage; never contacts the school."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
from pymongo.errors import ExecutionTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.models import ZONE, AppError
from local_app.auth import AuthService
from local_app.config import load_settings
from local_app.web import create_app
from support.mongo_fixture import MongoFixture
from support.account_fixture import services
from support.auth_fixture import root_session, grant

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026,10,24,9,tzinfo=ZONE)


class AdminDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        self.db = self.mongo.database()
        self.temp,self.store,self.service,self.catalog = services(self.db)
        self.account = self.service.verify_and_save('fixture01','pass','Fixture',None)
        self.auth = AuthService(self.store,self.service);grant(self.auth,['fixture01','fixture02'])
        self.app = create_app(load_settings(ROOT),self.service,self.store,self.catalog,
            lambda:{'allow_live':True,'clock_ok':True,'scheduler_running':True},auth=self.auth)
        self.client = self.app.test_client();root_session(self.auth,self.client)
    def tearDown(self):
        self.service.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def seed(self, key, status='success', *, day='2026-10-24', kind='scheduled', attempts=None):
        due = datetime.fromisoformat(day+'T07:00:00').replace(tzinfo=ZONE).timestamp()
        spec = {'due_at':due,'account_id':self.account.id,'revision':1,'begin':'09:00','end':'12:00',
                'seat_ids':['1'],'target_date':(datetime.fromtimestamp(due,ZONE)+timedelta(days=1)).date().isoformat()}
        if kind is not None:spec['kind'] = kind
        self.db.campus_runs.insert_one({'_id':key,'account_id':self.account.id,'target_date':spec['target_date'],
            'spec':spec,'status':status,'message':'fixture reason','attempts':attempts or [],'secret':'never-return-this'})
        return due
    def dashboard(self, **params):
        from local_app.dashboard import build_dashboard
        return build_dashboard(self.store,self.catalog,{'date':'2026-10-24',**params},now=NOW)
    def test_read_only_dashboard_preserves_reported_submission_capacity(self):
        from local_app.dashboard import build_dashboard
        before=self.snapshot()
        result=build_dashboard(self.store,self.catalog,{},now=NOW,runtime={'capacity':50})
        self.assertEqual(result['runtime'].get('capacity'),50)
        self.assertEqual(self.snapshot(),before)
    def snapshot(self):
        return {name:json.dumps(list(self.db[name].find().sort('_id',1)),sort_keys=True,default=str)
                for name in ('campus_accounts','campus_runs','campus_access','campus_security','campus_audit')}
    def test_release_day_counts_keep_skips_uncertain_and_pending_separate(self):
        for status in ('success','failed','skipped','uncertain','preparing','submitting','queued'):
            self.seed(status,status)
        self.seed('manual',kind='manual');self.seed('legacy',kind=None);self.seed('yesterday',day='2026-10-23')
        result = self.dashboard()
        self.assertEqual(result['summary'],{'total':8,'success':2,'failed':1,'skipped':1,'uncertain':1,
            'pending':3,'closed':4,'success_rate':50.0})
        self.assertEqual(result['trend'][-1]['date'],'2026-10-24')
        self.assertEqual(result['trend'][-1]['total'],8)
        self.assertEqual(self.dashboard(kind='manual')['summary']['total'],1)
        self.assertEqual(self.dashboard(kind='all')['summary']['total'],9)
    def test_first_attempt_missing_cannot_be_replaced_by_a_retry(self):
        due = datetime(2026,10,24,7,tzinfo=ZONE).timestamp()
        self.seed('missing',attempts=[{}, {'timings':{'request_started_at':due+.1,'request_ms':20}}])
        self.seed('unsent',attempts=[{'sent':False,'timings':{'request_started_at':due+.2}}])
        self.seed('early',attempts=[{'sent':True,'timings':{'request_started_at':due-.01,'request_ms':12}}])
        result = self.dashboard()
        self.assertEqual(result['delay_count'],1);self.assertEqual(result['delay_missing'],2)
        self.assertAlmostEqual(result['delays'][0]['value_ms'],-10,places=2)
    def test_missing_and_nonfinite_durations_stay_missing_and_percentiles_name_samples(self):
        values = [None,float('nan'),float('inf'),-1,0,10,20]
        for i,value in enumerate(values):
            self.seed(str(i),attempts=[{'timings':{'request_ms':value}}])
        data = self.dashboard()['trend'][-1]['request']
        self.assertEqual(data,{'count':3,'p50':10,'p95':20})
        self.assertIsNone(self.dashboard()['trend'][0]['request']['p50'])
    def test_more_than_a_page_is_counted_and_read_does_not_mutate_any_data(self):
        for i in range(125):self.seed(str(i))
        before = self.snapshot()
        with patch.object(self.service,'client',side_effect=AssertionError('No school access')):
            result = self.dashboard()
        self.assertEqual(result['summary']['total'],125);self.assertEqual(self.snapshot(),before)
        self.assertNotIn('never-return-this',json.dumps(result))
    def test_valid_plan_preview_uses_actual_friday_and_other_accounts_survive_bad_plan(self):
        good = self.service.save_plan(self.account.id,{'seat_ids':['1'],'mode':'day','time_mode':'full_day',
            'day_offset':1,'trigger':'07:00:00','enabled':True},self.account.revision)
        from local_app.dashboard import build_dashboard
        data = build_dashboard(self.store,self.catalog,{},now=datetime(2026,10,22,6,tzinfo=ZONE))
        row = next(a for a in data['accounts'] if a['account_id']==good.id)
        self.assertEqual((row['next_run']['target_date'],row['next_run']['end']),('2026-10-23','20:00'))
        self.assertEqual(row['check'],'ready');self.assertEqual(data['population']['checked'],1)
        self.db.campus_accounts.update_one({'_id':good.id},{'$set':{'plan.trigger':'bad'}})
        self.assertEqual(self.dashboard()['population']['attention'],1)
        self.assertEqual(self.dashboard()['population']['authorized'],2)
    def test_no_tasks_is_empty_not_failure_and_parameters_are_strict(self):
        self.assertIsNone(self.dashboard()['summary']['success_rate'])
        for params in ({'date':'20261024'},{'date':'bad'},{'days':'15'},{'kind':'unknown'}):
            with self.subTest(params=params),self.assertRaises(AppError):self.dashboard(**params)
    def test_range_limit_refuses_partial_statistics(self):
        due = datetime(2026,10,24,7,tzinfo=ZONE).timestamp()
        self.db.campus_runs.insert_many([{'_id':str(i),'spec':{'due_at':due},'status':'success'} for i in range(5001)])
        with self.assertRaises(AppError) as context:self.dashboard()
        self.assertEqual(context.exception.code,'dashboard_limit')
    def test_query_timeout_has_an_explicit_recoverable_error(self):
        with patch.object(self.store.runs,'find',side_effect=ExecutionTimeout('fixture')):
            with self.assertRaises(AppError) as context:self.dashboard()
        self.assertEqual(context.exception.code,'dashboard_timeout')
    def test_bad_legacy_shape_and_infinite_derived_delay_cannot_break_the_dashboard(self):
        self.db.campus_accounts.update_one({'_id':self.account.id},{'$set':{'plan':'broken legacy plan'}})
        self.seed('overflow',attempts=[{'timings':{'request_started_at':1e308}}])
        result=self.dashboard()
        self.assertEqual(result['population']['attention'],1)
        self.assertEqual(result['delay_count'],0)
        self.assertNotIn('Infinity',json.dumps(result))
    def test_public_root_or_student_cannot_access_admin_statistics(self):
        origin='https://library-fixture.example:45581';relay='f'*64
        app=create_app(load_settings(ROOT,{'public_origin':origin,'relay_token':relay}),
            self.service,self.store,self.catalog,lambda:{},auth=self.auth)
        client=app.test_client()
        for role in ('root','student'):
            token=self.auth._issue(role,account_id=self.account.id,student_id='fixture01')
            client.set_cookie('njfu_autolib_session',token,domain='library-fixture.example')
            response=client.get('/api/local/admin/dashboard',base_url=origin,headers={
                'X-NJFU-Relay-Token':relay,'X-NJFU-Relay-HTTPS':'1','X-NJFU-Client-IP':'198.51.100.2'})
            self.assertEqual(response.status_code,403)
    def test_root_api_is_read_only_and_has_no_sensitive_fields(self):
        self.seed('a')
        response = self.client.get('/api/local/admin/dashboard?date=2026-10-24',base_url='http://127.0.0.1:5003')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('never-return-this',response.get_data(as_text=True))
        self.assertNotIn('secret',response.json['accounts'][0])
    def conflict_accounts(self):
        plan={'seat_ids':['1'],'mode':'week','week_times':{'1':['09:00','12:00']},
              'begin':'09:00','end':'12:00','trigger':'07:00:00','enabled':True}
        self.service.save_plan(self.account.id,plan,self.account.revision)
        other=self.service.verify_and_save('fixture02','pass','Other')
        self.service.save_plan(other.id,plan|{'week_times':{'1':['10:00','13:00']}},other.revision)
        return other,plan
    def test_conflict_report_uses_validated_future_plans_and_never_mutates_or_calls_school(self):
        other,_=self.conflict_accounts()
        self.store.accounts.update_one({'_id':self.account.id},{'$set':{
            'seat_preferences':{'favorites':[{'seat_id':'2','tags':['private']}],'groups':[]},
            'seat_preferences_revision':1}})
        before=self.snapshot()
        with patch.object(self.service,'client',side_effect=AssertionError('No school access')):
            data=self.dashboard(date='2026-10-01',kind='manual')
        self.assertIn('seat_conflicts',data)
        reminder=data['seat_conflicts']
        self.assertEqual((reminder['start_date'],reminder['end_date']),('2026-10-25','2026-10-31'))
        self.assertEqual(reminder['checked_accounts'],2)
        self.assertEqual(reminder['unchecked_accounts'],0)
        self.assertEqual(len(reminder['items']),1)
        self.assertEqual(reminder['items'][0]['target_date'],'2026-10-26')
        self.assertEqual({a['account_id'] for a in reminder['items'][0]['accounts']},{self.account.id,other.id})
        self.assertEqual(self.snapshot(),before)
        self.assertNotIn('private',json.dumps(reminder))
        self.assertNotIn('secret',json.dumps(reminder))
    def test_disabled_and_unauthorized_plans_do_not_participate_in_reminders(self):
        other,plan=self.conflict_accounts()
        current=self.store.get_account(other.id)
        self.service.save_plan(other.id,plan|{'enabled':False},current['revision'])
        self.assertIn('seat_conflicts',self.dashboard())
        self.assertEqual(self.dashboard()['seat_conflicts']['items'],[])
        self.assertEqual(self.dashboard()['seat_conflicts']['checked_accounts'],1)
        self.store.accounts.update_one({'_id':other.id},{'$set':{'plan.enabled':True}})
        self.db.campus_access.update_one({'_id':'fixture02'},{'$set':{'enabled':False}})
        reminder=self.dashboard()['seat_conflicts']
        self.assertEqual(reminder['items'],[])
        self.assertEqual(reminder['checked_accounts'],1)
    def test_bad_active_plan_is_explicitly_unchecked_without_hiding_other_collisions(self):
        self.conflict_accounts()
        broken=self.service.verify_and_save('fixture03','pass','Broken')
        grant(self.auth,['fixture03'])
        self.store.accounts.update_one({'_id':broken.id},{'$set':{'plan':{'enabled':True,'seat_ids':['missing']}}})
        self.assertIn('seat_conflicts',self.dashboard())
        reminder=self.dashboard()['seat_conflicts']
        self.assertEqual(reminder['checked_accounts'],2)
        self.assertEqual(reminder['unchecked_accounts'],1)
        self.assertEqual(len(reminder['items']),1)
    def test_conflict_report_uses_single_day_adjustments_and_preserves_prepared_runs(self):
        _,_=self.conflict_accounts()
        self.seed('prepared','preparing')
        self.store.accounts.update_one({'_id':self.account.id},{'$set':{'date_exceptions':{'2026-10-26':{'action':'skip'}}}})
        before=self.snapshot()
        self.assertIn('seat_conflicts',self.dashboard())
        self.assertEqual(self.dashboard()['seat_conflicts']['items'],[])
        self.assertEqual(self.snapshot(),before)
    def test_conflict_details_are_available_only_to_local_root(self):
        self.conflict_accounts()
        response=self.client.get('/api/local/admin/dashboard',base_url='http://127.0.0.1:5003')
        self.assertEqual(response.status_code,200)
        self.assertIn('seat_conflicts',response.json)
        self.client.set_cookie('njfu_autolib_session',self.auth._issue('student',student_id='fixture01',account_id=self.account.id),domain='127.0.0.1')
        denied=self.client.get('/api/local/admin/dashboard',base_url='http://127.0.0.1:5003')
        self.assertEqual(denied.status_code,403)
        self.assertNotIn('seat_conflicts',denied.json)
    def test_anonymous_401_cannot_delete_a_newer_cookie_and_student_is_denied(self):
        self.client.delete_cookie('njfu_autolib_session',domain='127.0.0.1')
        response = self.client.get('/api/local/admin/dashboard',base_url='http://127.0.0.1:5003')
        self.assertEqual(response.status_code,401);self.assertNotIn('Set-Cookie',response.headers)
        self.client.set_cookie('njfu_autolib_session',self.auth._issue('student',student_id='fixture01',account_id=self.account.id),domain='127.0.0.1')
        self.assertEqual(self.client.get('/api/local/admin/dashboard',base_url='http://127.0.0.1:5003').status_code,403)
