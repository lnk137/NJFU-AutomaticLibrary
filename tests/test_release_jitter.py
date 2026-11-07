"""Real scheduling and crypto with deliberately slow, entirely fake school IO."""
import base64
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from Crypto.PublicKey import RSA
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.models import Identity, ZONE
from local_app.school import CampusClient
from support.mongo_fixture import MongoFixture
from test_booking_protocol import BookingTransport


def measure_delayed_key(mongo, public_key, scheduler_class=None):
    """Use real app services; no socket request can reach the school."""
    from local_app.config import load_settings
    from local_app.runtime import create_runtime
    from support.auth_fixture import grant

    transports = []
    class Transport(BookingTransport):
        def __init__(self, student):
            super().__init__(public_key)
            self.student, self.keys, self.posts, self.body = student, [], [], None
        def request(self, method, url, **options):
            path = urlsplit(url).path
            if path.endswith('/login/publicKey'):
                started = time.time()
                time.sleep(1.18 if self.student == 'fixture02' else .2)
                self.keys.append({'started_at': started, 'finished_at': time.time()})
            if method == 'POST':
                self.posts.append(time.time());self.body = options['json']
            if path.endswith('/reserve/resvInfo'):
                body = self.body
                rows = [] if not body else [{'uuid': 'fixture-'+self.student, 'logonName': self.student,
                    'resvDevInfoList': [{'devId': seat}], 'resvBeginTime': body['resvBeginTime'],
                    'resvEndTime': body['resvEndTime'], 'resvStatus': 1027}]
                response = requests.Response();response.status_code = 200;response.url = url
                response._content = json.dumps({'code': 0, 'data': rows}).encode()
                return response
            return super().request(method, url, **options)

    class School(CampusClient):
        def __init__(self, student, password, **options):
            transport = Transport(student);transports.append(transport)
            super().__init__(student, password, transport=transport, **options)
        def login(self):
            self.identity = Identity(self.student_id, 'Fixture', 123)
            return self.identity

    db = mongo.database()
    try:
        with tempfile.TemporaryDirectory(prefix='njfu-jitter-') as temp:
            runtime = create_runtime(load_settings(ROOT, {'data_dir': Path(temp), 'mongo_uri': mongo.uri,
                'database_name': db.name, 'allow_live': True}), client_factory=School)
            try:
                if scheduler_class:
                    runtime.scheduler.stop()
                    runtime.scheduler = scheduler_class(runtime.accounts, runtime.reservations, runtime.store)
                grant(runtime.auth, ['fixture00', 'fixture01', 'fixture02'])
                seat = next(iter(runtime.accounts.catalog));due = int(time.time())+3
                trigger = datetime.fromtimestamp(due, ZONE).strftime('%H:%M:%S')
                for i in range(3):
                    account = runtime.accounts.verify_and_save(f'fixture{i:02}', 'pass', str(i), None)
                    runtime.accounts.save_plan(account.id, {'seat_ids': [seat], 'day_offset': 1,
                        'mode': 'day', 'time_mode': 'full_day', 'trigger': trigger, 'enabled': True}, account.revision)
                runtime.start();until = time.monotonic()+7
                while time.monotonic() < until:
                    if db.campus_runs.count_documents({'status': 'success'}) == 3:
                        futures = [j['submit'] for j in runtime.scheduler.jobs.values() if j.get('submit')]
                        if all(f.done() for f in futures):break
                    time.sleep(.005)
                runtime.scheduler.stop()
                rows = list(db.campus_runs.find())
                accounts = {a.id: a.student_id for a in runtime.store.list_accounts()}
                result = {'school_network': False, 'rows': []}
                for row in rows:
                    student = accounts[row['account_id']]
                    transport = next(t for t in transports if t.student == student and t.posts)
                    result['rows'].append({'student': student, 'status': row['status'],
                        'posts': len(transport.posts), 'post_lateness_ms': round((transport.posts[0]-due)*1000, 3),
                        'key_reads': len(transport.keys), 'key_finished_before_due_ms': round((due-transport.keys[-1]['finished_at'])*1000, 3),
                        'prepared': row['attempts'][0]['timings']['prepared'],
                        'scheduler_timings': row.get('scheduler_timings'),
                        'history_timings': next(r for r in runtime.store.list_runs(row['account_id'], None).items
                            if r['id'] == row['_id']).get('scheduler_timings')})
                return result
            finally:runtime.close()
    finally:mongo.client.drop_database(db.name)


class ReleaseJitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.addClassCleanup(cls.mongo.close)
        cls.public_key = base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()
        cls.measurement = measure_delayed_key(cls.mongo, cls.public_key)
        print('slow_key_measurement:', json.dumps(cls.measurement))
    def test_slow_public_key_is_ready_before_release_without_early_or_duplicate_posts(self):
        self.assertEqual(len(self.measurement['rows']), 3)
        for row in self.measurement['rows']:
            with self.subTest(student=row['student']):
                self.assertEqual((row['status'], row['posts'], row['prepared']), ('success', 1, True))
                self.assertIn(row['key_reads'], (1, 2))
                self.assertGreater(row['key_finished_before_due_ms'], 0)
                self.assertGreaterEqual(row['post_lateness_ms'], 0)
                self.assertLess(row['post_lateness_ms'], 150)

    def test_persisted_phase_timings_identify_payload_delay_and_are_visible_in_history(self):
        for row in self.measurement['rows']:
            with self.subTest(student=row['student']):
                timings = row['scheduler_timings']
                self.assertIsInstance(timings, dict)
                self.assertEqual(timings, row['history_timings'])
                for stage in ('prepare', 'warm', 'submit'):
                    self.assertGreaterEqual(timings[stage]['queue_ms'], 0)
                    self.assertGreaterEqual(timings[stage]['account_lock_wait_ms'], 0)
                    self.assertGreaterEqual(timings[stage]['elapsed_ms'], 0)
                    self.assertGreaterEqual(timings[stage]['finished_at'], timings[stage]['started_at'])
                    expected_outcome = 'finished' if stage == 'submit' else 'ready'
                    self.assertEqual(timings[stage]['outcome'], expected_outcome)
                    attempts = timings[stage]['attempts']
                    self.assertIsInstance(attempts, list)
                    self.assertGreaterEqual(len(attempts), 1)
                    self.assertLessEqual(len(attempts), 11)
                    self.assertEqual(attempts[-1]['outcome'], expected_outcome)
                    for attempt in attempts:
                        for field in ('queue_ms', 'account_lock_wait_ms', 'elapsed_ms', 'started_at', 'finished_at'):
                            self.assertIn(type(attempt[field]), (int, float))
                        self.assertGreaterEqual(attempt['finished_at'], attempt['started_at'])
                if row['student'] == 'fixture02':
                    self.assertGreaterEqual(timings['warm']['payload_prepare_ms'], 1170)
                    self.assertLess(timings['warm']['finished_at'], timings['submit']['started_at'])


if __name__ == '__main__':unittest.main()
