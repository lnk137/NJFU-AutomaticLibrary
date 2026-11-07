"""Short-lived payload preparation and real scheduler timings, using fake school IO."""
import base64
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import requests
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_v1_5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.models import Identity, RunSpec, AppError, ZONE
from local_app.school import CampusClient
from support.mongo_fixture import MongoFixture
from test_booking_protocol import BookingTransport


class WarmPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = RSA.generate(1024)
        cls.public_key = base64.b64encode(cls.private_key.public_key().export_key(format='DER')).decode()

    def client(self):
        transport = BookingTransport(self.public_key)
        client = CampusClient('fixture01', 'pass', transport=transport, allow_live=True)
        client.identity = Identity('fixture01', 'Fixture', 123)
        return client, transport

    def run_spec(self):
        return RunSpec('a', 2, '2026-10-21', '07:30', '22:00', ('100455862',), 0)

    def prepare(self, client, run=None):
        method = getattr(client, 'prepare_submission', None)
        self.assertTrue(callable(method), 'The release-time public-key round trip has not been moved to preparation')
        method(run or self.run_spec(), '100455862')

    @staticmethod
    def key_count(transport):
        return sum(urlsplit(url).path.endswith('/login/publicKey') for _, url, _ in transport.calls)

    def test_read_only_warmup_then_first_post_needs_no_network_get(self):
        client, transport = self.client()
        client.allow_live = False
        self.prepare(client)
        self.assertTrue(all(method == 'GET' for method, _, _ in transport.calls))
        self.assertEqual(self.key_count(transport), 1)
        transport.calls.clear();client.allow_live = True
        reply = client.submit(self.run_spec(), '100455862')
        self.assertEqual(reply.outcome, 'accepted')
        self.assertEqual([method for method, _, _ in transport.calls], ['POST'])

    def test_prepared_encryption_is_consumed_only_once(self):
        client, transport = self.client()
        self.prepare(client)
        client.submit(self.run_spec(), '100455862')
        client.submit(self.run_spec(), '100455862')
        self.assertEqual(self.key_count(transport), 1)
        client.submit(self.run_spec(), '100455862')
        self.assertEqual(self.key_count(transport), 2)
        payloads = [options['json'] for method, _, options in transport.calls if method == 'POST']
        self.assertEqual(len({p['encryptVlaue'] for p in payloads}), 3)

    def test_all_candidate_payloads_are_warmed_by_one_read_only_key_fetch(self):
        client, transport = self.client()
        run = replace(self.run_spec(), seat_ids=('100455862', '100455863'))
        client.allow_live = False;self.prepare(client, run)
        self.assertTrue(all(method == 'GET' for method, _, _ in transport.calls))
        client.allow_live = True
        for seat in ('100455862', '100455863', '100455862', '100455863'):
            reply = client.submit(run, seat)
            self.assertTrue(reply.timings['prepared'])
        self.assertEqual(self.key_count(transport), 1)
        payloads = [options['json'] for method, _, options in transport.calls if method == 'POST']
        self.assertEqual(len({p['encryptVlaue'] for p in payloads}), 4)
        keys = [PKCS1_v1_5.new(self.private_key).decrypt(base64.b64decode(p['encryptVlaue']), b'bad') for p in payloads]
        self.assertEqual(len(set(keys)), 4)
        decoded = [AES.new(key, AES.MODE_CBC, b'ABCDEF1234123412').decrypt(
            bytes.fromhex(p['resvDevEncrypts'][0])).rstrip(b'\0').decode() for p,key in zip(payloads,keys)]
        self.assertEqual(decoded, ['100455862','100455863','100455862','100455863'])

    def test_expired_backup_payload_fetches_a_new_key(self):
        client, transport = self.client()
        run = replace(self.run_spec(), seat_ids=('100455862', '100455863'))
        with patch('local_app.school.elapsed_time', return_value=100):self.prepare(client, run)
        with patch('local_app.school.elapsed_time', return_value=101):client.submit(run, '100455862')
        with patch('local_app.school.elapsed_time', return_value=102.01):reply = client.submit(run, '100455863')
        self.assertFalse(reply.timings['prepared']);self.assertEqual(self.key_count(transport), 2)

    def test_sleep_expires_prepared_payload_even_when_awake_clock_does_not_advance(self):
        client,transport=self.client()
        with patch('time.monotonic',return_value=100), patch('local_app.school.elapsed_time',
                side_effect=[100,100,105],create=True):
            self.prepare(client)
            reply=client.submit(self.run_spec(),'100455862')
        self.assertFalse(reply.timings['prepared'])
        self.assertEqual(self.key_count(transport),2)

    def test_changed_context_discards_remaining_backup_payloads(self):
        client, transport = self.client()
        run = replace(self.run_spec(), seat_ids=('100455862', '100455863'))
        self.prepare(client, run);client.submit(run, '100455862')
        client.submit(replace(run, revision=run.revision+1), '100455863')
        self.assertEqual(self.key_count(transport), 2)
        reply = client.submit(run, '100455862')
        self.assertFalse(reply.timings['prepared']);self.assertEqual(self.key_count(transport), 3)

    def test_failed_batch_warmup_does_not_leave_partial_payloads(self):
        client, transport = self.client()
        run = replace(self.run_spec(), seat_ids=('100455862', 'invalid-seat'))
        with self.assertRaises(AppError):self.prepare(client, run)
        reply = client.submit(run, '100455862')
        self.assertFalse(reply.timings['prepared']);self.assertEqual(self.key_count(transport), 2)

    def test_payload_older_than_two_seconds_fetches_a_new_key(self):
        client, transport = self.client()
        with patch('local_app.school.elapsed_time', return_value=100):
            self.prepare(client)
        with patch('local_app.school.elapsed_time', return_value=102.01):
            self.assertEqual(client.submit(self.run_spec(), '100455862').outcome, 'accepted')
        self.assertEqual(self.key_count(transport), 2)

    def test_changed_run_seat_identity_or_session_never_reuses_payload(self):
        for change in ('revision', 'seat', 'date', 'time', 'identity', 'session'):
            with self.subTest(change=change):
                client, transport = self.client();run = self.run_spec();seat = '100455862'
                self.prepare(client, run)
                if change == 'revision':run = replace(run, revision=3)
                if change == 'seat':seat = '100455863'
                if change == 'date':run = replace(run, target_date='2026-10-22')
                if change == 'time':run = replace(run, begin='08:00')
                if change == 'identity':client.identity = Identity('fixture02', 'Other', 124)
                if change == 'session':client._token = 'fixture-new-session'
                self.assertEqual(client.submit(run, seat).outcome, 'accepted')
                self.assertEqual(self.key_count(transport), 2)

    def test_warmup_does_not_bypass_live_switch_or_submission_guard(self):
        for gate in ('live', 'clock'):
            with self.subTest(gate=gate):
                client, transport = self.client();self.prepare(client)
                if gate == 'live':client.allow_live = False
                else:client.submission_guard = lambda: False
                try:reply = client.submit(self.run_spec(), '100455862')
                except AppError:reply = None
                self.assertFalse(any(method == 'POST' for method, _, _ in transport.calls))
                if reply:
                    self.assertEqual(reply.outcome, 'rejected')
                    self.assertFalse(reply.sent)

    def test_failed_read_only_warmup_falls_back_to_fresh_key_without_extra_post(self):
        client, transport = self.client()
        def timeout():raise requests.Timeout('fixture key timeout')
        transport.after_key = timeout
        with self.assertRaises(AppError):self.prepare(client)
        transport.after_key = lambda: None
        reply = client.submit(self.run_spec(), '100455862')
        self.assertEqual(reply.outcome, 'accepted')
        self.assertFalse(reply.timings['prepared'])
        self.assertEqual(self.key_count(transport), 2)
        self.assertEqual(sum(method == 'POST' for method, _, _ in transport.calls), 1)

    def test_attempt_timings_distinguish_prepared_and_cold_requests(self):
        for warm in (False, True):
            with self.subTest(warm=warm):
                client, _ = self.client()
                if warm:self.prepare(client)
                reply = client.submit(self.run_spec(), '100455862')
                self.assertIsInstance(getattr(reply, 'timings', None), dict)
                self.assertEqual(reply.timings['prepared'], warm)
                self.assertGreater(reply.timings['request_started_at'], 0)
                self.assertGreaterEqual(reply.timings['prepare_ms'], 0)
                self.assertGreaterEqual(reply.timings['request_ms'], 0)
                self.assertEqual(set(reply.timings), {'prepared', 'request_started_at', 'request_finished_at',
                    'prepare_ms', 'guard_ms', 'request_ms', 'timing_version'})
                self.assertEqual(reply.timings['timing_version'], 2)


class ScheduledSpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.public_key = base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()

    @classmethod
    def tearDownClass(cls):cls.mongo.close()

    def measure(self, *, warmup=True):
        from local_app.config import load_settings
        from local_app.runtime import create_runtime
        transports = []
        public_key = self.public_key

        class TimedTransport(BookingTransport):
            def __init__(self, student):
                super().__init__(public_key)
                self.student, self.keys, self.posts, self.body = student, [], [], None

            def request(self, method, url, **options):
                path = urlsplit(url).path
                if path.endswith('/login/publicKey'):
                    self.keys.append(time.time());time.sleep(.2)
                if method == 'POST':
                    self.posts.append(time.time());self.body = options['json']
                if path.endswith('/reserve/resvInfo'):
                    body = self.body
                    rows = [] if not body else [{'uuid': 'fixture', 'logonName': self.student,
                        'resvDevInfoList': [{'devId': fixture_seat}],
                        'resvBeginTime': body['resvBeginTime'], 'resvEndTime': body['resvEndTime'], 'resvStatus': 1027}]
                    response = requests.Response();response.status_code = 200;response.url = url
                    response._content = json.dumps({'code': 0, 'data': rows}).encode()
                    return response
                return super().request(method, url, **options)

        class ScheduledSchool(CampusClient):
            prepare_submission = CampusClient.prepare_submission if warmup else None
            def __init__(self, student, password, **options):
                transport = TimedTransport(student);transports.append(transport)
                super().__init__(student, password, transport=transport, **options)
            def login(self):
                self.identity = Identity(self.student_id, 'Fixture', 123)
                return self.identity

        db = self.mongo.database()
        with tempfile.TemporaryDirectory(prefix='njfu-speed-') as temp:
            settings = load_settings(ROOT, {'data_dir': Path(temp), 'mongo_uri': self.mongo.uri,
                'database_name': db.name, 'allow_live': True})
            runtime = create_runtime(settings, client_factory=ScheduledSchool)
            from support.auth_fixture import grant
            grant(runtime.auth,[f'fixture{i:02}' for i in range(8)])
            try:
                fixture_seat = next(iter(runtime.accounts.catalog))
                due = int(time.time()) + 3
                trigger = datetime.fromtimestamp(due, ZONE).strftime('%H:%M:%S')
                for i in range(8):
                    a = runtime.accounts.verify_and_save(f'fixture{i:02}', 'pass', str(i), None)
                    runtime.accounts.save_plan(a.id, {'seat_ids': [fixture_seat], 'day_offset': 1,
                        'mode': 'day', 'begin': '09:00', 'end': '12:00', 'trigger': trigger, 'enabled': True}, a.revision)
                runtime.start();until = time.monotonic() + 7
                while runtime.store.runs.count_documents({'status': 'success'}) < 8 and time.monotonic() < until:
                    time.sleep(.01)
                keys = [t for transport in transports for t in transport.keys]
                posts = [t for transport in transports for t in transport.posts]
                measurement = {'accounts': 8, 'key_delay_ms': 200, 'warmup': warmup,
                    'school_network': False, 'post_lateness_ms': [round((t-due)*1000,2) for t in posts],
                    'key_lead_ms': [round((due-t)*1000,2) for t in keys],
                    'successes': runtime.store.runs.count_documents({'status': 'success'})}
            finally:runtime.close()
        self.mongo.client.drop_database(db.name)
        return measurement

    def test_eight_accounts_prepare_before_release_without_early_posts(self):
        measurement = self.measure()
        print('simulated_public_key_delay_ms:', json.dumps(measurement))
        self.assertEqual(len(measurement['post_lateness_ms']), 8)
        self.assertTrue(8 <= len(measurement['key_lead_ms']) <= 16)
        self.assertTrue(all(0 < value <= 4500 for value in measurement['key_lead_ms']), 'Public-key IO still occurs after release')
        self.assertTrue(all(0 <= value < 150 for value in measurement['post_lateness_ms']), 'Unexpected early or delayed POST')
        self.assertEqual(measurement['successes'], 8)
