"""Real booking service/client/storage; only school HTTP and elapsed time are fake."""
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.models import Identity, RunSpec, SchoolRecord, Seat, ZONE
from local_app.reservations import ReservationService
from local_app.school import CampusClient
from support.account_fixture import services
from support.mongo_fixture import MongoFixture


BUSY = {'code': 1, 'message': '当前设备正在被预约，请稍后重试'}


def record(seat='1', **changes):
    return {'uuid': 'fixture-record', 'logonName': 'fixture01',
            'resvDevInfoList': [{'devId': seat}], 'resvBeginTime': '2026-10-23 10:00:00',
            'resvEndTime': '2026-10-23 20:00:00', 'resvStatus': 1027} | changes


class RecoveryTransport:
    def __init__(self, private_key):
        self.key = private_key
        self.headers, self.cookies = {}, requests.cookies.RequestsCookieJar()
        self.replies, self.query_replies = deque(), deque()
        self.events, self.posts, self.payloads = [], [], []
        self.accepted_seat = None
        self.after_query = self.after_key = lambda: None

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        if (method, path) == ('GET', '/ic-web/sysConfig/public'):
            payload = {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': '0'}]}
        elif (method, path) == ('GET', '/ic-web/login/publicKey'):
            payload = {'code': 0, 'data': {'publicKey': base64.b64encode(
                self.key.public_key().export_key(format='DER')).decode()}}
            self.after_key()
        elif (method, path) == ('POST', '/ic-web/reserve/seat'):
            body = kwargs['json']
            key = PKCS1_v1_5.new(self.key).decrypt(base64.b64decode(body['encryptVlaue']), b'bad')
            seat = AES.new(key, AES.MODE_CBC, b'ABCDEF1234123412').decrypt(
                bytes.fromhex(body['resvDevEncrypts'][0])).rstrip(b'\0').decode()
            self.posts.append(seat); self.events.append(('POST', seat)); self.payloads.append(body)
            payload = self.replies.popleft() if self.replies else BUSY
            if isinstance(payload, dict) and payload.get('code') == 0:
                self.accepted_seat = seat
        elif (method, path) == ('GET', '/ic-web/reserve/resvInfo'):
            assert kwargs['params']['beginDate'] == kwargs['params']['endDate'] == '2026-10-23'
            self.events.append(('QUERY', kwargs['params']['page']))
            payload = self.query_replies.popleft() if self.query_replies else {
                'code': 0, 'data': [record(self.accepted_seat)] if self.accepted_seat else []}
            self.after_query()
        else:
            raise AssertionError(('Unexpected school operation', method, path))
        if isinstance(payload, Exception): raise payload
        response = requests.Response(); response.status_code = 200; response.url = url
        response._content = json.dumps(payload).encode()
        return response

    def close(self): pass


class BusyRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.key = RSA.generate(1024)

    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, self.catalog = services(self.db)
        self.catalog.update({str(i): Seat(str(i), f'A00{i}', '2F') for i in range(3, 6)})
        self.transport = RecoveryTransport(self.key)
        transport = self.transport

        class FixtureCampus(CampusClient):
            def __init__(self, student, password, **kwargs):
                super().__init__(student, password, transport=transport, **kwargs)

            def login(self):
                self.identity = Identity(self.student_id, 'Fixture', 123)
                return self.identity

        self.accounts.client_factory = FixtureCampus
        a = self.accounts.verify_and_save('fixture01', 'pass', 'Fixture', None)
        self.account = self.accounts.save_plan(a.id, {
            'seat_ids': ['1', '2'], 'day_offset': 1, 'mode': 'day', 'time_mode': 'custom',
            'begin': '10:00', 'end': '20:00', 'trigger': '07:00:00', 'enabled': True}, a.revision)
        due = datetime(2026, 10, 22, 7, tzinfo=ZONE).timestamp()
        self.now = due
        self.run = RunSpec(self.account.id, self.account.revision, '2026-10-23',
                           '10:00', '20:00', ('1', '2'), due)

    def tearDown(self):
        self.accounts.close(); self.temp.cleanup(); self.mongo.client.drop_database(self.db.name)

    def service(self):
        return ReservationService(self.accounts, self.store, True, wall_clock=lambda: self.now)

    def test_first_post_uses_one_durable_claim_with_attempted_seat(self):
        self.transport.replies.append({'code': 0})
        original_transition = self.store.transition_run
        original_request = self.transport.request
        observed = []

        def slow_claim(key, states, status, fields=None, **kwargs):
            if fields and ('submitted_at' in fields or 'attempted_seat_id' in fields):
                time.sleep(.1)  # Model durable-write latency, with real isolated storage.
            return original_transition(key, states, status, fields, **kwargs)

        def request(method, url, **kwargs):
            if method == 'POST':
                observed.append((time.perf_counter(), self.store.get_run(self.run.key)))
            return original_request(method, url, **kwargs)

        self.store.transition_run = slow_claim
        self.transport.request = request
        started = time.perf_counter()
        result = self.service().execute(self.run)
        self.assertEqual(result.status, 'success')
        self.assertEqual(len(observed), 1)
        posted, row = observed[0]
        self.assertEqual(row['status'], 'submitting')
        self.assertEqual(row['attempted_seat_id'], '1')
        self.assertEqual(row['submitted_at'], self.now)
        self.assertLess(posted-started, .17, 'The first attempt must avoid a second durable claim')

    def test_busy_queries_before_switching_to_backup_and_keeps_receipt(self):
        self.transport.replies.extend([BUSY, {'code': 0}])
        before = self.store.get_account(self.account.id)['plan']
        started = time.perf_counter()
        result = self.service().execute(self.run)
        elapsed = time.perf_counter()-started
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.transport.posts, ['1', '2'])
        self.assertEqual(self.transport.events, [('POST', '1'), ('QUERY', 1), ('POST', '2'), ('QUERY', 1)])
        self.assertGreaterEqual(elapsed, 1.2, 'Busy backup must observe the fixed request-start floor')
        self.assertLess(elapsed, 1.5, 'Use the later floor rather than summing the two intervals')
        row = self.store.get_run(self.run.key)
        self.assertEqual(row['attempted_seat_id'], '2')
        self.assertEqual([x['seat_id'] for x in row['attempts']], ['1', '2'])
        self.assertEqual(row['attempts'][0]['retry_check']['state'], 'empty')
        self.assertGreaterEqual(row['attempts'][0]['retry_check']['query_ms'], 0)
        self.assertEqual(result.records[0].seat_id, '2')
        self.assertEqual(self.store.get_account(self.account.id)['plan'], before)
        self.assertTrue(all(x['resvEndTime'] == '2026-10-23 20:00:00' for x in self.transport.payloads))

    def test_single_seat_can_retry_once_after_empty_query(self):
        self.transport.replies.extend([BUSY, {'code': 0}])
        result = self.service().execute(replace(self.run, seat_ids=('1',)))
        self.assertEqual(result.status, 'success'); self.assertEqual(self.transport.posts, ['1', '1'])

    def test_all_busy_is_bounded_across_five_seats_and_not_replayed(self):
        run = replace(self.run, seat_ids=('1', '2', '3', '4', '5'))
        service = self.service(); result = service.execute(run)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.transport.posts, ['1', '2', '3', '4', '5', '1'])
        service.execute(run)
        ReservationService(self.accounts, self.store, True).execute(run)
        self.assertEqual(len(self.transport.posts), 6)

    def test_each_of_two_seats_is_attempted_at_most_twice(self):
        self.assertEqual(self.service().execute(self.run).status, 'failed')
        self.assertEqual(self.transport.posts, ['1', '2', '1', '2'])

    def test_busy_with_an_exact_school_record_confirms_without_another_post(self):
        self.transport.query_replies.append({'code': 0, 'data': [record()]})
        result = self.service().execute(self.run)
        self.assertEqual(result.status, 'success'); self.assertEqual(self.transport.posts, ['1'])

    def test_late_record_for_an_earlier_attempt_confirms_that_seat(self):
        self.transport.query_replies.extend([{'code': 0, 'data': []}, {'code': 0, 'data': [record()]}])
        result = self.service().execute(self.run)
        self.assertEqual(result.status, 'success'); self.assertEqual(self.transport.posts, ['1', '2'])
        self.assertEqual(self.store.get_run(self.run.key)['attempted_seat_id'], '1')

    def test_later_read_only_reconcile_can_confirm_an_earlier_attempt(self):
        self.transport.query_replies.extend([
            {'code': 0, 'data': []}, requests.Timeout(), {'code': 0, 'data': [record()]}])
        service = self.service()
        self.assertEqual(service.execute(self.run).status, 'uncertain')
        self.assertEqual(self.store.get_run(self.run.key)['attempted_seat_id'], '2')
        self.assertEqual(service.reconcile(self.run.key).status, 'success')
        self.assertEqual(self.store.get_run(self.run.key)['attempted_seat_id'], '1')
        self.assertEqual(self.transport.posts, ['1', '2'])

    def test_other_existing_reservation_stops_backup(self):
        self.transport.query_replies.append({'code': 0, 'data': [record('5')]})
        result = self.service().execute(self.run)
        self.assertEqual(result.status, 'uncertain'); self.assertEqual(self.transport.posts, ['1'])

    def test_unknown_record_status_is_not_treated_as_an_empty_query(self):
        self.transport.query_replies.append({'code': 0, 'data': [record(resvStatus=9999)]})
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_wrong_student_record_stops_instead_of_confirming_or_retrying(self):
        self.transport.query_replies.append({'code': 0, 'data': [record(logonName='other')]})
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_query_timeout_stops_without_another_post(self):
        self.transport.query_replies.append(requests.Timeout())
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_malformed_query_stops_without_another_post(self):
        self.transport.query_replies.append({'code': 0, 'data': None})
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_failed_later_query_page_never_returns_partial_absence(self):
        self.transport.query_replies.extend([{'code': 0, 'data': [record('5')]*50}, {'code': 1, 'data': []}])
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])
        self.assertEqual(self.transport.events[-2:], [('QUERY', 1), ('QUERY', 2)])

    def test_unknown_code_one_and_similar_messages_never_retry(self):
        for reply in ({'code': 1, 'message': '您已有预约'}, {'code': 1, 'message': '系统繁忙'},
                      {'code': 2, 'message': BUSY['message']},
                      {'code': 1, 'message': BUSY['message']+'，需要人工处理'}):
            with self.subTest(reply=reply):
                self.transport.replies.append(reply)
                client = self.accounts.client(self.account.id); client.allow_live = True
                result = client.submit(self.run, '1')
                self.assertEqual(result.outcome, 'rejected'); self.assertFalse(result.retryable)
        self.assertFalse(any(x[0] == 'QUERY' for x in self.transport.events))

    def test_uncertain_post_is_never_repeated(self):
        self.transport.replies.append(requests.Timeout())
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_accepted_but_unconfirmed_post_does_not_try_backup(self):
        self.transport.replies.append({'code': 0})
        self.transport.query_replies.append({'code': 0, 'data': []})
        self.assertEqual(self.service().execute(self.run).status, 'uncertain')
        self.assertEqual(self.transport.posts, ['1'])

    def test_slow_query_expires_original_deadline_without_retry(self):
        self.transport.after_query = lambda: setattr(self, 'now', self.run.due_at+31)
        self.service().execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_permission_revocation_during_query_stops_retry(self):
        self.transport.after_query = lambda: setattr(self.accounts, 'is_allowed', lambda student: False)
        self.service().execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_plan_revision_change_during_query_stops_retry(self):
        self.transport.after_query = lambda: self.store.accounts.update_one(
            {'_id': self.account.id}, {'$inc': {'revision': 1}})
        self.service().execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_closed_gate_during_query_stops_retry(self):
        service = self.service()
        self.transport.after_query = lambda: setattr(service, 'allow_live', False)
        service.execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_failed_clock_guard_during_query_stops_retry(self):
        service = self.service()
        self.transport.after_query = lambda: setattr(service, 'guard', lambda: False)
        service.execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_deadline_rechecked_after_second_key_read(self):
        def after_key():
            if self.transport.posts: self.now = self.run.due_at+31
        self.transport.after_key = after_key
        self.service().execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_failed_durable_retry_check_write_never_sends_next_post(self):
        service = self.service(); original = self.store.transition_run
        def transition(key, states, state, fields, **kwargs):
            if any('retry_check' in x for x in fields.get('attempts', [])): return False
            return original(key, states, state, fields, **kwargs)
        with patch.object(self.store, 'transition_run', transition): service.execute(self.run)
        self.assertEqual(self.transport.posts, ['1'])

    def test_concurrent_execution_keeps_one_bounded_task(self):
        self.transport.replies.extend([BUSY, {'code': 0}]); service = self.service()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.execute(self.run), range(2)))
        self.assertEqual([x.status for x in results], ['success', 'success'])
        self.assertEqual(self.transport.posts, ['1', '2'])

    def test_target_day_queries_preserve_other_days_in_shared_snapshot(self):
        older = SchoolRecord('other-day', 'fixture01', '5', '2026-10-24 10:00:00', '2026-10-24 20:00:00', 1027)
        self.store.save_snapshot(self.account.id, self.account.revision, [older], 'fixture')
        self.transport.replies.extend([BUSY, {'code': 0}])
        self.service().execute(self.run)
        records = self.store.get_account(self.account.id)['snapshot']
        self.assertIn(asdict(older), records)

    def test_manual_expiry_during_query_does_not_extend_confirmation(self):
        self.now = datetime(2026, 10, 23, 8, tzinfo=ZONE).timestamp()
        run = replace(self.run, due_at=self.now, kind='manual', operation_id='fixture', expires_at=self.now+.5)
        self.transport.after_query = lambda: setattr(self, 'now', self.now+1)
        self.service().execute(run)
        self.assertEqual(self.transport.posts, ['1'])
        self.assertIn(('QUERY', 1), self.transport.events)


if __name__ == '__main__': unittest.main()
