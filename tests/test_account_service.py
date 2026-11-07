import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        from support.account_fixture import services
        self.db=self.mongo.database();self.temp,self.store,self.service,self.catalog=services(self.db)
    def tearDown(self):
        self.service.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)

    def test_accounts_queries_and_failed_password_update(self):
        from local_app.models import AppError
        a=self.service.verify_and_save('fixture01','pass','Alice',None)
        b=self.service.verify_and_save('fixture02','pass','Bob',None)
        self.service.query(a.id);self.service.query(b.id)
        self.assertEqual(self.store.get_account(a.id)['snapshot'][0]['student_id'],'fixture01')
        self.assertEqual(self.store.get_account(b.id)['snapshot'][0]['student_id'],'fixture02')
        before=self.store.get_account(a.id)['secret']
        with self.assertRaises(AppError):self.service.verify_and_save('fixture01','wrong','Alice',a.revision)
        self.assertEqual(before,self.store.get_account(a.id)['secret'])
        self.assertNotIn('password',str(self.store.list_accounts()))

    def test_blank_password_preserves_credentials_and_stale_plan_rejected(self):
        from local_app.models import AppError
        a=self.service.verify_and_save('fixture01','pass','Alice',None)
        self.service.verify_and_save('fixture01','','Alice',a.revision)
        self.assertEqual(len(self.store.list_accounts()),1)
        plan=dict(seat_ids=['1'],day_offset=1,mode='day',begin='09:00',end='12:00',trigger='20:00:00',enabled=True)
        fresh=self.store.list_accounts()[0]
        updated=self.service.save_plan(a.id,plan,fresh.revision)
        with self.assertRaises(AppError):self.service.save_plan(a.id,plan,fresh.revision)
        self.assertEqual(self.store.get_account(a.id)['revision'],updated.revision)
