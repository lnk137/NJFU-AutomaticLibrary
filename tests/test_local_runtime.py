import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.account_fixture import School
from support.auth_fixture import grant


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()

    def test_single_runtime_preserves_accounts_and_missing_key(self):
        from local_app.runtime import create_runtime
        from local_app.config import load_settings
        from local_app.models import AppError
        with tempfile.TemporaryDirectory() as d:
            db=self.mongo.database()
            settings=load_settings(Path(__file__).resolve().parents[1],{'data_dir':Path(d),'mongo_uri':self.mongo.uri,'database_name':db.name})
            first=create_runtime(settings,client_factory=School)
            try:
                first.start();self.assertTrue(first.scheduler.status()['scheduler_running'])
                with self.assertRaises(AppError):create_runtime(settings,client_factory=School)
                grant(first.auth,['fixture01'])
                a=first.accounts.verify_and_save('fixture01','pass','Alice',None)
                self.assertFalse(first.reservations.allow_live)
            finally:first.close()
            second=create_runtime(settings,client_factory=School)
            self.assertEqual(second.store.list_accounts()[0].id,a.id)
            second.close()
            (Path(d)/'credentials/key').unlink()
            with self.assertRaises(AppError):create_runtime(settings,client_factory=School)
            self.assertEqual(db.campus_accounts.count_documents({}),1)
            self.assertFalse((Path(d)/'credentials/key').exists())

    def test_partial_startup_failure_releases_lock_and_closes_database(self):
        from unittest.mock import patch
        from local_app.runtime import create_runtime
        from local_app.config import load_settings
        from local_app.models import AppError
        from pymongo import MongoClient
        opened=[]
        def mongo_client(*args,**kwargs):
            client=MongoClient(*args,**kwargs);opened.append(client);return client
        with tempfile.TemporaryDirectory() as d:
            settings=load_settings(Path(__file__).resolve().parents[1],{'data_dir':Path(d),
                'mongo_uri':self.mongo.uri,'database_name':self.mongo.database().name})
            with patch('local_app.runtime.MongoClient',mongo_client),patch('local_app.runtime.load_catalog',
                    side_effect=AppError('fixture_failure','Synthetic startup failure')):
                with self.assertRaises(AppError):create_runtime(settings,client_factory=School)
            from pymongo.errors import InvalidOperation
            with self.assertRaises(InvalidOperation):opened[0].admin.command('ping')
            recovered=create_runtime(settings,client_factory=School)
            try:self.assertFalse(recovered.reservations.allow_live)
            finally:recovered.close()
