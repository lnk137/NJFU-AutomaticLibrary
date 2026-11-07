"""Closing while mail drains cannot admit another booking or cancellation POST."""
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_app.config import load_settings
from local_app.models import AppError, Seat, ZONE
from local_app.runtime import create_runtime
from support.mongo_fixture import MongoFixture
from support.native_school import CampusFixture


class RuntimeCloseAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.addClassCleanup(cls.mongo.close)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='njfu-close-'))).resolve()
        self.fixture = self.stack.enter_context(CampusFixture())
        self.student = '2312000001'
        self.fixture.register(self.student, 1001)
        self.db = self.mongo.database()
        self.addCleanup(self.mongo.client.drop_database, self.db.name)
        catalog = {str(i): Seat(str(i), 'A' + str(i), '2F') for i in range(1, 4)}
        self.stack.enter_context(patch('local_app.runtime.load_catalog', return_value=catalog))
        root = Path(__file__).resolve().parents[1]
        settings = load_settings(root, {'data_dir': self.directory, 'mongo_uri': self.mongo.uri,
            'database_name': self.db.name, 'executor': 'python', 'allow_live': True})
        self.runtime = create_runtime(settings,
            client_factory=lambda student, password, **kwargs: self.fixture.client(student, password, **kwargs))
        self.addCleanup(self.runtime.close)
        self.runtime.auth.initialize_root('isolated-root-password', [self.student])
        self.account = self.runtime.accounts.verify_and_save(self.student, 'fixture-password')
        self.now = datetime.now(ZONE).replace(hour=8, minute=0, second=0, microsecond=0)
        self.runtime.reservations.wall_clock = lambda: self.now.timestamp()
        self.release_mail = threading.Event()
        self.release_first = threading.Event()
        self.close_thread = None
        self.addCleanup(self.release_background)

    def release_background(self):
        self.release_first.set()
        self.release_mail.set()
        if self.close_thread:
            self.close_thread.join(timeout=8)
            self.assertFalse(self.close_thread.is_alive(), 'Runtime close did not drain its owned work')

    def block_mail(self):
        entered = threading.Event()
        def sender(_):
            entered.set()
            self.release_mail.wait(timeout=10)
        self.runtime.notifications.sender = sender
        self.runtime.notifications.request_verification(self.account.id, {'address': 'fixture@example.test'})
        self.runtime.start()
        self.assertTrue(entered.wait(timeout=2), 'Private mail sender did not enter')

    def begin_close(self):
        entered = threading.Event()
        original = self.runtime.notifications.close
        def observed_close():
            entered.set()
            original()
        self.runtime.notifications.close = observed_close
        self.close_thread = threading.Thread(target=self.runtime.close, name='fixture-runtime-close', daemon=True)
        self.close_thread.start()
        self.assertTrue(entered.wait(timeout=2), 'Runtime did not begin draining mail')
        self.assertTrue(self.close_thread.is_alive())
        return self.runtime.reservations.guard(), self.runtime.cancellations.closed

    def test_mail_drain_cannot_admit_next_segment_and_keeps_first_receipt(self):
        self.block_mail()
        first_finished = threading.Event()
        execute = self.runtime.reservations.execute
        def pause_after_first(run, **kwargs):
            result = execute(run, **kwargs)
            if run.begin == '10:00':
                first_finished.set()
                self.release_first.wait(timeout=5)
            return result
        self.runtime.reservations.execute = pause_after_first
        preview = self.runtime.today.batches.preview(self.account.id, {
            'expected_revision': self.account.revision, 'date': self.now.date().isoformat(),
            'segments': [dict(begin='10:00', end='12:00', seat_ids=['1']),
                         dict(begin='12:00', end='14:00', seat_ids=['2'])]})
        batch = self.runtime.today.batches.submit(self.account.id, preview['preview_token'])
        self.assertTrue(first_finished.wait(timeout=3), 'First private TLS POST did not finish')
        self.assertEqual(len(self.fixture.posts()), 1)
        live_during_drain, cancel_closed = self.begin_close()
        self.release_first.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.runtime.today.batches.get(self.account.id, batch['batch_id'])
            if result['state'] != 'running':
                break
            time.sleep(.005)
        self.assertEqual(len(self.fixture.posts()), 1, 'Closing admitted a second actual fixture booking POST')
        self.assertFalse(live_during_drain)
        self.assertTrue(cancel_closed)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual([segment['status'] for segment in result['segments']], ['success', 'not_submitted'])
        first = self.runtime.store.get_run(result['segments'][0]['run_id'])
        self.assertEqual(first['status'], 'success')
        self.assertEqual(first['result']['records'][0]['status'], 1027)

    def test_mail_drain_cannot_admit_confirmed_cancellation(self):
        self.fixture.receipts.append({'owner': self.student, 'uuid': 'fixture-to-cancel', 'logonName': self.student,
            'resvDevInfoList': [{'devId': '1'}], 'resvBeginTime': self.now.date().isoformat() + ' 10:00:00',
            'resvEndTime': self.now.date().isoformat() + ' 12:00:00', 'resvStatus': 1027})
        preview = self.runtime.cancellations.preview(self.account.id,
            {'record_id': 'fixture-to-cancel', 'expected_revision': self.account.revision})
        self.block_mail()
        live_during_drain, cancel_closed = self.begin_close()
        failure = None
        try:
            self.runtime.cancellations.submit(self.account.id, preview['preview_token'])
        except AppError as exc:
            failure = exc.code
        posts = [event for event in self.fixture.events if event.get('path') == '/ic-web/reserve/delete']
        self.assertEqual(posts, [], 'Closing admitted an actual fixture cancellation POST')
        self.assertFalse(live_during_drain)
        self.assertTrue(cancel_closed)
        self.assertEqual(failure, 'service_closed')
        self.assertEqual(self.db.campus_cancellations.count_documents({}), 0)
        self.assertEqual(self.fixture.receipts[0]['uuid'], 'fixture-to-cancel')
