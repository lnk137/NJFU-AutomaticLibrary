import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.auth_fixture import root_session
from local_app.auth import AuthService


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        from support.account_fixture import services
        from local_app.config import load_settings
        from local_app.web import create_app
        self.db=self.mongo.database();self.temp,self.store,self.service,self.catalog=services(self.db)
        self.auth=AuthService(self.store,self.service)
        self.app=create_app(load_settings(Path(__file__).resolve().parents[1]),self.service,self.store,self.catalog,lambda:{'allow_live':False},auth=self.auth)
        self.client=self.app.test_client();root_session(self.auth,self.client);self.origin='http://127.0.0.1:5003'
        r=self.client.get('/api/local/bootstrap',base_url=self.origin)
        self.token=r.json['csrf']
    def tearDown(self):
        self.service.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def post(self,path,data,**changes):
        headers={'Origin':self.origin,'X-CSRF-Token':self.token}|changes
        return self.client.post(path,json=data,headers=headers,base_url=self.origin)

    def test_host_origin_csrf_and_json_validation(self):
        path='/api/local/accounts/verify';data={'student_id':'fixture01','password':'pass','alias':'Alice'}
        self.assertEqual(self.client.get('/api/local/accounts',base_url='http://evil.example').status_code,403)
        self.assertEqual(self.post(path,data,Origin='http://evil.example').status_code,403)
        self.assertEqual(self.post(path,data,**{'X-CSRF-Token':''}).status_code,403)
        self.assertEqual(self.post(path,[]).status_code,400)
        self.assertEqual(self.post(path,{'password':'x'*70000}).status_code,413)

    def test_school_policy_is_read_only_and_shared_with_frontend(self):
        response=self.client.get('/api/local/policy',base_url=self.origin)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['booking_open'],'07:00:00')
        self.assertEqual(self.post('/api/local/policy',{}).status_code,404)

    def test_accounts_and_disabled_legacy_routes(self):
        r=self.post('/api/local/accounts/verify',{'student_id':'fixture01','password':'pass','alias':'Alice'})
        self.assertEqual(r.status_code,200)
        self.assertNotIn('secret',str(r.json));self.assertNotIn('pass',str(r.json))
        for path in ('/db/reservation/all','/api/local/actions/reserve','/reserve','/db/reservation/delete'):
            self.assertEqual(self.post(path,{}).status_code,404)
        self.assertEqual(self.post('/api/local/accounts/missing/query',{}).status_code,404)
        self.assertEqual(self.client.get('/api/local/runs?limit=-1',base_url=self.origin).status_code,400)

    def test_read_only_reconcile_queries_the_run_date_with_live_gate_closed(self):
        from local_app.config import load_settings
        from local_app.models import RunSpec
        from local_app.reservations import ReservationService
        from local_app.web import create_app
        account=self.service.verify_and_save('fixture01','pass','Alice')
        run=RunSpec(account.id,account.revision,'2026-10-01','09:00','12:00',('1',),1.0)
        self.store.ensure_run(run)
        self.store.transition_run(run.key,('queued',),'uncertain',{'attempted_seat_id':'1'})
        self.service.client(account.id).active_statuses={1027}
        reservations=ReservationService(self.service,self.store,False)
        app=create_app(load_settings(Path(__file__).resolve().parents[1]),self.service,
            self.store,self.catalog,lambda:{'allow_live':False},reconcile=reservations.reconcile,auth=self.auth)
        self.client=app.test_client();root_session(self.auth,self.client)
        self.token=self.client.get('/api/local/bootstrap',base_url=self.origin).json['csrf']
        response=self.post('/api/local/runs/'+run.key+'/reconcile',{})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['result']['status'],'success')
        self.assertEqual(self.store.get_run(run.key)['status'],'success')
