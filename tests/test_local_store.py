import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        from local_app.store import LocalStore
        self.db=self.mongo.database();self.store=LocalStore(self.db)
    def tearDown(self):self.mongo.client.drop_database(self.db.name)

    def seed(self):
        from local_app.models import Identity
        return self.store.save_verified_account('a',Identity('fixture01','A',1),'Alice',{'ciphertext':'encrypted'},None)

    def test_accounts_and_revisions_are_isolated(self):
        from local_app.models import Identity,Plan,AppError
        a=self.seed();self.store.save_verified_account('b',Identity('fixture02','B',2),'Bob',{'ciphertext':'b'},None)
        p=Plan(('1',),1,'day','09:00','12:00',{},'20:00:00',True)
        updated=self.store.save_plan('a',p,a.revision)
        self.assertEqual(updated.revision,2);self.assertIsNone(self.store.get_account('b')['plan'])
        with self.assertRaises(AppError):self.store.save_plan('a',p,1)
        self.assertNotIn('secret',str(self.store.list_accounts()))

    def test_duplicate_attempt_and_interrupted_recovery(self):
        from local_app.models import RunSpec
        self.seed();r=RunSpec('a',1,'2026-10-20','09:00','12:00',('1',),123)
        self.store.ensure_run(r);self.store.ensure_run(r)
        self.assertEqual(self.db.campus_runs.count_documents({}),1)
        self.assertTrue(self.store.transition_run(r.key,('queued',),'submitting',{}))
        self.assertFalse(self.store.transition_run(r.key,('queued',),'submitting',{}))
        self.store.recover_interrupted()
        self.assertEqual(self.store.get_run(r.key)['status'],'uncertain')

    def test_history_pagination_counts_all_filtered_records(self):
        from local_app.models import RunSpec
        from datetime import date,timedelta
        self.seed()
        for n in range(101):self.store.ensure_run(RunSpec('a',1,(date(2026,1,1)+timedelta(days=n)).isoformat(),'09:00','12:00',('1',),n))
        first=self.store.list_runs(None,None)
        self.assertEqual(len(first.items),100);self.assertEqual(first.counts['total'],101)
        second=self.store.list_runs(None,None,cursor=first.next_cursor)
        self.assertEqual(len(second.items),1);self.assertEqual(second.counts['total'],101)

    def test_failed_query_does_not_erase_snapshot(self):
        self.seed();self.store.save_snapshot('a',1,[], '2026-10-19T12:00:00+08:00')
        self.store.query_failed('a',1,'network_error')
        row=self.store.get_account('a');self.assertEqual(row['snapshot_at'],'2026-10-19T12:00:00+08:00')
        self.assertEqual(row['query_error'],'network_error')
