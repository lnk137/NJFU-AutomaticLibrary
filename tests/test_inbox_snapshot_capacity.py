"""Snapshot capacity regressions use private Mongo/SQLite and fake school clients."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import requests

from local_app import inbox
from local_app.auth import AuthService
from local_app.models import AppError, RunPage
from deploy.inbox_store import InboxStore
from support.account_fixture import services
from support.auth_fixture import grant
from support.mongo_fixture import MongoFixture
from test_inbox_worker_recovery import fixture


class SnapshotCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.auth = AuthService(self.store, self.accounts)
        grant(self.auth, ['2310000001', '2310000002'])
        self.saved = [self.accounts.verify_and_save(student, 'fixture-password')
                      for student in ('2310000001', '2310000002')]
        self.engine = inbox.InboxEngine(self.accounts, self.store, self.auth)
        self.engine.snapshot({})

    def tearDown(self):
        self.accounts.close(); self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def history(self, account_id, *_args):
        return RunPage([{'id': str(index), 'account_id': account_id, 'status': 'failed',
                         'target_date': '2026-11-07', 'message': '席' * 2400}
                        for index in range(100)], None, {'total': 100, 'failed': 100})

    def test_large_histories_fit_transport_and_keep_every_account_and_auth(self):
        before = list(self.store.accounts.find().sort('_id', 1))
        with patch.object(self.store, 'list_runs', side_effect=self.history):
            packet = self.engine.snapshot({})
        body = requests.Request('POST', 'https://fixture.invalid/_inbox/snapshot', json=packet).prepare().body
        self.assertLessEqual(len(body), 2 * 1024 * 1024)
        self.assertEqual({row['student_id'] for row in packet['accounts']},
                         {account.student_id for account in self.saved})
        self.assertTrue(all(row['auth'] and row['account'] for row in packet['accounts']))
        self.assertTrue(all(0 < len(row['runs']['items']) < 100 for row in packet['accounts']))
        self.assertEqual(list(self.store.accounts.find().sort('_id', 1)), before)
        with tempfile.TemporaryDirectory(prefix='njfu-capacity-') as directory:
            cloud = InboxStore(Path(directory) / 'private/inbox.sqlite')
            cloud.publish(packet)
            for account in self.saved:
                cloud.issue(cloud.authenticate(account.student_id, 'fixture-password', 'fixture'))
                history = cloud.cached_runs(account.student_id, {})
                self.assertTrue(history['cache_truncated'])
                self.assertLess(history['cached_total'], 100)

    def test_omitted_receipt_cache_cannot_look_like_a_successful_empty_school_query(self):
        records = {'records': [{'id': str(i), 'message': 'fixture receipt' * 100} for i in range(20)],
                   'checked_at': '2026-11-07T14:00:00+08:00', 'revision': 1}
        original = deepcopy(records)
        with patch.object(self.accounts, 'snapshot', return_value=records), \
                patch.object(inbox, 'SNAPSHOT_MAX_BYTES', 4000):
            packet = self.engine.snapshot({})
        self.assertLessEqual(len(json.dumps(packet).encode()), 4000)
        for row in packet['accounts']:
            self.assertEqual(row['records']['records'], [])
            self.assertIsNone(row['records']['checked_at'])
            self.assertEqual(row['records']['error'], 'cache_capacity')
            self.assertTrue(row['records']['cache_truncated'])
            self.assertTrue(row['auth'])
        self.assertEqual(records, original)

    def test_capacity_never_publishes_only_a_subset_of_account_authorizations(self):
        before = deepcopy(list(self.store.accounts.find()))
        with patch.object(inbox, 'SNAPSHOT_MAX_BYTES', 256, create=True):
            with self.assertRaises(AppError) as error:
                self.engine.snapshot({})
        self.assertEqual(error.exception.code, 'inbox_snapshot_capacity')
        self.assertEqual(list(self.store.accounts.find()), before)

    def test_413_reexports_current_state_while_polling_continues(self):
        error = AppError('inbox_connection', 'fixture-only', 503)
        error.inbox_http_status = 413
        result = fixture(duration=22, failure=error)
        self.assertGreaterEqual(result.worker.engine.sequence, 2)
        self.assertGreaterEqual(len(result.polls), 5)
        self.assertTrue(all(call['budget'] <= 3 for call in result.calls))

    def test_ambiguous_server_error_still_retries_original_generation(self):
        error = AppError('inbox_connection', 'fixture-only', 503)
        error.inbox_http_status = 500
        result = fixture(duration=12, failure=error)
        self.assertEqual(result.worker.engine.sequence, 1)
        self.assertGreaterEqual(len(result.polls), 5)


if __name__ == '__main__': unittest.main()
