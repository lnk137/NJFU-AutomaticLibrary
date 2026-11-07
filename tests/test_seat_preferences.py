import copy
import sys
import threading
import time
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Seat, resolve_run, validate_plan
from local_app.auth import AuthService, COOKIE_NAME
from local_app.config import load_settings
from local_app.reservations import ReservationService
from local_app.scheduler import LocalScheduler
from local_app.web import create_app
from support.account_fixture import services
from support.auth_fixture import grant
from support.mongo_fixture import MongoFixture

ROOT = Path(__file__).resolve().parents[1]
PREFERENCES = {'favorites': [{'seat_id': '1', 'tags': [' 自定义 ', '自定义', '安静']}],
               'groups': [{'id': 'my-group', 'name': ' 我的座位 ', 'seat_ids': ['2', '1']}]}


class PreferenceRulesTests(unittest.TestCase):
    def normalize(self, data):
        from local_app.seat_preferences import validate_preferences
        return validate_preferences(data, {'1': Seat('1', 'A001', '2F'), '2': Seat('2', 'A002', '2F')})

    def test_custom_tags_normalize_without_inventing_labels_or_changing_order(self):
        original = copy.deepcopy(PREFERENCES)
        result = self.normalize(original)
        self.assertEqual(result['favorites'], [{'seat_id': '1', 'tags': ['自定义', '安静']}])
        self.assertEqual(result['groups'], [{'id': 'my-group', 'name': '我的座位', 'seat_ids': ['2', '1']}])
        self.assertEqual(original, PREFERENCES)
        self.assertEqual(self.normalize({'favorites': [], 'groups': []}), {'favorites': [], 'groups': []})

    def test_duplicate_unknown_and_empty_group_seats_are_rejected(self):
        for seats in ([], ['1', '1'], ['missing'], ['1', '2', '1', '2', '1', '2']):
            with self.subTest(seats=seats), self.assertRaises(AppError):
                self.normalize({'favorites': [], 'groups': [{'id': 'g', 'name': '组', 'seat_ids': seats}]})

    def test_duplicate_ids_names_and_favorites_are_rejected(self):
        group = {'id': 'g', 'name': '组', 'seat_ids': ['1']}
        invalid = [{'favorites': [{'seat_id': '1', 'tags': []}] * 2, 'groups': []},
                   {'favorites': [], 'groups': [group, group]},
                   {'favorites': [], 'groups': [group, group | {'id': 'g2'}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AppError):
                self.normalize(value)

    def test_partial_or_malformed_metadata_cannot_clear_saved_fields(self):
        for value in (None, [], {}, {'favorites': []}, {'favorites': [], 'groups': [], 'enabled': True},
                      {'favorites': [{'seat_id': '1', 'tags': '安静'}], 'groups': []},
                      {'favorites': [{'seat_id': 'missing', 'tags': []}], 'groups': []},
                      {'favorites': [], 'groups': [{'id': [], 'name': '组', 'seat_ids': ['1']}]}):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.normalize(value)

    def test_tag_and_group_name_limits_are_enforced(self):
        invalid = [{'favorites': [{'seat_id': '1', 'tags': [str(i) for i in range(6)]}], 'groups': []},
                   {'favorites': [{'seat_id': '1', 'tags': ['x' * 21]}], 'groups': []},
                   {'favorites': [], 'groups': [{'id': 'g', 'name': ' ', 'seat_ids': ['1']}]},
                   {'favorites': [], 'groups': [{'id': 'g', 'name': 'x' * 41, 'seat_ids': ['1']}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AppError):
                self.normalize(value)

    def test_retired_references_can_be_retained_or_removed_but_not_newly_introduced(self):
        from local_app.seat_preferences import validate_preferences
        catalog = {'1': Seat('1', 'A001', '2F')}
        old = {'favorites': [{'seat_id': 'retired', 'tags': ['原标签']}],
               'groups': [{'id': 'g', 'name': '旧组', 'seat_ids': ['retired', '1']}]}
        candidate = {'favorites': [], 'groups': old['groups']}
        self.assertEqual(validate_preferences(candidate, catalog, previous=old), candidate)
        with self.assertRaises(AppError):
            validate_preferences(old | {'groups': old['groups'] + [{'id': 'new', 'name': '新组', 'seat_ids': ['retired']}]}, catalog, previous=old)


class PreferenceServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.service, self.catalog = services(self.db)
        self.account = self.service.verify_and_save('99000001', 'pass', 'A')
        self.other = self.service.verify_and_save('99000002', 'pass', 'B')
        self.plan = dict(seat_ids=['1'], mode='day', day_offset=1, begin='09:00', end='12:00',
                         trigger='07:00:00', enabled=True)
        self.account = self.service.save_plan(self.account.id, self.plan, self.account.revision)
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'date_exceptions': {'2026-11-01': {'action': 'skip'}}}})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def save(self, preferences=PREFERENCES, revision=0):
        return self.service.save_seat_preferences(self.account.id, preferences, revision)

    def test_get_empty_library_never_migrates_or_writes_an_account(self):
        before = self.store.get_account(self.account.id)
        result = self.service.seat_preferences(self.account.id)
        self.assertEqual(result, {'account_id': self.account.id, 'revision': 0, 'favorites': [], 'groups': []})
        self.assertEqual(self.store.get_account(self.account.id), before)

    def test_metadata_save_preserves_plan_credentials_exceptions_and_prepared_task(self):
        before = self.store.get_account(self.account.id)
        control = self.store.get_account(self.other.id)
        scheduler = LocalScheduler(self.service, ReservationService(self.service, self.store, False), self.store)
        try:
            run = resolve_run(self.account.id, self.account.revision, validate_plan(self.plan, self.catalog), date(2026, 10, 24))
            self.store.ensure_run(run)
            history = self.store.get_run(run.key)
            self.assertTrue(scheduler._valid(run))
            result = self.save()
            after = self.store.get_account(self.account.id)
            self.assertEqual(result['revision'], 1)
            for field in ('revision', 'credential_revision', 'secret', 'plan', 'date_exceptions', 'snapshot'):
                self.assertEqual(after.get(field), before.get(field))
            self.assertTrue(scheduler._valid(run))
            self.assertEqual(self.store.get_run(run.key), history)
            self.assertEqual(self.store.get_account(self.other.id), control)
        finally:
            scheduler.stop()

    def test_library_has_independent_compare_and_swap(self):
        first = self.save()
        before = self.store.get_account(self.account.id)
        with self.assertRaises(AppError):
            self.save({'favorites': [], 'groups': []}, 0)
        self.assertEqual(self.store.get_account(self.account.id), before)
        saved = self.service.save_plan(self.account.id, self.plan | {'seat_ids': ['2']}, self.account.revision)
        changed = self.save({'favorites': [], 'groups': []}, first['revision'])
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(self.store.get_account(self.account.id)['revision'], saved.revision)
        self.assertEqual(self.store.get_account(self.account.id)['plan']['seat_ids'], ['2'])

    def test_group_edit_or_delete_does_not_change_a_saved_plan_snapshot(self):
        first = self.save()
        plan = self.service.save_plan(self.account.id, self.plan | {'seat_ids': ['2', '1']}, self.account.revision)
        second = self.save({'favorites': [], 'groups': [{'id': 'my-group', 'name': '新名字', 'seat_ids': ['1', '2']}]}, first['revision'])
        self.save({'favorites': [], 'groups': []}, second['revision'])
        self.assertEqual(self.store.get_account(self.account.id)['plan'], plan.plan)
        view = asdict(self.store.list_accounts()[0])
        self.assertIn('seat_preferences_revision', view)
        self.assertIn('seat_preferences', view)

    def test_metadata_does_not_wait_for_a_booking_account_lock(self):
        entered, release = threading.Event(), threading.Event()
        def hold_lock():
            with self.service.account_lock(self.account.id):
                entered.set()
                release.wait(2)
        worker = threading.Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.save()
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            worker.join(3)

    def test_invalid_metadata_or_revision_never_changes_saved_data(self):
        self.save()
        before = self.store.get_account(self.account.id)
        for value, revision in (({'favorites': []}, 1), (PREFERENCES, True), (PREFERENCES, -1)):
            with self.subTest(value=value, revision=revision), self.assertRaises(AppError):
                self.save(value, revision)
            self.assertEqual(self.store.get_account(self.account.id), before)

    def test_owner_api_is_private_and_needs_no_root_review(self):
        auth = AuthService(self.store, self.service)
        grant(auth, ['99000001', '99000002'])
        app = create_app(load_settings(ROOT), self.service, self.store, self.catalog, lambda: {}, auth=auth)
        client = app.test_client()
        origin = 'http://127.0.0.1:5003'
        path = f'/api/local/accounts/{self.account.id}/seat-preferences'
        client.set_cookie(COOKIE_NAME, auth._issue('student', account_id=self.account.id, student_id='99000001'), domain='127.0.0.1')
        token = client.get('/api/local/bootstrap', base_url=origin).json['csrf']
        headers = {'Origin': origin, 'X-CSRF-Token': token}
        self.assertEqual(client.get(path, base_url=origin).status_code, 200)
        result = client.put(path, json={'preferences': PREFERENCES, 'expected_revision': 0}, headers=headers, base_url=origin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['revision'], 1)
        self.assertNotIn('secret', str(result.json))
        foreign = f'/api/local/accounts/{self.other.id}/seat-preferences'
        self.assertEqual(client.get(foreign, base_url=origin).status_code, 404)
        self.assertEqual(client.put(foreign, json={'preferences': PREFERENCES, 'expected_revision': 0}, headers=headers, base_url=origin).status_code, 404)
        self.assertEqual(client.put(path, json={'preferences': PREFERENCES, 'expected_revision': 1}, headers={'Origin': origin}, base_url=origin).status_code, 403)
        client.set_cookie(COOKIE_NAME, '', domain='127.0.0.1')
        self.assertEqual(client.get(path, base_url=origin).status_code, 401)

    def test_retired_favorite_and_multiple_groups_can_be_repaired_individually(self):
        old = {'favorites': [{'seat_id': '2', 'tags': ['旧标签']}], 'groups': [
            {'id': 'g1', 'name': '旧组一', 'seat_ids': ['2', '1']}, {'id': 'g2', 'name': '旧组二', 'seat_ids': ['2']}]}
        first = self.save(old)
        before = self.store.get_account(self.account.id)
        self.catalog.pop('2')
        second = self.save({'favorites': [], 'groups': old['groups']}, first['revision'])
        third = self.save({'favorites': [], 'groups': [old['groups'][1]]}, second['revision'])
        last = self.save({'favorites': [], 'groups': [{'id': 'g2', 'name': '修正后的组', 'seat_ids': ['1']}]}, third['revision'])
        self.assertEqual(last['groups'][0]['seat_ids'], ['1'])
        after = self.store.get_account(self.account.id)
        for field in ('plan', 'revision', 'credential_revision', 'secret', 'date_exceptions'):
            self.assertEqual(after[field], before[field])


if __name__ == '__main__':
    unittest.main()
