"""Explicit cancellation with fake school transport and isolated real storage."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from support.account_fixture import services
from support.mongo_fixture import MongoFixture
from test_today_booking import TodaySchool
from local_app.models import AppError, SchoolRecord, ZONE


class CancelSchool(TodaySchool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancels, self.cancel_mode = [], 'success'

    def cancel(self, record_id):
        assert self.allow_live and self.cancellation_guard()
        self.cancels.append(record_id)
        if self.cancel_mode == 'timeout':
            raise AppError('network_error', 'Synthetic uncertain network', 503)
        if self.cancel_mode == 'reject':
            return {'code': 1}
        if self.cancel_mode != 'unverified':
            self.records = [r for r in self.records if r.id != record_id]
        return {'code': 0}


class CancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        from local_app.cancellations import CancellationService
        from local_app.reservations import ReservationService
        self.no_http = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP denied'))
        self.no_http.start()
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.accounts.client_factory = CancelSchool
        self.account = self.accounts.verify_and_save('fixture01', 'pass', 'Fixture')
        self.now = datetime(2026, 11, 6, 8, 10, tzinfo=ZONE).timestamp()
        self.reservations = ReservationService(self.accounts, self.store, True, wall_clock=lambda: self.now)
        self.service = CancellationService(self.accounts, self.reservations, self.store)
        self.school = self.accounts.client(self.account.id)
        self.record = SchoolRecord('cancel-fixture', 'fixture01', '1', '2026-11-06 09:00:00', '2026-11-06 12:00:00', 1027)
        self.sibling = replace(self.record, id='other', seat_id='2', begin='2026-11-06 14:00:00', end='2026-11-06 18:00:00')
        self.school.records = [self.record, self.sibling]

    def tearDown(self):
        self.service.close(); self.accounts.close()
        self.mongo.client.drop_database(self.db.name); self.temp.cleanup(); self.no_http.stop()

    def preview(self):
        return self.service.preview(self.account.id, {'record_id': self.record.id, 'expected_revision': self.account.revision})

    def submit(self, preview=None):
        return self.service.submit(self.account.id, (preview or self.preview())['preview_token'])

    def test_preview_only_reads_and_duplicate_confirm_cancels_exactly_one(self):
        before = deepcopy(self.store.get_account(self.account.id))
        preview = self.preview()
        self.assertEqual(self.school.cancels, [])
        first = self.submit(preview); second = self.submit(preview)
        self.assertEqual((first['state'], second['state']), ('success', 'success'))
        self.assertEqual(self.school.cancels, ['cancel-fixture'])
        self.assertEqual(self.school.records, [self.sibling])
        after = self.store.get_account(self.account.id)
        for key in ('plan', 'date_exceptions', 'secret', 'revision', 'credential_revision'):
            self.assertEqual(before.get(key), after.get(key))
        self.assertEqual(self.store.runs.count_documents({}), 0)

    def test_distinct_previews_for_same_receipt_cannot_repeat_cancel(self):
        a, b = self.preview(), self.preview()
        self.assertEqual(self.submit(a)['cancel_id'], self.submit(b)['cancel_id'])
        self.assertEqual(len(self.school.cancels), 1)

    def test_timeout_and_restart_never_replay(self):
        self.school.cancel_mode = 'timeout'; preview = self.preview()
        self.assertEqual(self.submit(preview)['state'], 'uncertain')
        from local_app.cancellations import CancellationService
        self.service.close(); self.service = CancellationService(self.accounts, self.reservations, self.store)
        self.assertEqual(self.submit(preview)['state'], 'uncertain')
        self.assertEqual(len(self.school.cancels), 1)

    def test_ack_without_readback_is_uncertain_until_fresh_query(self):
        self.school.cancel_mode = 'unverified'; result = self.submit()
        self.assertEqual(result['state'], 'uncertain')
        self.school.records = [self.sibling]
        self.store.save_snapshot(self.account.id, self.account.revision, self.school.records,
            datetime.fromtimestamp(self.now + 1, ZONE).isoformat())
        self.assertEqual(self.service.get(self.account.id, result['cancel_id'])['state'], 'uncertain')
        self.assertEqual(self.service.reconcile(self.account.id, result['cancel_id'])['state'], 'success')
        self.assertEqual(len(self.school.cancels), 1)

    def test_unknown_status_after_ack_is_not_cancellation_proof(self):
        self.school.cancel_mode = 'unverified'
        result = self.submit()
        self.school.records = [replace(self.record, status=999999), self.sibling]
        self.assertEqual(self.service.reconcile(self.account.id, result['cancel_id'])['state'], 'uncertain')
        self.assertEqual(self.service.get(self.account.id, result['cancel_id'])['state'], 'uncertain')

    def test_rejection_keeps_siblings_and_never_claims_success(self):
        self.school.cancel_mode = 'reject'
        self.assertEqual(self.submit()['state'], 'failed')
        self.assertEqual(self.school.records, [self.record, self.sibling])

    def test_expiry_revision_access_live_clock_and_release_guards(self):
        for condition in ('expired', 'revision', 'access', 'live', 'clock', 'release'):
            with self.subTest(condition=condition):
                preview = self.preview()
                original_now = self.now
                if condition == 'expired': self.now += 121
                if condition == 'revision': self.store.accounts.update_one({'_id': self.account.id}, {'$inc': {'revision': 1}})
                if condition == 'access': self.accounts.is_allowed = lambda _: False
                if condition == 'live': self.reservations.allow_live = False
                if condition == 'clock': self.reservations.guard = lambda: False
                if condition == 'release': self.service.quiet_window = lambda: (_ for _ in ()).throw(AppError('release_window', 'Synthetic window', 409))
                with self.assertRaises(AppError): self.submit(preview)
                self.now = original_now
                self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'revision': self.account.revision}})
                self.accounts.is_allowed = lambda _: True; self.reservations.allow_live = True
                self.reservations.guard = lambda: True; self.service.quiet_window = None
        self.assertEqual(self.school.cancels, [])

    def test_wrong_identity_changed_interval_and_started_record_rejected(self):
        for record in (replace(self.record, student_id='other'), replace(self.record, status=128),
                       replace(self.record, begin='2026-11-06 07:30:00')):
            self.school.records = [record]
            with self.assertRaises(AppError): self.preview()
        self.school.records = [self.record]; preview = self.preview()
        self.school.records = [replace(self.record, end='2026-11-06 13:00:00')]
        with self.assertRaises(AppError): self.submit(preview)
        self.assertEqual(self.school.cancels, [])

    def test_status_is_owned_and_read_only(self):
        result = self.submit()
        other = self.accounts.verify_and_save('fixture02', 'pass', 'Other')
        with self.assertRaises(AppError): self.service.get(other.id, result['cancel_id'])
        for _ in range(3): self.service.get(self.account.id, result['cancel_id'])
        self.assertEqual(len(self.school.cancels), 1)


class CancellationTransportTests(unittest.TestCase):
    def test_exact_official_endpoint_payload_and_live_guard(self):
        from local_app.school import CampusClient
        from support.school_fixture import Transport, login_steps
        transport = Transport(login_steps() + [('POST', '/ic-web/reserve/delete', {'code': 0}, 200, {})])
        client = CampusClient('fixture01', 'pass', transport=transport)
        client.login()
        with self.assertRaises(AppError): client.cancel('fixture-uuid')
        client.allow_live = True; client.cancellation_guard = lambda: True
        self.assertEqual(client.cancel('fixture-uuid')['code'], 0)
        self.assertEqual(transport.calls[-1][2]['json'], {'uuid': 'fixture-uuid'})
        self.assertFalse(transport.calls[-1][2]['allow_redirects'])
