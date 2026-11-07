"""Regression boundaries from the audit; real services, fictional school only."""
import base64
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import threading
import time
import unittest

from Crypto.PublicKey import RSA

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Identity, RunSpec, ZONE
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from local_app.school import CampusClient
from local_app.today import TodayBookingService
from support.account_fixture import services
from support.mongo_fixture import MongoFixture
from test_booking_protocol import BookingTransport


class AuditBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.addClassCleanup(cls.mongo.close)
        cls.key = base64.b64encode(RSA.generate(1024).public_key().export_key(format='DER')).decode()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, _ = services(self.db)
        self.addCleanup(self.mongo.client.drop_database, self.db.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.accounts.close)

    def final_guard(self, change, *, manual=False):
        now = [datetime(2026, 10, 25, 7, tzinfo=ZONE).timestamp()]
        base = now[0]
        active, final = [True], [False]
        transport = BookingTransport(self.key, reply={'code': 9, 'message': 'fixture rejected'})

        class School(CampusClient):
            def __init__(client, student, password, **kwargs):
                super().__init__(student, password, transport=transport, **kwargs)
            def login(client):
                client.identity = Identity(client.student_id, 'Fixture', 123)
                return client.identity
            def _request(client, method, path, **kwargs):
                final[0] = method == 'POST'
                try:return super()._request(method, path, **kwargs)
                finally:final[0] = False

        self.accounts.client_factory = School
        a = self.accounts.verify_and_save('fixture01', 'pass')
        a = self.accounts.save_plan(a.id, {'seat_ids': ['1'], 'day_offset': 1, 'mode': 'day',
            'begin': '09:00', 'end': '12:00', 'trigger': '07:00:00', 'enabled': True}, a.revision)
        run = RunSpec(a.id, a.revision, '2026-10-26', '09:00', '12:00', ('1',), base)
        now[0] = base + 29.9
        if manual:
            run = replace(run, target_date='2026-10-25', kind='manual', operation_id='fixture',
                          expires_at=base+20)
            now[0] = base + 19.9
        reservations = ReservationService(self.accounts, self.store, True, wall_clock=lambda: now[0])
        reservations.guard = lambda: active[0]
        def permission(student):
            if final[0]:
                if change == 'time':now[0] += .2
                elif change == 'pause':active[0] = False
                elif change == 'live':reservations.allow_live = False
            return True
        self.accounts.is_allowed = permission
        self.accounts.client(a.id).prepare_submission(run, '1')
        result = reservations.execute(run)
        self.assertFalse(any(method == 'POST' for method, _, _ in transport.calls))
        self.assertNotEqual(result.status, 'success')
        self.assertEqual(len(self.store.get_run(run.key).get('attempts', [])), 1)
        self.assertIs(self.store.get_run(run.key)['attempts'][0]['sent'], False)

    def test_deadline_crossed_during_final_permission_read_cannot_post(self):
        self.final_guard('time')

    def test_pause_during_final_permission_read_cannot_post(self):
        self.final_guard('pause')

    def test_live_gate_closed_during_final_permission_read_cannot_post(self):
        self.final_guard('live')

    def test_manual_preview_expiring_during_final_permission_read_cannot_post(self):
        self.final_guard('time', manual=True)

    def test_weekend_evening_preview_uses_actual_day_without_changing_plan(self):
        a = self.accounts.verify_and_save('fixture01', 'pass')
        reservations = ReservationService(self.accounts, self.store, False)
        today = TodayBookingService(self.accounts, reservations, self.store)
        self.addCleanup(today.close)
        for day in (24, 25):
            reservations.wall_clock = lambda: datetime(2026, 10, day, 14, tzinfo=ZONE).timestamp()
            for begin in ('18:30', '19:00'):
                with self.subTest(day=day, begin=begin):
                    preview = today.preview(a.id, {'date': f'2026-10-{day}', 'expected_revision': a.revision,
                        'seat_ids': ['1'], 'begin': begin, 'end': '22:00'})
                    self.assertEqual((preview['begin'], preview['end']), (begin, '22:00'))
        self.assertIsNone(self.store.get_account(a.id)['plan'])
        self.assertEqual(self.store.runs.count_documents({}), 0)

    def test_friday_today_caps_end_and_rejects_short_period(self):
        a = self.accounts.verify_and_save('fixture01', 'pass')
        reservations = ReservationService(self.accounts, self.store, False,
            wall_clock=lambda: datetime(2026, 10, 23, 14, tzinfo=ZONE).timestamp())
        today = TodayBookingService(self.accounts, reservations, self.store)
        self.addCleanup(today.close)
        data = {'date': '2026-10-23', 'expected_revision': a.revision, 'seat_ids': ['1'], 'end': '22:00'}
        self.assertEqual(today.preview(a.id, data | {'begin': '18:00'})['end'], '20:00')
        with self.assertRaises(AppError):today.preview(a.id, data | {'begin': '19:00'})

    def test_blocked_poll_is_not_ready_and_recovers_when_poll_returns(self):
        now = [time.time()]
        reservations = ReservationService(self.accounts, self.store, True)
        scheduler = LocalScheduler(self.accounts, reservations, self.store,
                                   wall_clock=lambda: now[0], monotonic=lambda: now[0])
        entered, release = threading.Event(), threading.Event()
        original = scheduler.refresh
        def blocked():
            entered.set();release.wait(3);original()
        scheduler.refresh = blocked
        try:
            scheduler.start();self.assertTrue(entered.wait(1))
            now[0] += 6
            status = scheduler.status()
            self.assertFalse(status['ready_to_submit'])
            self.assertTrue(status['scheduler_warning'])
            release.set()
            until = time.monotonic()+2
            while not scheduler.status()['ready_to_submit'] and time.monotonic()<until:time.sleep(.005)
            self.assertTrue(scheduler.status()['ready_to_submit'])
            self.assertTrue(scheduler.clock_ok)
        finally:
            release.set();scheduler.stop()

    def test_http_timestamp_excludes_final_guard_wait(self):
        observed = {}
        class Transport(BookingTransport):
            def request(transport, method, url, **kwargs):
                if method == 'POST':observed['post'] = time.time()
                return super().request(method, url, **kwargs)
        client = CampusClient('fixture01', 'pass', transport=Transport(self.key), allow_live=True)
        self.addCleanup(client.close)
        client.identity = Identity('fixture01', 'Fixture', 123)
        run = RunSpec('fixture', 1, '2026-10-26', '09:00', '12:00', ('1',), 0)
        client.prepare_submission(run, '1')
        def guard():
            time.sleep(.06);observed['guard_done'] = time.time();return True
        client.submission_guard = guard
        response = client.submit(run, '1')
        self.assertEqual(response.outcome, 'accepted')
        self.assertGreaterEqual(response.timings['request_started_at'], observed['guard_done'])
        self.assertLess(abs(observed['post']-response.timings['request_started_at']), .02)
        self.assertGreaterEqual(response.timings['guard_ms'], 50)
        self.assertLess(response.timings['request_ms'], 40)
        self.assertEqual(response.timings['timing_version'], 2)

    def test_existing_run_fast_path_keeps_state_without_redundant_writes(self):
        from pymongo import MongoClient, monitoring
        from local_app.store import LocalStore
        commands = []
        class Listener(monitoring.CommandListener):
            def started(self, event):commands.append(event.command_name)
            def succeeded(self, event):pass
            def failed(self, event):pass
        with MongoClient(self.mongo.uri, event_listeners=[Listener()]) as client:
            store = LocalStore(client[self.db.name])
            run = RunSpec('fixture', 1, '2026-10-26', '09:00', '12:00', ('1',), 0)
            original = store.ensure_run(run)
            commands.clear()
            self.assertEqual(store.ensure_run(run), original)
            self.assertEqual(commands, ['find'], 'Existing run still performs writes on the first-POST path')
            store.transition_run(run.key, ('queued',), 'submitting', {'attempted_seat_id': '1'})
            self.assertEqual(store.ensure_run(replace(run, revision=2))['status'], 'submitting')
            self.assertEqual(store.get_run(run.key)['spec']['revision'], 1)

    def test_dashboard_keeps_timing_boundary_version_and_local_guard_cost(self):
        from local_app.dashboard import safe_attempts
        timing = {'timing_version': 2, 'guard_ms': 60, 'request_started_at': 10,
                  'request_finished_at': 11, 'request_ms': 1000, 'prepare_ms': 2,
                  'password': 'fixture-never-report'}
        result = safe_attempts({'attempts': [{'sent': True, 'outcome': 'accepted', 'timings': timing}]})
        self.assertEqual(result[0]['timings']['timing_version'], 2)
        self.assertEqual(result[0]['timings']['guard_ms'], 60)
        self.assertEqual(result[0]['timings']['request_finished_at'], 11)
        self.assertNotIn('password', result[0]['timings'])

    def test_failed_warmup_after_release_goes_to_submit_without_another_warmup(self):
        from concurrent.futures import Future
        from local_app.models import RunResult
        due = time.time()-.01
        run = RunSpec('fixture', 1, '2026-10-26', '09:00', '12:00', ('1',), due)
        reservations = ReservationService(self.accounts, self.store, True)
        submitted = threading.Event()
        def execute(spec):
            submitted.set();return RunResult('skipped', 'fixture', [])
        reservations.execute = execute
        scheduler = LocalScheduler(self.accounts, reservations, self.store)
        ready = Future();ready.set_result(True)
        scheduler.jobs[(run.key,run.revision)] = {'run': run, 'prep': ready, 'warm': ready, 'submit': None,
            'timings': {'warm': {'refresh_needed': True, 'attempt_count': 1}}}
        try:
            scheduler.tick()
            self.assertTrue(submitted.wait(.5), 'A failed prewarm caused another warmup after release')
        finally:scheduler.stop()
