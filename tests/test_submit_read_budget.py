"""Submission read budget and retained safety, using real isolated MongoDB."""
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import urlsplit

from Crypto.PublicKey import RSA
from pymongo import MongoClient, monitoring
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.auth import AuthService
from local_app.models import AppError, Identity, RunSpec
from local_app.reservations import ReservationService
from local_app.school import CampusClient
from support.account_fixture import services
from support.mongo_fixture import MongoFixture
from test_booking_protocol import BookingTransport


class Commands(monitoring.CommandListener):
    def __init__(self):
        self.active = False
        self.names = []

    def started(self, event):
        if self.active:
            self.names.append(event.command_name)

    def succeeded(self, event): pass
    def failed(self, event): pass


class SubmitReadBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.addClassCleanup(cls.mongo.close)
        cls.key = base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()

    def setUp(self):
        self.db = self.mongo.database()
        self.commands = Commands()
        self.connection = MongoClient(self.mongo.uri, event_listeners=[self.commands])
        self.temp, self.store, self.accounts, _ = services(self.connection[self.db.name])
        self.auth = AuthService(self.store)
        self.auth.access.insert_one({'_id': 'fixture01', 'enabled': True})
        self.accounts.is_allowed = self.auth.is_allowed
        self.posts, self.logins = [], []
        self.before_guard = lambda: None
        owner = self

        class Transport(BookingTransport):
            def request(transport, method, url, **kwargs):
                if method == 'POST':
                    owner.commands.active = False
                    # A separate connection observes the actual persisted claim.
                    row = owner.db.campus_runs.find_one({'_id': owner.run.key})
                    owner.posts.append({'commands': list(owner.commands.names), 'row': row})
                    transport.body = kwargs['json']
                if urlsplit(url).path.endswith('/reserve/resvInfo'):
                    response = requests.Response()
                    response.status_code, response.url = 200, url
                    response._content = json.dumps({'code': 0, 'data': [{'uuid': 'fixture',
                        'logonName': 'fixture01', 'resvDevInfoList': [{'devId': '1'}],
                        'resvBeginTime': transport.body['resvBeginTime'],
                        'resvEndTime': transport.body['resvEndTime'], 'resvStatus': 1027}]}).encode()
                    return response
                return super().request(method, url, **kwargs)

        class School(CampusClient):
            def __init__(client, student, password, **kwargs):
                super().__init__(student, password, transport=Transport(owner.key), **kwargs)

            def login(client):
                owner.logins.append(client.student_id)
                client.identity = Identity(client.student_id, 'Fixture', 123)
                return client.identity

            def _request(client, method, path, **kwargs):
                if method == 'POST':
                    owner.before_guard()
                return super()._request(method, path, **kwargs)

        self.accounts.client_factory = School
        account = self.accounts.verify_and_save('fixture01', 'fixture-password')
        self.account = self.accounts.save_plan(account.id, {'seat_ids': ['1'], 'mode': 'day',
            'begin': '09:00', 'end': '12:00', 'trigger': '07:00:00', 'enabled': True}, account.revision)
        self.run = RunSpec(account.id, self.account.revision, '2026-10-26',
                           '09:00', '12:00', ('1',), time.time()-.001)
        self.now = self.run.due_at+.001
        self.service = ReservationService(self.accounts, self.store, True, wall_clock=lambda: self.now)
        self.store.ensure_run(self.run)
        self.store.transition_run(self.run.key, ('queued',), 'preparing', {}, revision=self.run.revision)
        self.accounts.client(self.account.id).prepare_submission(self.run, '1')
        self.logins.clear()
        self.commands.names.clear()
        self.commands.active = True

    def tearDown(self):
        self.accounts.close()
        self.temp.cleanup()
        self.connection.close()
        self.mongo.client.drop_database(self.db.name)

    def test_prepared_first_post_uses_at_most_seven_reads_and_keeps_durable_claim(self):
        result = self.service.execute(self.run)
        self.assertEqual(result.status, 'success')
        self.assertEqual(len(self.posts), 1)
        before = self.posts[0]
        self.assertEqual(before['row']['status'], 'submitting')
        self.assertEqual(before['row']['attempted_seat_id'], '1')
        self.assertEqual(before['row']['spec']['revision'], self.run.revision)
        commands = Counter(before['commands'])
        self.assertEqual(commands['update'], 1, 'Keep the one pre-POST claim write')
        self.assertEqual(commands['insert'], 0)
        self.assertLessEqual(commands['find'], 7, 'Prepared sessions still repeat the checked account/access reads')
        self.assertEqual(self.logins, [])
        self.assertTrue(self.store.get_run(self.run.key)['attempts'][0]['timings']['prepared'])
        self.service.execute(self.run)
        self.assertEqual(len(self.posts), 1, 'Completed runs must not POST again')

    def assert_final_blocked(self, change):
        self.before_guard = change
        result = self.service.execute(self.run)
        self.assertNotEqual(result.status, 'success')
        self.assertEqual(self.posts, [])
        row = self.store.get_run(self.run.key)
        self.assertEqual(len(row['attempts']), 1)
        self.assertFalse(row['attempts'][0]['sent'])
        self.assertEqual(self.logins, [])

    def test_final_guard_reads_revoked_access_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: self.db.campus_access.update_one(
            {'_id': 'fixture01'}, {'$set': {'enabled': False}}))

    def test_final_guard_reads_changed_plan_revision_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: self.db.campus_accounts.update_one(
            {'_id': self.account.id}, {'$inc': {'revision': 1}}))

    def test_final_guard_reads_disabled_plan_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: self.db.campus_accounts.update_one(
            {'_id': self.account.id}, {'$set': {'plan.enabled': False}}))

    def test_final_guard_rechecks_deadline_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: setattr(self, 'now', self.run.due_at+31))

    def test_final_guard_rechecks_live_switch_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: setattr(self.service, 'allow_live', False))

    def test_final_guard_rechecks_clock_after_cached_session_reuse(self):
        self.assert_final_blocked(lambda: setattr(self.service, 'guard', lambda: False))

    def test_failed_claim_prevents_even_a_prepared_post(self):
        # Another claimant wins after ensure_run returned the preparing snapshot.
        original = self.store.ensure_run
        def claimed(run):
            row = original(run)
            self.db.campus_runs.update_one({'_id': run.key}, {'$set': {'status': 'submitting'}})
            return row
        self.store.ensure_run = claimed
        self.assertEqual(self.service.execute(self.run).status, 'uncertain')
        self.assertEqual(self.posts, [])

    def test_two_concurrent_executions_send_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service.execute, self.run) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result(timeout=5).status, 'success')
        self.assertEqual(len(self.posts), 1)

    def expire_session(self):
        revision, created, client = self.accounts._clients[self.account.id]
        self.accounts._clients[self.account.id] = (revision, created-901, client)

    def test_expired_session_uses_fresh_authentication_and_one_post(self):
        self.expire_session()
        self.assertEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.logins, ['fixture01'])
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.store.get_run(self.run.key)['attempts'][0]['timings']['prepared'])

    def test_missing_session_uses_fresh_authentication_and_one_post(self):
        self.accounts.discard(self.account.id)
        self.assertEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.logins, ['fixture01'])
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.store.get_run(self.run.key)['attempts'][0]['timings']['prepared'])

    def test_missing_cached_identity_cannot_reach_post(self):
        self.accounts._clients[self.account.id][2].identity = None
        self.assertNotEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.posts, [])

    def test_mismatched_cached_identity_cannot_reach_post(self):
        self.accounts._clients[self.account.id][2].identity = Identity('another-student', 'Fixture', 123)
        self.assertNotEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.posts, [])

    def test_permission_revoked_before_expired_session_lookup_prevents_login(self):
        self.expire_session()
        original = self.accounts.client
        def revoked(*args, **kwargs):
            self.db.campus_access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
            return original(*args, **kwargs)
        self.accounts.client = revoked
        self.assertNotEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.logins, [])
        self.assertEqual(self.posts, [])

    def test_credential_revision_mismatch_refreshes_cached_session(self):
        self.db.campus_accounts.update_one({'_id': self.account.id}, {'$inc': {'credential_revision': 1}})
        self.assertEqual(self.service.execute(self.run).status, 'success')
        self.assertEqual(self.logins, ['fixture01'])
        self.assertEqual(len(self.posts), 1)

    def test_ordinary_client_lookup_still_checks_revocation(self):
        self.db.campus_access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        with self.assertRaises(AppError):
            self.accounts.client(self.account.id)
        self.assertEqual(self.posts, [])

    def test_force_refresh_ignores_checked_snapshot_and_checks_revocation(self):
        row = self.store.get_account(self.account.id)
        self.db.campus_access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        with self.assertRaises(AppError):
            self.accounts.client(self.account.id, force=True, _checked_account=row)
        self.assertEqual(self.logins, [])

    def test_other_account_snapshot_cannot_bypass_normal_access_check(self):
        row = self.store.get_account(self.account.id) | {'_id': 'another-account'}
        self.db.campus_access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        with self.assertRaises(AppError):
            self.accounts.client(self.account.id, _checked_account=row)
        self.assertEqual(self.logins, [])

    def test_success_does_not_cache_authorization_for_another_run(self):
        self.assertEqual(self.service.execute(self.run).status, 'success')
        self.db.campus_access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        later = RunSpec(self.account.id, self.account.revision, '2026-10-27',
                        '09:00', '12:00', ('1',), self.run.due_at)
        self.assertEqual(self.service.execute(later).status, 'skipped')
        self.assertEqual(len(self.posts), 1)

    def test_final_account_read_failure_stops_without_post(self):
        def failed_read(*args, **kwargs):
            raise RuntimeError('Fictional database read failure')
        self.before_guard = lambda: setattr(self.store, 'get_account', failed_read)
        self.assertEqual(self.service.execute(self.run).status, 'uncertain')
        self.assertEqual(self.posts, [])
