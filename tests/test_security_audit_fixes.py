"""Security regressions with task-owned Mongo/SQLite and school/HTTP doubles only."""
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.auth import AuthService
from local_app.inbox import InboxEngine
from local_app.models import AppError, ZONE
from deploy.inbox_gateway import create_gateway, Reply, Upstream
from deploy.inbox_store import COOKIE, InboxStore
from support.account_fixture import School, services
from support.auth_fixture import grant
from support.mongo_fixture import MongoFixture
from test_inbox_gateway import snapshot

STUDENT = '2310000001'
PLAN = dict(seat_ids=['1'], mode='day', time_mode='custom', day_offset=1,
            begin='09:00', end='12:00', trigger='07:00:00', enabled=True)
SEGMENTED = PLAN | {'segments': [
    dict(id='morning', begin='09:00', end='12:00', seat_ids=['1']),
    dict(id='afternoon', begin='13:00', end='16:00', seat_ids=['2'])]}


class AccountSecurityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, self.catalog = services(self.db)
        self.auth = AuthService(self.store, self.accounts)
        self.accounts.is_allowed = self.auth.is_allowed
        grant(self.auth, [STUDENT])
        self.auth.security.insert_one({'_id': 'root', 'password_hash': 'fixture-unused'})
        self.account = self.accounts.verify_and_save(STUDENT, 'pass', 'Fixture')
        self.now = datetime(2026, 10, 24, 17, tzinfo=ZONE).timestamp()
        self.accounts.wall_clock = lambda: self.now
        self.engine = InboxEngine(self.accounts, self.store, self.auth, clock=lambda: self.now)
        self.packet = self.engine.snapshot({})
        self.accounts.auth_observer = self.engine.note_auth

    def tearDown(self):
        self.accounts.close(); self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def job(self, plan=None):
        row = self.store.get_account(self.account.id)
        item = self.engine.snapshot({})['accounts'][0]
        return {'request_id': str(uuid.uuid4()), 'student_id': STUDENT, 'version': 1,
                'generation': item['auth']['generation'], 'expected_revision': row['revision'],
                'plan': deepcopy(plan or PLAN), 'created_at': self.now - 1,
                'expires_at': self.now + 86400, 'attempt': 1}

    def test_anonymous_wrong_password_preserves_offline_auth_and_pending_generation(self):
        before = self.engine.verifiers.find_one({'_id': self.account.id})
        with self.assertRaises(AppError) as error:
            self.auth.login({'kind': 'student', 'username': STUDENT, 'password': 'wrong'}, 'fixture')
        self.assertEqual(error.exception.code, 'login_failed')
        self.assertFalse(getattr(error.exception, 'saved_credential_rejected', False))
        self.assertEqual(self.engine.verifiers.find_one({'_id': self.account.id}), before)
        self.assertEqual(self.engine.apply(self.job())['state'], 'applied')

    def test_saved_password_rejection_still_blocks_offline_auth_with_trusted_marker(self):
        with patch.object(School, 'login', side_effect=AppError('login_failed', 'fixture rejection', 401)):
            with self.assertRaises(AppError) as error:
                self.auth.login({'kind': 'student', 'username': STUDENT, 'password': 'pass'}, 'fixture')
        self.assertIs(getattr(error.exception, 'saved_credential_rejected', False), True)
        self.assertEqual(self.engine.verifiers.find_one({'_id': self.account.id})['blocked'], 'login_failed')

    def test_saved_client_rejection_still_blocks_offline_auth(self):
        with patch.object(School, 'login', side_effect=AppError('verification_required', 'fixture rejection', 401)):
            with self.assertRaises(AppError): self.accounts.client(self.account.id, force=True)
        self.assertEqual(self.engine.verifiers.find_one({'_id': self.account.id})['blocked'], 'verification_required')

    def test_password_rotation_revokes_old_student_session_but_same_password_does_not(self):
        old = self.auth._issue('student', account_id=self.account.id, student_id=STUDENT)
        same = self.accounts.verify_and_save(STUDENT, 'pass', 'Fixture', self.account.revision)
        self.assertIsNotNone(self.auth.current(old))
        self.accounts.verify_and_save(STUDENT, 'changed-pass', 'Fixture', same.revision)
        self.assertIsNone(self.auth.current(old))
        token, user = self.auth.login({'kind': 'student', 'username': STUDENT, 'password': 'changed-pass'}, 'fixture')
        self.assertEqual(user['student_id'], STUDENT)
        self.assertIsNotNone(self.auth.current(token))

    def test_legacy_unversioned_student_session_requires_login(self):
        token = self.auth._issue('student', account_id=self.account.id, student_id=STUDENT)
        self.auth.sessions.update_one({'_id': hashlib.sha256(token.encode()).hexdigest()},
                                     {'$unset': {'credential_revision': ''}})
        self.assertIsNone(self.auth.current(token))

    def test_offline_managed_segmented_plan_is_rejected_without_plan_or_marker_write(self):
        gate = Mock(); gate.is_managed.return_value = True
        self.accounts.managed_gate = gate
        before = self.store.get_account(self.account.id)
        result = self.engine.apply(self.job(SEGMENTED))
        self.assertEqual(result.get('code'), 'dual_segmented_unsupported')
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_snapshot_exports_managed_capabilities_and_fails_closed(self):
        gate = Mock(); self.accounts.managed_gate = gate
        for observed, managed in ((True, True), (False, False), (None, True), (RuntimeError('fixture'), True)):
            with self.subTest(observed=type(observed).__name__):
                gate.is_managed.side_effect = observed if isinstance(observed, Exception) else None
                gate.is_managed.return_value = observed
                account = self.engine.snapshot({})['accounts'][0]['account']
                self.assertEqual(account.get('capabilities'), {
                    'execution_mode': 'dual_node' if managed else 'local',
                    'date_override_segments': not managed,
                    'credential_sync_state': 'unknown' if managed else 'not_required'})

    def test_offline_segmented_plan_respects_the_same_preparation_window(self):
        # The proposed trigger is not in the old snapshot's quiet-window list.
        self.now = datetime(2026, 10, 24, 16, 59, 58, tzinfo=ZONE).timestamp()
        before = self.store.get_account(self.account.id)
        result = self.engine.apply(self.job(SEGMENTED | {'trigger': '17:00:00'}))
        self.assertEqual(result.get('code'), 'account_busy')
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_offline_valid_plan_commit_and_recovery_preserve_atomic_marker(self):
        job = self.job()
        first = self.engine.apply(job)
        self.assertEqual(first['state'], 'applied')
        row = self.store.get_account(self.account.id)
        self.assertEqual(row['last_inbox']['revision'], row['revision'])
        self.assertEqual(row['last_inbox']['request_id'], job['request_id'])
        self.engine.receipts.delete_one({'_id': job['request_id']})
        self.accounts.save_plan(self.account.id, PLAN | {'seat_ids': ['2']}, row['revision'])
        self.assertEqual(self.engine.apply(job), first)
        self.assertEqual(self.store.get_account(self.account.id)['plan']['seat_ids'], ['2'])


class GatewaySecurityAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-security-gateway-')
        self.store = InboxStore(Path(self.temp.name) / 'private' / 'inbox.sqlite')
        self.packet = snapshot()
        self.packet['accounts'][0]['runs'] = {'items': [
            {'id': 'new', 'account_id': 'account-a', 'target_date': '2026-10-26', 'status': 'success'},
            {'id': 'middle', 'account_id': 'account-a', 'target_date': '2026-10-25', 'status': 'failed'},
            {'id': 'old', 'account_id': 'account-a', 'target_date': '2026-10-25', 'status': 'success'}],
            'counts': {'total': 101, 'success': 70, 'failed': 31}, 'next_cursor': 'upstream-only-cursor'}
        self.store.publish(self.packet)
        self.upstream = Mock(); self.upstream.probe.return_value = False
        self.origin = 'https://inbox-fixture.example:45581'
        self.config = {'origin': self.origin, 'relay_token': 'r' * 64, 'worker_token': 'w' * 64,
                       'static_root': self.temp.name}
        self.client = create_gateway(self.config, self.store, self.upstream).test_client()
        self.headers = {'X-NJFU-Relay-HTTPS': '1', 'X-NJFU-Relay-Token': 'r' * 64,
                        'X-NJFU-Client-IP': '198.51.100.2'}
        self.csrf = self.get('/bootstrap').json['csrf']
        self.assertEqual(self.post('/auth/login', {'kind': 'student', 'username': STUDENT,
                                                  'password': 'fixture-password'}).status_code, 200)

    def tearDown(self): self.temp.cleanup()
    def get(self, path): return self.client.get('/api/local' + path, base_url=self.origin, headers=self.headers)
    def post(self, path, data):
        return self.client.post('/api/local' + path, json=data, base_url=self.origin,
                                headers=self.headers | {'Origin': self.origin, 'X-CSRF-Token': self.csrf})

    def test_untrusted_failed_login_does_not_revoke_an_existing_offline_session(self):
        token = self.client.get_cookie(COOKIE, domain='inbox-fixture.example').value
        self.upstream.probe.return_value = True
        self.upstream.forward.return_value = Reply(401, b'{"code":"login_failed"}')
        self.assertEqual(self.post('/auth/login', {'kind': 'student', 'username': STUDENT, 'password': 'wrong'}).status_code, 401)
        self.assertIsNotNone(self.store.current(token))

    def test_trusted_saved_credential_rejection_revokes_offline_session(self):
        self.upstream.probe.return_value = True
        reply = Reply(401, b'{"code":"login_failed"}')
        reply.saved_credential_rejected = True
        self.upstream.forward.return_value = reply
        self.post('/auth/login', {'kind': 'student', 'username': STUDENT, 'password': 'fixture-password'})
        self.assertIsNone(self.store.current(self.client.get_cookie(COOKIE, domain='inbox-fixture.example').value))

    def test_logout_clears_browser_cookies_when_upstream_probe_fails(self):
        self.upstream.probe.side_effect = AppError('executor_unavailable', 'fixture timeout', 503)
        response = self.post('/auth/logout', {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok': True, 'upstream_revoked': False})
        self.assertIsNone(self.client.get_cookie(COOKIE, domain='inbox-fixture.example'))
        self.assertTrue(any(value.startswith('njfu_autolib_session=;') for value in response.headers.getlist('Set-Cookie')))

    def test_logout_clears_browser_cookies_when_upstream_returns_failure(self):
        self.upstream.probe.return_value = True
        self.upstream.forward.return_value = Reply(503, b'{"code":"service_error"}')
        response = self.post('/auth/logout', {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['upstream_revoked'])
        self.assertIsNone(self.client.get_cookie(COOKIE, domain='inbox-fixture.example'))

    def test_saved_rejection_marker_is_carried_internally_without_being_relayed(self):
        upstream = Upstream(self.origin, 'r' * 64); upstream.csrf = 'fixture'
        response = Mock(status_code=401, content=b'{"code":"login_failed"}',
                        headers={'X-NJFU-Saved-Credential-Rejected': '1'})
        response.raw.headers.getlist.return_value = []
        upstream._send = Mock(return_value=response)
        reply = upstream.forward('POST', '/api/local/auth/login', '198.51.100.2', data={})
        self.assertIs(reply.saved_credential_rejected, True)
        self.upstream.probe.return_value = True; self.upstream.forward.return_value = reply
        result = self.post('/auth/login', {'kind': 'student', 'username': STUDENT, 'password': 'fixture-password'})
        self.assertNotIn('X-NJFU-Saved-Credential-Rejected', result.headers)

    def test_logout_reports_upstream_confirmation_only_after_success(self):
        self.upstream.probe.return_value = True
        self.upstream.forward.return_value = Reply(200, b'{"ok":true}')
        self.assertEqual(self.post('/auth/logout', {}).json, {'ok': True, 'upstream_revoked': True})

    def test_cached_history_filters_and_pages_without_repeating_or_claiming_completeness(self):
        first = self.get('/runs?date=2026-10-25&limit=1').json
        self.assertEqual([row['id'] for row in first['items']], ['middle'])
        self.assertEqual(first['counts'], {'total': 2, 'failed': 1, 'success': 1})
        self.assertTrue(first['cache_only']); self.assertTrue(first['cache_truncated'])
        self.assertEqual(first['cached_total'], 3)
        self.assertNotEqual(first['next_cursor'], 'upstream-only-cursor')
        second = self.get('/runs?date=2026-10-25&limit=1&cursor=' + first['next_cursor']).json
        self.assertEqual([row['id'] for row in second['items']], ['old'])
        self.assertIsNone(second['next_cursor'])

    def test_cached_history_rejects_stale_cursor_and_other_account_filter(self):
        first = self.get('/runs?limit=1').json
        newer = deepcopy(self.packet); newer['generation'] = 2
        newer['accounts'][0]['runs']['items'].pop()
        self.store.publish(newer)
        self.assertEqual(self.get('/runs?limit=1&cursor=' + first['next_cursor']).json.get('code'), 'history_cache_changed')
        self.assertEqual(self.get('/runs?account_id=another-account').status_code, 404)

    def test_cached_history_rejects_invalid_filters_and_foreign_cursor(self):
        for query in ('date=bad', 'limit=0', 'limit=101', 'cursor=upstream-only-cursor'):
            with self.subTest(query=query):
                self.assertEqual(self.get('/runs?' + query).status_code, 400)


if __name__ == '__main__': unittest.main()
