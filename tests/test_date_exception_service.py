import copy
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.auth import AuthService, COOKIE_NAME
from local_app.config import load_settings
from local_app.models import AppError, RunSpec, ZONE, resolve_run, validate_plan
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from local_app.web import create_app
from support.account_fixture import services
from support.auth_fixture import grant, root_session
from support.mongo_fixture import MongoFixture

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 24, 6, tzinfo=ZONE)


class DateExceptionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.service, self.catalog = services(self.db)
        self.service.wall_clock = lambda: NOW.timestamp()
        self.account = self.service.verify_and_save('99000001', 'pass', 'A')
        self.other = self.service.verify_and_save('99000002', 'pass', 'B')
        self.plan = dict(seat_ids=['1'], day_offset=1, mode='day', begin='09:00', end='12:00',
                         trigger='07:00:00', enabled=True)
        self.account = self.service.save_plan(self.account.id, self.plan, self.account.revision)
        self.other = self.service.save_plan(self.other.id, self.plan, self.other.revision)
        self.scheduler = None

    def tearDown(self):
        if self.scheduler:
            self.scheduler.stop()
        self.service.close()
        self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def update(self, data, revision=None):
        return self.service.update_date_exceptions(self.account.id, data,
                                                  self.account.revision if revision is None else revision)

    def test_range_save_only_changes_own_exceptions_and_revision(self):
        before = self.store.get_account(self.account.id)
        other = self.store.get_account(self.other.id)
        changed = self.update({'action': 'skip_range', 'start_date': '2026-10-25', 'end_date': '2026-10-27'})
        after = self.store.get_account(self.account.id)
        self.assertEqual(set(changed.date_exceptions), {'2026-10-25', '2026-10-26', '2026-10-27'})
        self.assertEqual(changed.revision, self.account.revision + 1)
        for field in ('plan', 'secret', 'credential_revision', 'snapshot'):
            self.assertEqual(after.get(field), before.get(field))
        self.assertEqual(self.store.get_account(self.other.id), other)
        self.assertEqual(self.store.runs.count_documents({}), 0)

    def test_malformed_action_and_extreme_past_dates_are_validation_errors(self):
        before = self.store.get_account(self.account.id)
        for data in ({'action': []}, {'action': 'override', 'date': '0001-01-01',
                                     'begin': '09:00', 'end': '12:00', 'seat_ids': ['1']}):
            with self.subTest(data=data), self.assertRaises(AppError):
                self.update(data)
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_skip_next_uses_the_next_unreleased_effective_date(self):
        self.service.wall_clock = lambda: datetime(2026, 10, 24, 8, tzinfo=ZONE).timestamp()
        changed = self.update({'action': 'skip_next'})
        self.assertEqual(changed.date_exceptions, {'2026-10-26': {'action': 'skip'}})

    def test_override_is_saved_without_rewriting_the_weekly_plan(self):
        weekly = self.plan | {'mode': 'week', 'week_times': {'1': ['09:00', '12:00']}}
        self.account = self.service.save_plan(self.account.id, weekly, self.account.revision)
        changed = self.update({'action': 'override', 'date': '2026-10-25', 'begin': '14:00',
                               'end': '18:00', 'seat_ids': ['2', '1']})
        self.assertEqual(changed.plan['week_times'], {'1': ['09:00', '12:00']})
        self.assertEqual(changed.date_exceptions['2026-10-25']['seat_ids'], ['2', '1'])

    def test_remove_restores_base_and_leaves_other_exceptions(self):
        changed = self.update({'action': 'skip_range', 'start_date': '2026-10-25', 'end_date': '2026-10-27'})
        restored = self.update({'action': 'remove', 'start_date': '2026-10-25', 'end_date': '2026-10-26'}, changed.revision)
        self.assertEqual(restored.date_exceptions, {'2026-10-27': {'action': 'skip'}})
        run = resolve_run(restored.id, restored.revision, validate_plan(restored.plan, self.catalog),
                          date(2026, 10, 24), date_exceptions=restored.date_exceptions)
        self.assertEqual((run.begin, run.end, run.seat_ids), ('09:00', '12:00', ('1',)))

    def test_stale_revision_or_invalid_override_leaves_current_data_untouched(self):
        changed = self.update({'action': 'skip_next'})
        before = self.store.get_account(self.account.id)
        for data, revision in [({'action': 'skip_next'}, self.account.revision),
                               ({'action': 'override', 'date': '2026-10-26', 'begin': '09:00',
                                 'end': '10:00', 'seat_ids': ['1']}, changed.revision)]:
            with self.subTest(data=data), self.assertRaises(AppError):
                self.update(data, revision)
            self.assertEqual(self.store.get_account(self.account.id), before)

    def test_released_and_completed_dates_cannot_be_changed_or_replayed(self):
        self.service.wall_clock = lambda: datetime(2026, 10, 24, 8, tzinfo=ZONE).timestamp()
        old = RunSpec(self.account.id, self.account.revision, '2026-10-25', '09:00', '12:00', ('1',),
                      datetime(2026, 10, 24, 7, tzinfo=ZONE).timestamp())
        self.store.ensure_run(old)
        self.store.transition_run(old.key, ('queued',), 'success', {'attempted_seat_id': '1'})
        before = self.store.get_run(old.key)
        with self.assertRaises(AppError):
            self.update({'action': 'skip_range', 'start_date': '2026-10-25', 'end_date': '2026-10-25'})
        self.assertEqual(self.store.get_run(old.key), before)
        self.assertEqual(self.store.get_account(self.account.id)['revision'], self.account.revision)

    def test_different_future_day_edit_is_refused_in_active_submission_window(self):
        self.service.wall_clock = lambda: datetime(2026, 10, 24, 7, 0, 1, tzinfo=ZONE).timestamp()
        before = self.store.get_account(self.account.id)
        with self.assertRaises(AppError):
            self.update({'action': 'skip_range', 'start_date': '2026-10-27', 'end_date': '2026-10-27'})
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_midnight_removal_cannot_requeue_the_previous_release(self):
        self.account = self.service.save_plan(self.account.id, self.plan | {'trigger': '23:59:50'}, self.account.revision)
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'date_exceptions': {'2026-10-25': {'action': 'skip'}}}})
        self.service.wall_clock = lambda: datetime(2026, 10, 25, 0, 0, 5, tzinfo=ZONE).timestamp()
        before = self.store.get_account(self.account.id)
        with self.assertRaises(AppError):
            self.update({'action': 'remove', 'start_date': '2026-10-25', 'end_date': '2026-10-25'})
        self.assertEqual(self.store.get_account(self.account.id), before)
        self.scheduler = LocalScheduler(self.service, ReservationService(self.service, self.store, True),
                                        self.store, wall_clock=self.service.wall_clock)
        self.scheduler.refresh()
        self.assertEqual(self.store.runs.count_documents({'account_id': self.account.id}), 0)

    def test_commit_budget_is_protected_before_the_next_midnight_release(self):
        self.account = self.service.save_plan(self.account.id, self.plan | {'trigger': '00:00:00'}, self.account.revision)
        self.service.wall_clock = lambda: datetime(2026, 10, 24, 23, 59, 58, tzinfo=ZONE).timestamp()
        before = self.store.get_account(self.account.id)
        with self.assertRaises(AppError):
            self.update({'action': 'skip_range', 'start_date': '2026-10-27', 'end_date': '2026-10-27'})
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_slow_read_crossing_release_is_rechecked_before_the_write(self):
        for finish in [datetime(2026, 10, 24, 7, 0, 1, tzinfo=ZONE),
                       datetime(2026, 10, 24, 7, 1, 0, tzinfo=ZONE)]:
            clock = [datetime(2026, 10, 24, 6, 59, 56, tzinfo=ZONE).timestamp()]
            self.service.wall_clock = lambda: clock[0]
            original = self.store.runs.find_one
            def delayed(*args, **kwargs):
                result = original(*args, **kwargs)
                clock[0] = finish.timestamp()
                return result
            before = self.store.get_account(self.account.id)
            with self.subTest(finish=finish), patch.object(self.store.runs, 'find_one', side_effect=delayed):
                with self.assertRaises(AppError):
                    self.update({'action': 'skip_range', 'start_date': '2026-10-25', 'end_date': '2026-10-25'})
            self.assertEqual(self.store.get_account(self.account.id), before)

    def test_exception_invalidates_prepared_revision_without_changing_task_history(self):
        self.scheduler = LocalScheduler(self.service, ReservationService(self.service, self.store, False), self.store)
        run = resolve_run(self.account.id, self.account.revision, validate_plan(self.plan, self.catalog), NOW.date())
        self.store.ensure_run(run)
        self.assertTrue(self.scheduler._valid(run))
        self.update({'action': 'skip_next'})
        self.assertFalse(self.scheduler._valid(run))
        self.assertEqual(self.store.get_run(run.key)['status'], 'queued')

    def test_scheduler_queues_control_account_but_never_the_skipped_account(self):
        self.update({'action': 'skip_next'})
        clock = datetime(2026, 10, 24, 6, 59, tzinfo=ZONE).timestamp()
        self.scheduler = LocalScheduler(self.service, ReservationService(self.service, self.store, True),
                                        self.store, wall_clock=lambda: clock)
        self.scheduler.refresh()
        self.assertEqual({job['run'].account_id for job in self.scheduler.jobs.values()}, {self.other.id})
        self.assertEqual(self.store.runs.count_documents({'account_id': self.account.id}), 0)

    def test_long_leave_is_ready_and_dashboard_reads_do_not_mutate_data(self):
        from local_app.dashboard import build_dashboard
        self.update({'action': 'skip_range', 'start_date': '2026-10-25', 'end_date': '2026-11-13'})
        auth = AuthService(self.store, self.service)
        grant(auth, ['99000001', '99000002'])
        before = list(self.store.accounts.find().sort('_id', 1))
        result = build_dashboard(self.store, self.catalog, {}, now=NOW)
        own = next(row for row in result['accounts'] if row['account_id'] == self.account.id)
        self.assertEqual((own['check'], own['next_run']['target_date']), ('ready', '2026-11-14'))
        self.assertEqual(list(self.store.accounts.find().sort('_id', 1)), before)

    def test_ordinary_plan_save_preserves_date_exceptions_without_root_approval(self):
        changed = self.update({'action': 'skip_next'})
        saved = self.service.save_plan(changed.id, self.plan | {'begin': '10:00', 'end': '14:00'}, changed.revision)
        self.assertEqual(saved.date_exceptions, {'2026-10-25': {'action': 'skip'}})
        self.assertTrue(saved.plan['enabled'])

    def test_owner_web_save_needs_no_root_and_other_accounts_are_denied(self):
        auth = AuthService(self.store, self.service)
        grant(auth, ['99000001', '99000002'])
        app = create_app(load_settings(ROOT), self.service, self.store, self.catalog, lambda: {}, auth=auth)
        client = app.test_client()
        origin = 'http://127.0.0.1:5003'
        client.set_cookie(COOKIE_NAME, auth._issue('student', account_id=self.account.id, student_id='99000001'), domain='127.0.0.1')
        token = client.get('/api/local/bootstrap', base_url=origin).json['csrf']
        body = {'action': 'skip_next', 'expected_revision': self.account.revision}
        headers = {'Origin': origin, 'X-CSRF-Token': token}
        response = client.post(f'/api/local/accounts/{self.account.id}/date-exceptions', json=body,
                               headers=headers, base_url=origin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['account']['date_exceptions'], {'2026-10-25': {'action': 'skip'}})
        denied = client.post(f'/api/local/accounts/{self.other.id}/date-exceptions', json=body,
                             headers=headers, base_url=origin)
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn('secret', str(response.json))
        client.set_cookie(COOKIE_NAME, '', domain='127.0.0.1')
        self.assertEqual(client.post(f'/api/local/accounts/{self.account.id}/date-exceptions', json=body,
                                    headers=headers, base_url=origin).status_code, 401)


if __name__ == '__main__':
    unittest.main()
