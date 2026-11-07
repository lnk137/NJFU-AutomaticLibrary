"""Slow public-key reads finish before release without extending payload lifetime."""
import base64
from datetime import datetime
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit

from Crypto.PublicKey import RSA
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Identity, RunSpec, ZONE
from local_app.elapsed_clock import elapsed_time
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from local_app.school import CampusClient
from support.account_fixture import services
from support.mongo_fixture import MongoFixture
from test_booking_protocol import BookingTransport


class EarlyWarmupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture();cls.addClassCleanup(cls.mongo.close)
        cls.key = base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()

    def measure(self, delay):
        db = self.mongo.database()
        temp, store, accounts, _ = services(db)
        transports = []
        public_key = self.key
        class Transport(BookingTransport):
            def __init__(self):
                super().__init__(public_key);self.posts, self.keys, self.body = [], [], None
            def request(self, method, url, **kwargs):
                path = urlsplit(url).path
                if path.endswith('/login/publicKey'):
                    self.keys.append(time.time());time.sleep(delay)
                if method == 'POST':
                    self.posts.append(time.time());self.body = kwargs['json']
                if path.endswith('/reserve/resvInfo'):
                    rows = [] if not self.body else [{'uuid': 'fixture', 'logonName': 'fixture01',
                        'resvDevInfoList': [{'devId': '1'}], 'resvStatus': 1027,
                        'resvBeginTime': self.body['resvBeginTime'], 'resvEndTime': self.body['resvEndTime']}]
                    response = requests.Response();response.status_code = 200
                    response._content = json.dumps({'code': 0, 'data': rows}).encode()
                    return response
                return super().request(method, url, **kwargs)
        class School(CampusClient):
            def __init__(self, student, password, **kwargs):
                transport = Transport();transports.append(transport)
                super().__init__(student, password, transport=transport, **kwargs)
            def login(self):
                self.identity = Identity(self.student_id, 'Fixture', 123);return self.identity
        scheduler = None
        try:
            accounts.client_factory = School
            a = accounts.verify_and_save('fixture01', 'pass')
            due = int(time.time()) + 6
            accounts.save_plan(a.id, {'seat_ids': ['1'], 'day_offset': 1, 'mode': 'day',
                'begin': '09:00', 'end': '12:00', 'enabled': True,
                'trigger': datetime.fromtimestamp(due, ZONE).strftime('%H:%M:%S')}, a.revision)
            scheduler = LocalScheduler(accounts, ReservationService(accounts, store, True), store)
            scheduler.start()
            until = time.monotonic()+11
            while time.monotonic()<until:
                if store.runs.count_documents({'status': 'success'}) == 1:break
                time.sleep(.005)
            scheduler.stop()
            row = store.runs.find_one({'account_id': a.id})
            posts = [t for transport in transports for t in transport.posts]
            keys = [t for transport in transports for t in transport.keys]
            self.assertEqual(row['status'], 'success')
            self.assertEqual(len(posts), 1)
            self.assertGreaterEqual(posts[0], due)
            self.assertTrue(row['attempts'][0]['timings']['prepared'])
            return (posts[0]-due)*1000, len(keys)
        finally:
            if scheduler:scheduler.stop()
            accounts.close();temp.cleanup();self.mongo.client.drop_database(db.name)

    def test_three_second_key_is_ready_at_release(self):
        latency, keys = self.measure(3)
        self.assertLess(latency, 250, 'Slow prewarm still held the first POST past release')
        self.assertEqual(keys, 1)

    def test_fast_early_payload_is_refreshed_instead_of_used_after_expiry(self):
        latency, keys = self.measure(.03)
        self.assertEqual(keys, 2)
        self.assertLess(latency, 250)

    def test_slow_read_respects_warmup_budget_and_leaves_no_cached_payload(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(.6)
                try:
                    self.send_response(200);self.end_headers();self.wfile.write(b'{}')
                except OSError:pass
            def log_message(self, *args):pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        class LocalTransport(requests.Session):
            def request(self, method, url, **kwargs):
                assert method == 'GET'
                return super().request(method, f'http://127.0.0.1:{server.server_port}/', **kwargs)
        client = CampusClient('fixture01', 'pass', transport=LocalTransport())
        client.identity = Identity('fixture01', 'Fixture', 123)
        client._booking_ready = True
        run = RunSpec('fixture', 1, '2026-10-26', '09:00', '12:00', ('1',), time.time()+1)
        try:
            method = getattr(client, 'prepare_scheduled_submission', None)
            self.assertTrue(callable(method), 'Scheduled warmup has no bounded network budget')
            started = time.monotonic()
            with self.assertRaises(AppError):method(run, '1', elapsed_time()+.15)
            self.assertLess(time.monotonic()-started, .5)
            self.assertIsNone(client._take_prepared(run, '1'))
        finally:
            client.close();server.shutdown();server.server_close();thread.join(2)
