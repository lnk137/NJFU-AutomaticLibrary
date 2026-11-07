"""Managed authentication protection defers queued edits without recording failure."""
from datetime import datetime
import unittest
from unittest.mock import Mock
import uuid

from local_app.auth import AuthService
from local_app.inbox import InboxEngine
from local_app.models import AppError, ZONE
from support.account_fixture import services
from support.auth_fixture import grant
from support.mongo_fixture import MongoFixture

PLAN = dict(seat_ids=['1'], mode='day', time_mode='custom', day_offset=1,
            begin='09:00', end='12:00', trigger='07:00:00', enabled=True)


class InboxAuthWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.auth = AuthService(self.store, self.accounts)
        grant(self.auth, ['2310000001'])
        self.account = self.accounts.verify_and_save('2310000001', 'fixture-password')
        self.due = datetime(2026, 11, 8, 7, tzinfo=ZONE).timestamp()
        self.now = self.due - 120
        self.accounts.wall_clock = lambda: self.now
        self.engine = InboxEngine(self.accounts, self.store, self.auth, clock=lambda: self.now)
        packet = self.engine.snapshot({})
        self.job = dict(request_id=str(uuid.uuid4()), student_id=self.account.student_id,
                        version=1, expected_revision=self.account.revision, plan=dict(PLAN),
                        generation=packet['accounts'][0]['auth']['generation'],
                        created_at=self.now-1, expires_at=self.due+86400, attempt=1)
        self.gate = Mock(); self.gate.is_managed.return_value = True
        self.accounts.managed_gate = self.gate

    def tearDown(self):
        self.accounts.close(); self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def test_preparation_auth_window_defers_then_applies_the_same_receipt_once(self):
        def require_auth(_account):
            if self.due-183 <= self.now <= self.due+40:
                raise AppError('dual_auth_window', 'fixture guard', 409)
        self.gate.require_auth.side_effect = require_auth
        before = self.store.get_account(self.account.id)
        first = self.engine.apply(self.job)
        self.assertEqual(first['state'], 'deferred')
        self.assertIsNone(self.engine.receipts.find_one({'_id': self.job['request_id']}))
        self.assertEqual(self.store.get_account(self.account.id), before)
        self.now = self.due + 45
        second = self.engine.apply(self.job)
        self.assertEqual(second['state'], 'applied')
        self.assertEqual(self.engine.apply(self.job), second)
        self.assertEqual(self.store.get_account(self.account.id)['revision'], before['revision']+1)

    def test_unconfirmed_auth_window_defers_even_after_transport_attempt_limit(self):
        self.gate.require_auth.side_effect = AppError('dual_auth_window_unavailable', 'fixture guard', 409)
        self.job['attempt'] = 12
        result = self.engine.apply(self.job)
        self.assertEqual(result['state'], 'deferred')
        self.assertIsNone(self.engine.receipts.find_one({'_id': self.job['request_id']}))
        self.assertIsNone(self.store.get_account(self.account.id)['plan'])

    def test_expired_edit_is_terminal_even_while_auth_window_is_blocked(self):
        self.gate.require_auth.side_effect = AppError('dual_auth_window', 'fixture guard', 409)
        self.job['expires_at'] = self.now-1
        result = self.engine.apply(self.job)
        self.assertEqual((result['state'], result['code']), ('failed', 'inbox_expired'))
        self.gate.require_auth.assert_not_called()


if __name__ == '__main__': unittest.main()
