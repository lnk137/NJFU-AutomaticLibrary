"""Runtime coexistence; every dependency uses isolated storage and a fake school."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.config import load_settings
from support.account_fixture import School
from support.auth_fixture import root_session
from support.mongo_fixture import MongoFixture


class RecordingPower:
    def __init__(self,*args,**kwargs):self.started=False;self.closed=False
    def start(self):self.started=True
    def close(self):self.closed=True
    def status(self):return {'integration_fixture':True}


class FeatureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()

    def test_form_and_power_lifecycle_and_root_routes_coexist(self):
        from local_app.runtime import create_runtime
        database=self.mongo.database()
        with tempfile.TemporaryDirectory(prefix='njfu-integration-') as data:
            settings=load_settings(Path(__file__).resolve().parents[1],{
                'data_dir':Path(data),'mongo_uri':self.mongo.uri,'database_name':database.name})
            with patch('local_app.runtime.PowerService',RecordingPower,create=True):
                runtime=create_runtime(settings,client_factory=School)
                try:
                    self.assertIsNotNone(runtime.forms)
                    self.assertIsNotNone(getattr(runtime,'power',None))
                    runtime.start();self.assertTrue(runtime.power.started)
                    client=runtime.app.test_client();root_session(runtime.auth,client)
                    origin='http://127.0.0.1:5003'
                    self.assertEqual(client.get('/api/local/admin/form-imports',base_url=origin).json,{'items':[]})
                    self.assertEqual(client.get('/api/local/admin/power',base_url=origin).json,{'integration_fixture':True})
                    template=client.get('/api/local/admin/form-template',base_url=origin)
                    self.assertEqual(template.status_code,200);self.assertIn('personal',template.headers['Content-Disposition'])
                    runtime.auth.logout(client.get_cookie('njfu_autolib_session',domain='127.0.0.1').value)
                    for path in ['/api/local/admin/form-imports','/api/local/admin/power']:
                        self.assertEqual(client.get(path,base_url=origin).status_code,401)
                finally:runtime.close()
                self.assertTrue(runtime.power.closed)
                self.assertEqual(database.campus_accounts.count_documents({}),0)
                self.assertEqual(database.campus_runs.count_documents({}),0)
        self.mongo.client.drop_database(database.name)
