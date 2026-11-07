"""Observed official seat protocol, tested without contacting the school."""
import base64
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))


class BookingTransport:
    def __init__(self, public_key, *, captcha='0', reply=None):
        self.public_key, self.captcha = public_key, captcha
        self.reply = {'code': 0, 'message': 'success'} if reply is None else reply
        self.calls, self.headers = [], {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.after_key = lambda: None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if (method, path) == ('GET', '/ic-web/sysConfig/public'):
            payload = {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': self.captcha, 'sysKind': 3}]}
        elif (method, path) == ('GET', '/ic-web/login/publicKey'):
            payload = {'code': 0, 'data': {'publicKey': self.public_key, 'nonceStr': 'fixture'}}
            self.after_key()
        elif (method, path) == ('POST', '/ic-web/reserve/seat'):
            payload = self.reply
        else:
            payload = {'code': -100, 'message': '旧预约接口'}
        response = requests.Response()
        response.status_code, response.url = 200, url
        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        pass


class BookingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = RSA.generate(1024)
        cls.public_key = base64.b64encode(cls.private_key.public_key().export_key(format='DER')).decode()

    def client(self, **options):
        from local_app.school import CampusClient
        from local_app.models import Identity
        transport = BookingTransport(options.pop('public_key', self.public_key), **options)
        client = CampusClient('fixture01', 'pass', transport=transport, allow_live=True)
        client.identity = Identity('fixture01', 'Fixture', 123)
        return client, transport

    @staticmethod
    def run_spec():
        from local_app.models import RunSpec
        return RunSpec('a', 1, '2026-10-21', '07:30', '22:00', ('100455862',), 0)

    def test_official_seat_endpoint_and_encrypted_payload_round_trip(self):
        client, transport = self.client()
        reply = client.submit(self.run_spec(), '100455862')
        self.assertEqual(reply.outcome, 'accepted')
        self.assertTrue(reply.sent)
        posts = [call for call in transport.calls if call[0] == 'POST']
        self.assertEqual([urlsplit(call[1]).path for call in posts], ['/ic-web/reserve/seat'])
        payload = posts[0][2]['json']
        self.assertEqual(payload['resvDev'], [0])
        self.assertEqual(payload['resvBeginTime'], '2026-10-21 07:30:00')
        self.assertEqual(payload['resvEndTime'], '2026-10-21 22:00:00')
        key = PKCS1_v1_5.new(self.private_key).decrypt(base64.b64decode(payload['encryptVlaue']), b'invalid')
        self.assertEqual(len(key), 16)
        self.assertTrue(key.isalnum())
        encrypted = payload['resvDevEncrypts'][0]
        self.assertEqual(encrypted, encrypted.upper())
        decoded = AES.new(key, AES.MODE_CBC, b'ABCDEF1234123412').decrypt(bytes.fromhex(encrypted))
        self.assertEqual(decoded, b'100455862\0\0\0\0\0\0\0')
        self.assertEqual(payload['resvMember'], [123])

    def test_each_submission_fetches_key_and_uses_a_new_random_aes_key(self):
        client, transport = self.client()
        client.submit(self.run_spec(), '100455862')
        client.submit(self.run_spec(), '100455863')
        keys = [PKCS1_v1_5.new(self.private_key).decrypt(base64.b64decode(call[2]['json']['encryptVlaue']), b'bad')
                for call in transport.calls if call[0] == 'POST' and urlsplit(call[1]).path.endswith('/seat')]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(sum(urlsplit(call[1]).path.endswith('/login/publicKey') for call in transport.calls), 2)

    def test_required_or_unknown_reservation_captcha_stops_before_post(self):
        for flag in ('1', '2', None, 'unexpected'):
            with self.subTest(flag=flag):
                client, transport = self.client(captcha=flag)
                reply = client.submit(self.run_spec(), '100455862')
                self.assertEqual(reply.outcome, 'rejected')
                self.assertFalse(reply.retryable)
                self.assertFalse(reply.sent)
                self.assertEqual([call[0] for call in transport.calls], ['GET'])

    def test_invalid_public_key_never_posts_a_booking(self):
        for value in ('not-a-public-key', '', None):
            with self.subTest(value=value):
                client, transport = self.client(public_key=value)
                reply = client.submit(self.run_spec(), '100455862')
                self.assertEqual(reply.outcome, 'rejected')
                self.assertFalse(any(call[0] == 'POST' for call in transport.calls))

    def test_guard_rechecked_after_public_key_fetch(self):
        client, transport = self.client()
        allowed = [True]
        client.submission_guard = lambda: allowed[0]
        transport.after_key = lambda: allowed.__setitem__(0, False)
        reply = client.submit(self.run_spec(), '100455862')
        self.assertEqual(reply.outcome, 'rejected')
        self.assertFalse(any(call[0] == 'POST' for call in transport.calls))

    def test_zero_padding_matches_independent_node_crypto_vector(self):
        from unittest.mock import patch
        from local_app.booking_protocol import seat_fields
        # Independently computed with node:crypto, AES-128-CBC and zero-filled block.
        with patch('local_app.booking_protocol.secrets.choice', side_effect=list('AbCdEf0123456789')):
            payload = seat_fields('100455862', self.public_key)
        self.assertEqual(payload['resvDevEncrypts'], ['B9109EC52E6575D7DB335565CF73F840'])

    def test_read_only_preflight_keeps_actual_submission_blocked(self):
        from local_app.models import AppError
        client, transport = self.client()
        client.allow_live = False
        client.prepare_booking()
        with self.assertRaises(AppError):
            client.submit(self.run_spec(), '100455862')
        self.assertEqual([call[0] for call in transport.calls], ['GET'])

    def test_school_rejection_code_and_safe_reason_are_preserved(self):
        client, _ = self.client(reply={'code': 54321, 'message': '该座位已被预约 token=fixture-secret password=fixture-pass'})
        reply = client.submit(self.run_spec(), '100455862')
        self.assertEqual(reply.code, 54321)
        self.assertIn('该座位已被预约', reply.message)
        self.assertNotIn('fixture-secret', reply.message)
        self.assertNotIn('fixture-pass', reply.message)
        self.assertFalse(reply.retryable)

    def test_old_plaintext_endpoint_cannot_be_posted(self):
        from local_app.models import AppError
        client, transport = self.client()
        with self.assertRaises(AppError):
            client._request('POST', 'ic-web/reserve', json={'resvDev': ['100455862']})
        self.assertEqual(transport.calls, [])

    def test_observed_approved_record_can_confirm_exact_booking(self):
        from local_app.models import SchoolRecord
        from local_app.reservations import records_match
        client, _ = self.client()
        record = SchoolRecord('fixture', 'fixture01', '100455862', '2026-10-21 07:30:00', '2026-10-21 22:00:00', 1027)
        self.assertTrue(records_match(self.run_spec(), '100455862', [record], client.identity,
                                      active_statuses=client.active_statuses))
