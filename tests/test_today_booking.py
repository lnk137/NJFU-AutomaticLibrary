"""Manual today booking crosses the real local API with an isolated school."""
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from support.account_fixture import School
from support.mongo_fixture import MongoFixture
from support.auth_fixture import grant, root_session
from local_app.models import SchoolRecord, Submission, ZONE


class TodaySchool(School):
    active_statuses = frozenset({1027})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records, self.posts = [], []
        self.uncertain = False

    def prepare_booking(self):
        pass

    def query(self, *args):
        return list(self.records)

    def submit(self, run, seat):
        assert self.allow_live
        self.posts.append(seat)
        if self.uncertain:
            return Submission('uncertain')
        self.records.append(SchoolRecord('fixture', self.student_id, seat,
                            f'{run.target_date} {run.begin}:00', f'{run.target_date} {run.end}:00', 1027))
        return Submission('accepted', code=0, sent=True)


class TodayApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def setUp(self):
        from local_app.config import load_settings
        from local_app.runtime import create_runtime
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-today-test-')
        self.db = self.mongo.database()
        settings = load_settings(Path(__file__).resolve().parents[1], {
            'data_dir': Path(self.temp.name), 'mongo_uri': self.mongo.uri,
            'database_name': self.db.name, 'allow_live': True})
        self.runtime = create_runtime(settings, client_factory=TodaySchool)
        grant(self.runtime.auth,['fixture01','fixture02'])
        self.now = datetime(2026, 10, 23, 8, 10, tzinfo=ZONE).timestamp()
        self.runtime.reservations.wall_clock = lambda: self.now
        self.account = self.runtime.accounts.verify_and_save('fixture01', 'pass', 'Fixture', None)
        self.school = self.runtime.accounts.client(self.account.id)
        self.seat = next(iter(self.runtime.accounts.catalog))
        self.client = self.runtime.app.test_client()
        root_session(self.runtime.auth,self.client)
        self.origin = 'http://127.0.0.1:5003'
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']

    def tearDown(self):
        self.runtime.close()
        self.mongo.client.drop_database(self.db.name)
        self.temp.cleanup()

    def post(self, suffix, data, account=None):
        return self.client.post(f'/api/local/accounts/{account or self.account.id}/today/{suffix}', json=data,
                                headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)

    def preview(self, **changes):
        data = {'expected_revision': self.account.revision, 'date': '2026-10-23',
                'seat_ids': [self.seat], 'begin': '09:00', 'end': '20:00'} | changes
        response = self.post('preview', data)
        self.assertEqual(response.status_code, 200)
        return response.json

    def commit(self, preview):
        return self.post('submit', {'preview_token': preview['preview_token']})

    def test_preview_does_not_submit_or_change_the_scheduled_plan(self):
        preview = self.preview()
        self.assertEqual((preview['date'], preview['begin'], preview['end']), ('2026-10-23', '09:00', '20:00'))
        self.assertEqual(self.school.posts, [])
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)
        self.assertIsNone(self.runtime.store.get_account(self.account.id)['plan'])

    def test_explicit_confirmation_submits_once_and_records_manual_success(self):
        preview = self.preview()
        first, second = self.commit(preview), self.commit(preview)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json['result']['status'], 'success')
        self.assertEqual(self.school.posts, [self.seat])
        self.assertEqual(first.json['run_id'], second.json['run_id'])
        row = self.runtime.store.get_run(first.json['run_id'])
        self.assertEqual(row['spec']['kind'], 'manual')
        self.assertEqual(row['target_date'], '2026-10-23')
        self.assertIsNone(self.runtime.store.get_account(self.account.id)['plan'])

    def test_closed_live_gate_blocks_confirmation_but_allows_preview(self):
        self.runtime.reservations.allow_live = False
        preview = self.preview()
        self.assertEqual(self.commit(preview).status_code, 403)
        self.assertEqual(self.school.posts, [])

    def test_paused_clock_blocks_confirmation(self):
        preview = self.preview()
        self.runtime.scheduler.clock_ok = False
        self.assertEqual(self.commit(preview).status_code, 503)
        self.assertEqual(self.school.posts, [])

    def test_expired_or_tampered_preview_never_submits(self):
        preview = self.preview()
        bad = dict(preview)
        token = preview['preview_token']
        bad['preview_token'] = token[:-1] + ('a' if token[-1] != 'a' else 'b')
        self.assertEqual(self.commit(bad).status_code, 400)
        self.now += 121
        self.assertEqual(self.commit(preview).status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_preview_cannot_be_used_for_another_account(self):
        preview = self.preview()
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        response = self.post('submit', {'preview_token': preview['preview_token']}, account=other.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.school.posts, [])

    def test_account_revision_change_invalidates_preview(self):
        preview = self.preview()
        self.runtime.accounts.verify_and_save('fixture01', 'pass', 'Changed', self.account.revision)
        self.assertEqual(self.commit(preview).status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_friday_past_time_short_slot_and_wrong_date_are_rejected(self):
        for changes in ({'begin': '08:00'}, {'begin': '19:00'},
                        {'date': '2026-10-24'}, {'begin': '09:10'}):
            data = {'expected_revision': 1, 'date': '2026-10-23', 'seat_ids': [self.seat],
                    'begin': '09:00', 'end': '20:00'} | changes
            with self.subTest(changes=changes):
                self.assertEqual(self.post('preview', data).status_code, 400)
        self.assertEqual(self.school.posts, [])

    def test_friday_preview_shows_capped_end_before_explicit_confirmation(self):
        preview=self.preview(end='22:00')
        self.assertEqual(preview['end'],'20:00')
        self.assertEqual(self.school.posts,[])
        self.assertEqual(self.commit(preview).json['result']['status'],'success')
        self.assertEqual(self.school.records[0].end,'2026-10-23 20:00:00')

    def test_overlapping_school_record_blocks_preview(self):
        self.school.records = [SchoolRecord('exists', 'fixture01', self.seat,
                               '2026-10-23 10:00:00', '2026-10-23 12:00:00', 1027)]
        response = self.post('preview', {'expected_revision': 1, 'date': '2026-10-23',
                             'seat_ids': [self.seat], 'begin': '09:00', 'end': '20:00'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_uncertain_result_blocks_a_new_manual_attempt(self):
        preview = self.preview()
        self.school.uncertain = True
        self.assertEqual(self.commit(preview).json['result']['status'], 'uncertain')
        response = self.post('preview', {'expected_revision': 1, 'date': '2026-10-23',
                             'seat_ids': [self.seat], 'begin': '09:00', 'end': '20:00'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.school.posts, [self.seat])

    def test_record_created_after_preview_blocks_confirmation(self):
        preview = self.preview()
        self.school.records = [SchoolRecord('external', 'fixture01', self.seat,
                               '2026-10-23 10:00:00', '2026-10-23 12:00:00', 1027)]
        self.assertEqual(self.commit(preview).status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_today_preview_keeps_future_records_in_the_shared_snapshot(self):
        future = SchoolRecord('future', 'fixture01', self.seat,
                             '2026-10-24 09:00:00', '2026-10-24 12:00:00', 1027)
        self.school.query = lambda begin, end: [future] if end >= date(2026, 10, 24) else []
        self.preview()
        snapshot = self.runtime.accounts.snapshot(self.account.id)
        self.assertEqual([r['id'] for r in snapshot['records']], ['future'])
        self.assertEqual(self.school.posts, [])

    def test_slow_confirmation_precheck_cannot_outlive_preview(self):
        preview = self.preview()
        def slow_query(*args):
            self.now += 121
            return []
        self.school.query = slow_query
        response = self.commit(preview)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['result']['status'], 'skipped')
        self.assertEqual(self.school.posts, [])

    def test_concurrent_confirmations_use_one_durable_attempt(self):
        preview = self.preview()
        def confirm(_):
            with self.runtime.app.test_client() as client:
                root_session(self.runtime.auth,client)
                return client.post(f'/api/local/accounts/{self.account.id}/today/submit',
                    json={'preview_token': preview['preview_token']},
                    headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf},
                    base_url=self.origin)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(confirm, range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual([r.json['result']['status'] for r in responses], ['success', 'success'])
        self.assertEqual(self.school.posts, [self.seat])
        self.assertEqual(self.db.campus_runs.count_documents({}), 1)

    def test_manual_history_and_existing_scheduled_plan_remain_separate(self):
        saved = self.runtime.accounts.save_plan(self.account.id, {
            'mode': 'day', 'time_mode': 'full_day', 'seat_ids': [self.seat], 'day_offset': 1,
            'begin': '07:30', 'end': '22:00', 'trigger': '08:28:00', 'enabled': True}, 1)
        before = self.runtime.store.get_account(self.account.id)['plan']
        preview = self.preview(expected_revision=saved.revision)
        result = self.commit(preview).json
        history = self.client.get('/api/local/runs', base_url=self.origin).json
        self.assertEqual(history['items'][0]['spec']['kind'], 'manual')
        self.assertEqual(history['items'][0]['status'], 'success')
        self.assertEqual(self.runtime.store.get_account(self.account.id)['plan'], before)
        self.assertNotEqual(result['run_id'], self.account.id + ':2026-10-23')
