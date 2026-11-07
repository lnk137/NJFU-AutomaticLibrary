"""Same-day batches cross the local API with a real isolated Mongo and school double."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from support.auth_fixture import grant, root_session
from support.mongo_fixture import MongoFixture
from test_today_booking import TodaySchool
from local_app.models import AppError, RunSpec, SchoolRecord, Submission, ZONE


class BatchSchool(TodaySchool):
    """No network: record the actual per-segment executor submissions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submissions = []
        self.replies = []
        self.after_submit = None
        self.after_query = None

    def query(self, *args):
        result = super().query(*args)
        if self.after_query:
            self.after_query()
        return result

    def submit(self, run, seat):
        assert self.allow_live
        assert self.submission_guard()
        self.submissions.append((run.begin, run.end, seat))
        reply = self.replies.pop(0) if self.replies else None
        if reply is not None:
            self.posts.append(seat)
            if isinstance(reply, Exception):
                raise reply
            result = reply
        else:
            result = super().submit(run, seat)
        if self.after_submit:
            self.after_submit(run, seat)
        return result


class TodayBatchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def setUp(self):
        from local_app.config import load_settings
        from local_app.runtime import create_runtime
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-today-batch-test-')
        self.releases = []
        self.no_http = patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP disabled'))
        self.no_http.start()
        self.db = self.mongo.database()
        self.settings = load_settings(Path(__file__).resolve().parents[1], {
            'data_dir': Path(self.temp.name), 'mongo_uri': self.mongo.uri,
            'database_name': self.db.name, 'allow_live': True})
        self.runtime = create_runtime(self.settings, client_factory=BatchSchool)
        grant(self.runtime.auth, ['fixture01', 'fixture02'])
        self.now = datetime(2026, 10, 23, 8, 10, tzinfo=ZONE).timestamp()
        self.runtime.reservations.wall_clock = lambda: self.now
        self.account = self.runtime.accounts.verify_and_save('fixture01', 'pass', 'Fixture', None)
        self.school = self.runtime.accounts.client(self.account.id)
        self.seats = list(self.runtime.accounts.catalog)[:5]
        self.client = self.runtime.app.test_client()
        root_session(self.runtime.auth, self.client)
        self.origin = 'http://127.0.0.1:5003'
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.runtime.close()
        self.no_http.stop()
        self.mongo.client.drop_database(self.db.name)
        self.temp.cleanup()

    def path(self, suffix='', account=None):
        return f'/api/local/accounts/{account or self.account.id}/today/batches' + suffix

    def post(self, suffix, data, *, account=None, client=None):
        return (client or self.client).post(self.path(suffix, account), json=data,
            headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)

    def segments(self):
        return [dict(begin='09:00', end='12:00', seat_ids=self.seats[:1]),
                dict(begin='14:00', end='18:00', seat_ids=self.seats[1:3]),
                dict(begin='18:00', end='20:00', seat_ids=self.seats[3:4])]

    def preview(self, segments=None, **changes):
        data = {'expected_revision': self.account.revision, 'date': '2026-10-23',
                'segments': self.segments() if segments is None else segments} | changes
        response = self.post('/preview', data)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def submit(self, preview):
        response = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertIn(response.status_code, (200, 202), response.json)
        return response.json

    def get(self, batch_id):
        response = self.client.get(self.path('/' + batch_id), base_url=self.origin)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def finished(self, batch):
        # Busy exhaustion and later accepted segments share account cooldowns;
        # keep this observation budget below the actual 30-second send window.
        deadline = time.monotonic() + 10
        while batch['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.005)
            batch = self.get(batch['batch_id'])
        self.assertNotEqual(batch['state'], 'running', batch)
        return batch

    def block_first_submission(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold(run, seat):
            if len(self.school.submissions) == 1:
                started.set()
                release.wait(5)
        self.school.after_submit = hold
        return started, release

    def assert_unsubmitted(self, batch, count=3):
        self.assertEqual(batch['state'], 'stopped', batch)
        self.assertEqual([s['status'] for s in batch['segments']], ['not_submitted'] * count)
        self.assertEqual(self.school.posts, [])

    def test_preview_is_read_only_and_normalizes_chronological_order(self):
        preview = self.preview(list(reversed(self.segments())))
        self.assertEqual([(s['index'], s['begin'], s['end']) for s in preview['segments']],
                         [(0, '09:00', '12:00'), (1, '14:00', '18:00'), (2, '18:00', '20:00')])
        self.assertEqual(preview['segments'][1]['seats'][0]['id'], self.seats[1])
        self.assertEqual(self.school.posts, [])
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)
        self.assertEqual(self.db.campus_today_batches.count_documents({}), 0)
        latest = self.client.get(self.path(), base_url=self.origin)
        self.assertEqual(latest.json, {'batch': None})

    def test_one_confirmation_runs_adjacent_segments_once_in_order(self):
        preview = self.preview(list(reversed(self.segments())))
        result = self.finished(self.submit(preview))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual([s['status'] for s in result['segments']], ['success'] * 3)
        self.assertEqual(self.school.submissions, [('09:00', '12:00', self.seats[0]),
            ('14:00', '18:00', self.seats[1]), ('18:00', '20:00', self.seats[3])])
        self.assertEqual(len({s['run_id'] for s in result['segments']}), 3)
        for segment in result['segments']:
            run = self.runtime.store.get_run(segment['run_id'])
            self.assertEqual(run['spec']['kind'], 'manual')
            self.assertEqual(set(run['spec']), {'account_id', 'revision', 'target_date', 'begin',
                'end', 'seat_ids', 'due_at', 'kind', 'operation_id', 'expires_at'})
        again = self.finished(self.submit(preview))
        self.assertEqual(again, result)
        self.assertEqual(self.db.campus_runs.count_documents({}), 3)

    def test_latest_means_last_confirmed_even_when_the_execution_clock_is_unchanged(self):
        with patch('local_app.today_batches.uuid.uuid4', return_value=uuid.UUID('f' * 32)):
            first = self.preview([self.segments()[0]])
        self.finished(self.submit(first))
        with patch('local_app.today_batches.uuid.uuid4', return_value=uuid.UUID('a' * 32)):
            second = self.preview([self.segments()[1]])
        self.finished(self.submit(second))
        latest = self.client.get(self.path(), base_url=self.origin).json['batch']
        self.assertEqual(latest['batch_id'], second['batch_id'])

    def test_executor_gate_error_leaves_no_running_segment_or_later_post(self):
        with patch.object(self.runtime.reservations, 'execute', side_effect=AppError('fixture_stop', '执行已停止', 409)):
            result = self.finished(self.submit(self.preview()))
        self.assert_unsubmitted(result)
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)

    def test_seven_valid_segments_are_supported_on_an_ordinary_day(self):
        self.now = datetime(2026, 10, 24, 6, tzinfo=ZONE).timestamp()
        boundaries = ['07:30', '09:30', '11:30', '13:30', '15:30', '17:30', '19:30', '21:30']
        segments = [dict(begin=begin, end=end, seat_ids=self.seats[:1])
                    for begin, end in zip(boundaries, boundaries[1:])]
        batch = self.finished(self.submit(self.preview(segments, date='2026-10-24')))
        self.assertEqual(batch['state'], 'completed', batch)
        self.assertEqual([s['status'] for s in batch['segments']], ['success'] * 7)

    def _assert_retry_record_stops(self, **changes):
        self.school.replies = [Submission('rejected', retryable=True, code=1, message='busy', sent=True)]
        def add_record(run, seat):
            record = dict(id='external', student_id='fixture01', seat_id=self.seats[4],
                begin='2026-10-23 12:00:00', end='2026-10-23 14:00:00', status=1027) | changes
            self.school.records = [SchoolRecord(**record)]
        self.school.after_submit = add_record
        batch = self.finished(self.submit(self.preview()))
        self.assertEqual(batch['state'], 'stopped', batch)
        self.assertEqual([s['status'] for s in batch['segments']], ['uncertain', 'not_submitted', 'not_submitted'])
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_manual_backup_stops_for_a_new_unknown_status(self):
        self._assert_retry_record_stops(status=9999)

    def test_manual_backup_stops_for_a_new_overlapping_interval(self):
        self._assert_retry_record_stops(begin='2026-10-23 10:00:00')

    def test_manual_backup_stops_for_a_new_wrong_identity(self):
        self._assert_retry_record_stops(student_id='other')

    def test_manual_backup_stops_for_a_new_malformed_interval(self):
        self._assert_retry_record_stops(end='invalid')

    def test_scheduled_backup_still_stops_for_non_overlapping_active_records(self):
        self.runtime.accounts.save_plan(self.account.id, {'mode': 'day', 'seat_ids': self.seats[:2],
            'day_offset': 1, 'begin': '14:00', 'end': '18:00', 'trigger': '08:28:00', 'enabled': True}, 1)
        self.school.records = [SchoolRecord('earlier', 'fixture01', self.seats[4],
            '2026-10-24 09:00:00', '2026-10-24 12:00:00', 1027)]
        self.school.replies = [Submission('rejected', retryable=True, code=1, message='busy', sent=True)]
        run = RunSpec(self.account.id, 2, '2026-10-24', '14:00', '18:00', tuple(self.seats[:2]), self.now)
        result = self.runtime.reservations.execute(run)
        self.assertEqual(result.status, 'uncertain')
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_invalid_segments_reject_the_whole_preview(self):
        invalid = [[], self.segments() * 3, [None], [dict(begin='09:10', end='12:00', seat_ids=self.seats[:1])],
            [dict(begin='08:00', end='12:00', seat_ids=self.seats[:1])],
            [dict(begin='19:00', end='22:00', seat_ids=self.seats[:1])],
            [dict(begin='09:00', end='10:00', seat_ids=self.seats[:1])],
            [dict(begin='09:00', end='12:00', seat_ids=[])],
            [dict(begin='09:00', end='12:00', seat_ids=[self.seats[0]] * 2)],
            [dict(begin='09:00', end='12:00', seat_ids=['unknown'])],
            [dict(begin='09:00', end='12:00', seat_ids=self.seats + ['unknown'])]]
        for segments in invalid:
            with self.subTest(segments=segments):
                response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23', 'segments': segments})
                self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)
        self.assertEqual(self.school.posts, [])

    def test_friday_effective_cap_is_shown_and_overlap_is_rejected(self):
        preview = self.preview([dict(begin='18:00', end='22:00', seat_ids=self.seats[:1])])
        self.assertEqual(preview['segments'][0]['end'], '20:00')
        overlap = self.segments()
        overlap[1]['begin'] = '11:30'
        response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23', 'segments': overlap})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.school.posts, [])

    def test_wrong_date_and_revision_never_create_a_batch(self):
        for changes, status in (({'date': '2026-10-24'}, 400), ({'expected_revision': 2}, 409),
                                ({'expected_revision': True}, 409)):
            response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23',
                'segments': self.segments()} | changes)
            self.assertEqual(response.status_code, status, response.json)
        self.assertEqual(self.db.campus_today_batches.count_documents({}), 0)

    def test_external_overlap_and_unverifiable_records_block_preview(self):
        for changes in ({}, {'status': 9999}, {'status': '1027'}, {'student_id': 'other'},
                        {'begin': 'bad-time'}, {'end': '2026-10-23 09:00:00'}):
            record = dict(id='external', student_id='fixture01', seat_id=self.seats[4],
                begin='2026-10-23 10:00:00', end='2026-10-23 12:00:00', status=1027) | changes
            self.school.records = [SchoolRecord(**record)]
            response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23', 'segments': self.segments()})
            self.assertIn(response.status_code, (409, 502), response.json)
        self.assertEqual(self.school.posts, [])

    def test_non_overlapping_records_and_future_snapshot_are_preserved(self):
        self.school.records = [SchoolRecord('adjacent', 'fixture01', self.seats[4],
            '2026-10-23 12:00:00', '2026-10-23 14:00:00', 1027),
            SchoolRecord('future', 'fixture01', self.seats[4],
            '2026-10-24 09:00:00', '2026-10-24 12:00:00', 1027)]
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'completed')
        snapshot = self.runtime.accounts.snapshot(self.account.id)
        self.assertIn('future', [r['id'] for r in snapshot['records']])

    def test_manual_backup_can_follow_another_segment_receipt(self):
        def reject_second(run, seat):
            if len(self.school.submissions) == 1:
                self.school.replies.append(Submission('rejected', retryable=True, code=1, message='busy', sent=True))
        self.school.after_submit = reject_second
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual([s['status'] for s in result['segments']], ['success'] * 3)
        self.assertEqual(self.school.posts, [self.seats[0], self.seats[1], self.seats[2], self.seats[3]])
        run = self.runtime.store.get_run(result['segments'][1]['run_id'])
        self.assertEqual(run['attempts'][0]['retry_check']['state'], 'clear')

    def test_clear_failure_continues_and_keeps_prior_success(self):
        def reject_second(run, seat):
            if len(self.school.submissions) == 1:
                self.school.replies.extend([Submission('rejected', retryable=True, code=1,
                    message='当前设备正在被预约，请稍后重试', sent=True)] * 4)
        self.school.after_submit = reject_second
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'failed', 'success'])
        self.assertEqual([r.begin for r in self.school.records], ['2026-10-23 09:00:00', '2026-10-23 18:00:00'])

    def _assert_global_rejection_stops(self, message, code=1):
        def reject_second(run, seat):
            if len(self.school.submissions) == 1:
                self.school.replies.append(Submission('rejected', code=code, message=message, sent=True))
        self.school.after_submit = reject_second
        batch = self.finished(self.submit(self.preview()))
        self.assertEqual(batch['state'], 'stopped', batch)
        self.assertEqual([s['status'] for s in batch['segments']], ['success', 'failed', 'not_submitted'])
        self.assertEqual(self.school.posts, self.seats[:2])
        self.assertEqual(len(self.school.records), 1)

    def test_rate_limiting_stops_later_segments_and_keeps_the_prior_receipt(self):
        self._assert_global_rejection_stops('操作太频繁')

    def test_unknown_rejection_stops_later_segments(self):
        self._assert_global_rejection_stops('unknown fixture rejection', code=54321)

    def test_school_global_booking_limit_stops_later_segments(self):
        self._assert_global_rejection_stops('您已有预约')

    def test_uncertain_submission_stops_remaining_segments_without_rollback(self):
        def uncertain_second(run, seat):
            if len(self.school.submissions) == 1:
                self.school.replies.append(Submission('uncertain', sent=True))
        self.school.after_submit = uncertain_second
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'stopped', result)
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'uncertain', 'not_submitted'])
        self.assertEqual(len(self.school.records), 1)
        self.assertEqual(self.school.posts, self.seats[:2])

    def _account_protected_digest(self, account_id):
        row = self.runtime.store.get_account(account_id)
        fields = ('revision', 'credential_revision', 'plan', 'date_exceptions',
                  'seat_preferences_revision', 'seat_preferences', 'secret', 'alias', 'identity')
        return hashlib.sha256(json.dumps({key: row.get(key) for key in fields},
            sort_keys=True, default=str).encode()).hexdigest()

    def _delayed_receipt_batch(self):
        self.account = self.runtime.accounts.save_plan(self.account.id, {'mode': 'day',
            'seat_ids': self.seats[:2], 'day_offset': 1, 'begin': '09:00', 'end': '12:00',
            'trigger': '08:28:00', 'enabled': True}, self.account.revision)
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        protected = self._account_protected_digest(self.account.id)
        other_before = hashlib.sha256(json.dumps(self.runtime.store.get_account(other.id),
            sort_keys=True, default=str).encode()).hexdigest()
        self.school.replies = [Submission('uncertain', sent=True)]
        segments = self.segments()
        segments[0]['seat_ids'] = self.seats[:2]
        preview = self.preview(segments)
        batch = self.finished(self.submit(preview))
        self.assertEqual([s['status'] for s in batch['segments']], ['uncertain', 'not_submitted', 'not_submitted'])
        self.school.records = [SchoolRecord('delayed-receipt', 'fixture01', self.seats[0],
            '2026-10-23 09:00:00', '2026-10-23 12:00:00', 1027)]
        return preview, batch, protected, other.id, other_before

    def _batch_read_views(self, preview):
        by_id = self.get(preview['batch_id'])
        latest = self.client.get(self.path(), base_url=self.origin)
        self.assertEqual(latest.status_code, 200, latest.json)
        duplicate = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertEqual(duplicate.status_code, 200, duplicate.json)
        return [by_id, latest.json['batch'], duplicate.json]

    def _assert_verified_receipt_projection(self, mode):
        preview, batch, protected, other_id, other_before = self._delayed_receipt_batch()
        persisted = deepcopy(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}))
        run_id = batch['segments'][0]['run_id']
        if mode == 'explicit':
            response = self.client.post('/api/local/runs/' + run_id + '/reconcile', json={},
                headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['result']['status'], 'success')
        else:
            response = self.client.post(f'/api/local/accounts/{self.account.id}/query', json={},
                headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)
            self.assertEqual(response.status_code, 200, response.json)
        verified = self.runtime.store.get_run(run_id)
        self.assertEqual(verified['status'], 'success')
        self.runtime.reservations.allow_live = False
        self.runtime.scheduler.clock_ok = False
        with patch.object(self.runtime.today.batches.pool, 'submit', side_effect=AssertionError('Read must not dispatch')):
            views = self._batch_read_views(preview)
        for view in views:
            self.assertEqual(view['state'], 'stopped', view)
            self.assertEqual([s['status'] for s in view['segments']], ['success', 'not_submitted', 'not_submitted'])
            self.assertEqual(view['segments'][0]['result'], verified['result'])
            self.assertEqual(view['segments'][0]['message'], verified['result']['message'])
            self.assertEqual(view['segments'][1:], batch['segments'][1:])
        self.assertEqual(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}), persisted)
        self.assertEqual(self._account_protected_digest(self.account.id), protected)
        self.assertEqual(hashlib.sha256(json.dumps(self.runtime.store.get_account(other_id),
            sort_keys=True, default=str).encode()).hexdigest(), other_before)
        self.assertEqual(self.school.posts, self.seats[:1])
        self.assertEqual(self.db.campus_runs.count_documents({}), 1)

    def test_explicit_reconcile_projects_verified_success_without_replaying_the_stopped_batch(self):
        self._assert_verified_receipt_projection('explicit')

    def test_account_query_projects_verified_success_without_replaying_the_stopped_batch(self):
        self._assert_verified_receipt_projection('account')

    def test_projection_ignores_foreign_or_mismatched_manual_run_bindings(self):
        preview, batch, _, other_id, _ = self._delayed_receipt_batch()
        run_id = batch['segments'][0]['run_id']
        self.assertEqual(self.runtime.reservations.reconcile(run_id).status, 'success')
        verified = self.runtime.store.get_run(run_id)
        mismatches = [('', 'account_id', other_id), ('', 'target_date', '2026-10-24'),
            ('spec', 'account_id', other_id), ('spec', 'revision', self.account.revision + 1),
            ('spec', 'target_date', '2026-10-24'), ('spec', 'begin', '09:30'), ('spec', 'end', '12:30'),
            ('spec', 'seat_ids', list(reversed(self.seats[:2]))), ('spec', 'operation_id', 'f' * 32),
            ('spec', 'kind', 'scheduled'), ('spec', 'revision', True)]
        persisted = deepcopy(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}))
        sentinel = 'FOREIGN-PROJECTION-RESULT'
        for section, key, value in mismatches:
            with self.subTest(section=section, key=key):
                row = deepcopy(verified)
                row['result'] = {'status': 'success', 'message': sentinel,
                    'records': [{'student_id': 'fixture02', 'id': sentinel}], 'verified_at': 'fixture'}
                (row[section] if section else row)[key] = value
                self.db.campus_runs.replace_one({'_id': run_id}, row)
                for view in self._batch_read_views(preview):
                    self.assertEqual(view['segments'][0]['status'], 'uncertain', view)
                    self.assertNotIn(sentinel, json.dumps(view))
                    self.assertNotIn('fixture02', json.dumps(view))
                self.assertEqual(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}), persisted)
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_projection_requires_the_deterministic_manual_run_key(self):
        preview, batch, _, _, _ = self._delayed_receipt_batch()
        run_id = batch['segments'][0]['run_id']
        self.assertEqual(self.runtime.reservations.reconcile(run_id).status, 'success')
        row = self.runtime.store.get_run(run_id)
        row['_id'] = run_id + '-unrelated'
        row['result']['message'] = 'UNRELATED-PROJECTION-RESULT'
        self.db.campus_runs.insert_one(row)
        self.db.campus_today_batches.update_one({'_id': preview['batch_id']}, {'$set': {'segments.0.run_id': row['_id']}})
        persisted = deepcopy(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}))
        for view in self._batch_read_views(preview):
            self.assertEqual(view['segments'][0]['status'], 'uncertain', view)
            self.assertNotIn('UNRELATED-PROJECTION-RESULT', json.dumps(view))
        self.assertEqual(self.db.campus_today_batches.find_one({'_id': preview['batch_id']}), persisted)
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_readback_failure_stops_and_retains_the_successful_school_record(self):
        def fail_readback(run, seat):
            self.school.query = lambda *args: (_ for _ in ()).throw(RuntimeError('fixture query unavailable'))
        self.school.after_submit = fail_readback
        result = self.finished(self.submit(self.preview()))
        self.assertEqual([s['status'] for s in result['segments']], ['uncertain', 'not_submitted', 'not_submitted'])
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(len(self.school.records), 1)
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_new_external_record_after_preview_prevents_any_submission(self):
        preview = self.preview()
        self.school.records = [SchoolRecord('external', 'fixture01', self.seats[4],
            '2026-10-23 10:00:00', '2026-10-23 12:00:00', 1027)]
        self.assert_unsubmitted(self.finished(self.submit(preview)))
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)

    def test_revision_and_access_changes_after_preview_cannot_submit(self):
        preview = self.preview()
        self.runtime.accounts.verify_and_save('fixture01', 'pass', 'Changed', 1)
        response = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(self.school.posts, [])
        self.account = self.runtime.store.view(self.runtime.store.get_account(self.account.id))
        self.school = self.runtime.accounts.client(self.account.id)
        preview = self.preview()
        self.runtime.auth.access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        response = self.post('/submit', {'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 403, response.json)
        self.assertEqual(self.school.posts, [])

    def test_safety_changes_between_segments_stop_the_remaining_segments(self):
        def revoke(run, seat):
            self.runtime.auth.access.update_one({'_id': 'fixture01'}, {'$set': {'enabled': False}})
        self.school.after_submit = revoke
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual([s['status'] for s in result['segments'][1:]], ['not_submitted'] * 2)
        self.assertEqual(self.school.posts, self.seats[:1])

    def test_stale_revision_between_segments_stops_after_preserving_first_receipt(self):
        def change_revision():
            if len(self.school.posts) == 1:
                self.runtime.store.accounts.update_one({'_id': self.account.id}, {'$inc': {'revision': 1}})
        self.school.after_query = change_revision
        result = self.finished(self.submit(self.preview()))
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual([s['status'] for s in result['segments'][1:]], ['not_submitted'] * 2)
        self.assertEqual(len(self.school.records), 1)

    def test_clock_live_and_expiry_rechecked_after_slow_precheck(self):
        preview = self.preview()
        self.school.after_query = lambda: setattr(self, 'now', self.now + 121)
        self.assert_unsubmitted(self.finished(self.submit(preview)))
        self.assertEqual(self.db.campus_runs.count_documents({}), 0)

    def test_closed_live_and_invalid_clock_allow_no_confirmation(self):
        preview = self.preview()
        self.runtime.reservations.allow_live = False
        self.assertEqual(self.post('/submit', {'preview_token': preview['preview_token']}).status_code, 403)
        self.runtime.reservations.allow_live = True
        self.runtime.scheduler.clock_ok = False
        self.assertEqual(self.post('/submit', {'preview_token': preview['preview_token']}).status_code, 503)
        self.assertEqual(self.school.posts, [])

    def test_tampered_expired_and_cross_kind_tokens_are_safe_4xx(self):
        preview = self.preview()
        token = preview['preview_token']
        for bad in (None, {}, token[:-1] + ('a' if token[-1] != 'a' else 'b')):
            response = self.post('/submit', {'preview_token': bad})
            self.assertEqual(response.status_code, 400, response.json)
        scalar = self.client.post(f'/api/local/accounts/{self.account.id}/today/preview', json={
            'expected_revision': 1, 'date': '2026-10-23', 'begin': '09:00', 'end': '12:00', 'seat_ids': self.seats[:1]},
            headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin).json
        self.assertEqual(self.post('/submit', {'preview_token': scalar['preview_token']}).status_code, 400)
        wrong = self.client.post(f'/api/local/accounts/{self.account.id}/today/submit',
            json={'preview_token': preview['preview_token']}, headers={'Origin': self.origin,
            'X-CSRF-Token': self.csrf}, base_url=self.origin)
        self.assertEqual(wrong.status_code, 400, wrong.json)
        self.now += 121
        self.assertEqual(self.post('/submit', {'preview_token': token}).status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_concurrent_confirmation_and_status_reads_never_repeat_the_batch(self):
        preview = self.preview()
        started, release = self.block_first_submission()
        def confirm(_):
            with self.runtime.app.test_client() as client:
                root_session(self.runtime.auth, client)
                return self.post('/submit', {'preview_token': preview['preview_token']}, client=client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(confirm, range(2)))
        self.assertTrue(started.wait(1))
        self.assertEqual([r.status_code for r in responses], [202, 202])
        self.assertEqual(responses[0].json['batch_id'], responses[1].json['batch_id'])
        for _ in range(3):
            self.assertEqual(self.get(preview['batch_id'])['state'], 'running')
            self.assertEqual(self.client.get(self.path(), base_url=self.origin).json['batch']['batch_id'], preview['batch_id'])
        self.assertEqual(self.school.posts, self.seats[:1])
        release.set()
        result = self.finished(responses[0].json)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.school.posts), 3)
        self.assertEqual(self.db.campus_today_batches.count_documents({}), 1)
        stored = self.db.campus_today_batches.find_one({})
        self.assertNotIn('preview_token', json.dumps(stored, default=str))
        self.assertNotIn(preview['preview_token'], json.dumps(stored, default=str))

    def test_durable_batch_and_run_claims_exist_before_the_school_post(self):
        preview = self.preview()
        original = self.school.submit
        def inspect_claim(run, seat):
            batch = self.db.campus_today_batches.find_one({'_id': preview['batch_id']})
            self.assertEqual(batch['state'], 'running')
            segment = next(s for s in batch['segments'] if s['run_id'] == run.key)
            self.assertEqual(segment['status'], 'running')
            self.assertEqual(self.runtime.store.get_run(run.key)['status'], 'submitting')
            expected = uuid.uuid5(uuid.NAMESPACE_URL, preview['batch_id'] + ':' + str(segment['index'])).hex
            self.assertEqual(run.operation_id, expected)
            return original(run, seat)
        self.school.submit = inspect_claim
        batch = self.finished(self.submit(preview))
        self.assertEqual(batch['state'], 'completed', batch)

    def test_a_second_batch_for_the_same_account_is_not_claimed_while_one_is_running(self):
        first, second = self.preview(), self.preview()
        started, release = self.block_first_submission()
        batch = self.submit(first)
        self.assertTrue(started.wait(1))
        response = self.post('/submit', {'preview_token': second['preview_token']})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(self.db.campus_today_batches.count_documents({}), 1)
        release.set()
        self.finished(batch)

    def test_global_worker_queue_is_bounded_and_duplicate_confirmation_still_reads_original(self):
        previews = [(self.account.id, self.preview())]
        capacity = self.runtime.today.batches.MAX_PENDING
        grant(self.runtime.auth, [f'fixture{index:02}' for index in range(3, capacity + 2)])
        for index in range(2, capacity + 2):
            other = self.runtime.accounts.verify_and_save(f'fixture{index:02}', 'pass', 'Other', None)
            response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23',
                'segments': self.segments()}, account=other.id)
            self.assertEqual(response.status_code, 200, response.json)
            previews.append((other.id, response.json))
        started, release = self.block_first_submission()
        first = self.submit(previews[0][1])
        self.assertTrue(started.wait(1))
        for account_id, preview in previews[1:capacity]:
            response = self.post('/submit', {'preview_token': preview['preview_token']}, account=account_id)
            self.assertEqual(response.status_code, 202, response.json)
        account_id, preview = previews[capacity]
        full = self.post('/submit', {'preview_token': preview['preview_token']}, account=account_id)
        self.assertEqual(full.status_code, 409, full.json)
        self.assertEqual(self.db.campus_today_batches.count_documents({}), capacity)
        self.assertEqual(self.submit(previews[0][1])['batch_id'], first['batch_id'])
        release.set()
        # The intentionally single batch worker drains eight accounts, with two
        # account cooldowns per three-segment batch. Do not weaken slot limits.
        deadline = time.monotonic() + capacity*3 + 5
        while self.db.campus_today_batches.count_documents({'state': 'running'}) and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.db.campus_today_batches.count_documents({'state': 'running'}), 0)
        self.assertEqual(self.db.campus_today_batches.count_documents({'state': 'completed'}), capacity)

    def test_confirmation_does_not_wait_for_an_account_lock_held_by_another_operation(self):
        preview = self.preview()
        with self.runtime.accounts.account_lock(self.account.id):
            before = time.monotonic()
            batch = self.submit(preview)
            self.assertLess(time.monotonic() - before, 1)
            batch = self.finished(batch)
        self.assert_unsubmitted(batch)

    def test_http_confirmation_returns_while_school_work_is_blocked(self):
        preview = self.preview()
        started, release = self.block_first_submission()
        before = time.monotonic()
        batch = self.submit(preview)
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(started.wait(1))
        self.assertEqual(batch['state'], 'running')
        self.assertEqual(self.db.campus_today_batches.count_documents({'state': 'running'}), 1)
        release.set()
        self.finished(batch)

    def test_expiry_while_queued_never_creates_a_run_for_that_batch(self):
        first = self.preview()
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        second = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23',
            'segments': self.segments()}, account=other.id)
        self.assertEqual(second.status_code, 200, second.json)
        started, release = self.block_first_submission()
        first_batch = self.submit(first)
        self.assertTrue(started.wait(1))
        queued = self.post('/submit', {'preview_token': second.json['preview_token']}, account=other.id)
        self.assertEqual(queued.status_code, 202, queued.json)
        self.now += 121
        release.set()
        self.finished(first_batch)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            batch = self.client.get(self.path('/' + second.json['batch_id'], other.id), base_url=self.origin).json
            if batch['state'] != 'running':
                break
            time.sleep(.005)
        self.assertEqual([s['status'] for s in batch['segments']], ['not_submitted'] * 3)
        self.assertEqual(self.db.campus_runs.count_documents({'account_id': other.id}), 0)

    def test_release_preparation_window_covers_all_enabled_triggers(self):
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        self.runtime.accounts.save_plan(other.id, {'mode': 'day', 'seat_ids': self.seats[:1],
            'day_offset': 1, 'begin': '09:00', 'end': '12:00', 'trigger': '08:13:00', 'enabled': True}, 1)
        response = self.post('/preview', {'expected_revision': 1, 'date': '2026-10-23', 'segments': self.segments()})
        self.assertEqual(response.status_code, 409, response.json)
        self.now = datetime(2026, 10, 23, 6, 57, tzinfo=ZONE).timestamp()
        self.assertEqual(self.post('/preview', {'expected_revision': 1,
            'date': '2026-10-23', 'segments': self.segments()}).status_code, 409)
        self.assertEqual(self.school.posts, [])

    def test_trigger_window_entered_during_query_stops_before_first_post(self):
        self.runtime.accounts.save_plan(self.account.id, {'mode': 'day', 'seat_ids': self.seats[:1],
            'day_offset': 1, 'begin': '09:00', 'end': '12:00', 'trigger': '08:14:00', 'enabled': True}, 1)
        self.account = self.runtime.store.view(self.runtime.store.get_account(self.account.id))
        preview = self.preview()
        self.school.after_query = lambda: setattr(self, 'now', self.now + 61)
        self.assert_unsubmitted(self.finished(self.submit(preview)))

    def test_guard_checks_scheduled_window_immediately_before_actual_post(self):
        self.runtime.accounts.save_plan(self.account.id, {'mode': 'day', 'seat_ids': self.seats[:1],
            'day_offset': 1, 'begin': '09:00', 'end': '12:00', 'trigger': '08:14:00', 'enabled': True}, 1)
        self.account = self.runtime.store.view(self.runtime.store.get_account(self.account.id))
        preview = self.preview()
        original = self.school.submit
        def slow_payload(run, seat):
            self.now += 61
            if not self.school.submission_guard():
                return Submission('rejected', sent=False, message='guard stopped')
            return original(run, seat)
        self.school.submit = slow_payload
        batch = self.finished(self.submit(preview))
        self.assertEqual(batch['state'], 'stopped', batch)
        self.assertEqual([s['status'] for s in batch['segments'][1:]], ['not_submitted'] * 2)
        self.assertEqual(self.school.posts, [])

    def test_shutdown_stops_remaining_work_and_closes_the_worker(self):
        preview = self.preview()
        started, release = self.block_first_submission()
        batch = self.submit(preview)
        self.assertTrue(started.wait(1))
        close = threading.Thread(target=self.runtime.close)
        close.start()
        deadline = time.monotonic() + 1
        while not self.runtime.closed and time.monotonic() < deadline:
            time.sleep(.005)
        release.set()
        close.join(3)
        self.assertFalse(close.is_alive())
        stored = self.db.campus_today_batches.find_one({'_id': batch['batch_id']})
        self.assertEqual(stored['state'], 'stopped')
        self.assertEqual([s['status'] for s in stored['segments'][1:]], ['not_submitted'] * 2)
        self.assertEqual(self.school.posts, self.seats[:1])
        self.assertFalse(any(t.is_alive() and t.name.startswith('today-batch') for t in threading.enumerate()))

    def test_restart_marks_interrupted_segments_without_replaying_them(self):
        from local_app.runtime import create_runtime
        preview = self.preview()
        self.school.replies = [Submission('uncertain', sent=True)]
        result = self.finished(self.submit(preview))
        self.runtime.close()
        # Simulate the durable state left by a process loss before terminal updates.
        self.db.campus_today_batches.update_one({'_id': result['batch_id']}, {'$set': {
            'state': 'running', 'segments.0.status': 'running', 'segments.1.status': 'pending',
            'segments.2.status': 'pending'}})
        self.runtime = create_runtime(self.settings, client_factory=BatchSchool)
        self.runtime.reservations.wall_clock = lambda: self.now
        self.client = self.runtime.app.test_client()
        root_session(self.runtime.auth, self.client)
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']
        batch = self.get(result['batch_id'])
        self.assertEqual(batch['state'], 'stopped')
        self.assertEqual([s['status'] for s in batch['segments']], ['uncertain', 'not_submitted', 'not_submitted'])
        self.assertEqual(self.submit(preview), batch)
        self.assertEqual(self.school.posts, self.seats[:1])
        self.assertEqual(self.db.campus_runs.count_documents({}), 1)
        self.assertEqual(self.runtime.accounts._clients, {})

    def _assert_unsubmitted_manual_recovery(self, state):
        from local_app.runtime import create_runtime
        preview = self.preview()
        with patch.object(self.runtime.today.batches.pool, 'submit'):
            batch = self.submit(preview)
        stored = self.db.campus_today_batches.find_one({'_id': batch['batch_id']})
        segment = stored['segments'][0]
        run = RunSpec(self.account.id, 1, '2026-10-23', segment['begin'], segment['end'],
            tuple(segment['seat_ids']), self.now, kind='manual', operation_id=segment['operation_id'],
            expires_at=stored['expires_at'])
        self.runtime.store.ensure_run(run)
        if state == 'preparing':
            self.runtime.store.transition_run(run.key, ('queued',), 'preparing', {})
        control = RunSpec(self.account.id, 1, '2026-10-24', '09:00', '12:00', tuple(self.seats[:1]), self.now)
        self.runtime.store.ensure_run(control)
        self.db.campus_today_batches.update_one({'_id': batch['batch_id']}, {'$set': {
            'segments.0.status': 'running', 'segments.0.run_id': run.key}})
        self.runtime.close()
        self.runtime = create_runtime(self.settings, client_factory=BatchSchool)
        self.runtime.reservations.wall_clock = lambda: self.now
        self.client = self.runtime.app.test_client()
        root_session(self.runtime.auth, self.client)
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']
        self.assert_unsubmitted(self.get(batch['batch_id']))
        self.assertEqual(self.runtime.store.get_run(run.key)['status'], 'skipped')
        self.assertEqual(self.runtime.store.get_run(control.key)['status'], 'queued')
        self.assertEqual(self.runtime.accounts._clients, {})

    def test_restart_terminalizes_only_the_bound_unsubmitted_queued_manual_run(self):
        self._assert_unsubmitted_manual_recovery('queued')

    def test_restart_terminalizes_only_the_bound_unsubmitted_preparing_manual_run(self):
        self._assert_unsubmitted_manual_recovery('preparing')

    def test_scheduled_plan_date_exceptions_favorites_and_other_account_are_exactly_preserved(self):
        self.account = self.runtime.accounts.save_plan(self.account.id, {'mode': 'day',
            'seat_ids': self.seats[:2], 'day_offset': 1, 'begin': '09:00', 'end': '12:00',
            'trigger': '08:28:00', 'enabled': True}, 1)
        self.runtime.accounts.wall_clock = lambda: self.now
        self.account = self.runtime.accounts.update_date_exceptions(self.account.id, {
            'action': 'skip_range', 'start_date': '2026-10-24', 'end_date': '2026-10-24'}, self.account.revision)
        self.runtime.accounts.save_seat_preferences(self.account.id, {
            'favorites': [{'seat_id': seat, 'tags': []} for seat in self.seats[:2]], 'groups': []}, 0)
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        before = self.runtime.store.get_account(self.account.id)
        fields = ('revision', 'credential_revision', 'plan', 'date_exceptions',
                  'seat_preferences_revision', 'seat_preferences', 'secret', 'alias', 'identity')
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        protected = digest({key: before.get(key) for key in fields})
        other_before = digest(self.runtime.store.get_account(other.id))
        self.assertEqual(self.finished(self.submit(self.preview()))['state'], 'completed')
        after = self.runtime.store.get_account(self.account.id)
        self.assertEqual(protected, digest({key: after.get(key) for key in fields}))
        self.assertEqual(other_before, digest(self.runtime.store.get_account(other.id)))

    def test_ownership_session_and_csrf_boundaries_cover_all_batch_routes(self):
        preview = self.preview()
        other = self.runtime.accounts.verify_and_save('fixture02', 'pass', 'Other', None)
        self.assertEqual(self.post('/submit', {'preview_token': preview['preview_token']}, account=other.id).status_code, 403)
        bad_csrf = self.client.post(self.path('/submit'), json={'preview_token': preview['preview_token']},
            headers={'Origin': self.origin, 'X-CSRF-Token': 'bad'}, base_url=self.origin)
        self.assertEqual(bad_csrf.status_code, 403)
        student = self.runtime.app.test_client()
        student.set_cookie('njfu_autolib_session', self.runtime.auth._issue('student',
            account_id=other.id, student_id=other.student_id), domain='127.0.0.1')
        for suffix in ('', '/' + preview['batch_id']):
            self.assertEqual(student.get(self.path(suffix), base_url=self.origin).status_code, 404)
        for suffix in ('/preview', '/submit'):
            self.assertEqual(self.post(suffix, {}, client=student).status_code, 404)
        anonymous = self.runtime.app.test_client()
        self.assertEqual(anonymous.get(self.path(), base_url=self.origin).status_code, 401)
        self.assertEqual(self.post('/submit', {}, client=anonymous).status_code, 401)
        self.assertEqual(self.school.posts, [])

    def test_offline_gateway_never_accepts_or_exposes_batch_school_operations(self):
        from deploy.inbox_gateway import create_gateway
        from deploy.inbox_store import InboxStore
        from test_inbox_gateway import snapshot, FakeUpstream
        store = InboxStore(Path(self.temp.name) / 'offline' / 'inbox.sqlite')
        store.publish(snapshot())
        origin = 'https://batch-offline-fixture.example:45581'
        config = {'origin': origin, 'relay_token': 'r' * 64, 'worker_token': 'w' * 64, 'static_root': self.temp.name}
        gateway = create_gateway(config, store, FakeUpstream()).test_client()
        headers = {'X-NJFU-Relay-HTTPS': '1', 'X-NJFU-Relay-Token': 'r' * 64,
                   'X-NJFU-Client-IP': '198.51.100.2'}
        csrf = gateway.get('/api/local/bootstrap', base_url=origin, headers=headers).json['csrf']
        headers |= {'Origin': origin, 'X-CSRF-Token': csrf}
        login = gateway.post('/api/local/auth/login', json={'kind': 'student', 'username': '2310000001',
            'password': 'fixture-password'}, base_url=origin, headers=headers)
        self.assertEqual(login.status_code, 200, login.json)
        base = '/api/local/accounts/account-a/today/batches'
        for suffix in ('/preview', '/submit'):
            self.assertEqual(gateway.post(base + suffix, json={}, base_url=origin, headers=headers).status_code, 403)
        self.assertEqual(gateway.get(base, base_url=origin, headers=headers).status_code, 403)
        self.assertEqual(self.school.posts, [])


if __name__ == '__main__':
    unittest.main()
