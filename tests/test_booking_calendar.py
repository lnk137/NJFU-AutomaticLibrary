"""Pure saved-data calendar tests: no database process, client, writes or school I/O."""
import copy
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.booking_calendar import BookingCalendarService
from local_app.models import AppError, RunSpec, Seat, ZONE, resolve_runs, validate_plan

CATALOG = {'1': Seat('1', 'A001', '2F'), '2': Seat('2', 'A002', '2F')}
NOW = datetime(2026, 10, 29, 6, tzinfo=ZONE).timestamp()
PLAN = {'seat_ids': ['1'], 'day_offset': 1, 'mode': 'day', 'time_mode': 'custom', 'begin': '09:00',
        'end': '22:00', 'week_times': {}, 'trigger': '07:00:00', 'enabled': True}


class ReadCollection:
    def __init__(self, rows=()):
        self.rows, self.reads = list(rows), []

    def find(self, query):
        self.reads.append(copy.deepcopy(query))
        def matches(row):
            for key, value in query.items():
                actual = row
                for part in key.split('.'):
                    actual = actual.get(part) if isinstance(actual, dict) else None
                if isinstance(value, dict):
                    if not isinstance(actual, str):
                        return False
                    if '$gte' in value and actual < value['$gte'] or '$lte' in value and actual > value['$lte']:
                        return False
                elif actual != value:
                    return False
            return True
        return SimpleNamespace(limit=lambda count: copy.deepcopy([row for row in self.rows if matches(row)][:count]))


class ReadStore:
    def __init__(self):
        self.account = {'_id': 'a', 'student_id': 's1', 'revision': 3, 'plan': copy.deepcopy(PLAN),
                        'date_exceptions': {}, 'snapshot': [], 'snapshot_at': None,
                        'secret': {'must': 'never appear'}, 'identity': {'acc_no': 'private'}}
        self.runs = ReadCollection()
        self.db = SimpleNamespace(campus_today_batches=ReadCollection(), campus_cancellations=ReadCollection())
        self.account_reads = 0

    def get_account(self, account_id):
        self.account_reads += 1
        if account_id != 'a':
            raise AppError('not_found', '账号不存在', 404)
        return copy.deepcopy(self.account)


def receipt(**changes):
    return {'id': 'school-1', 'student_id': 's1', 'seat_id': '1', 'begin': '2026-10-30 09:00:00',
            'end': '2026-10-30 20:00:00', 'status': 1027} | changes


def run_row(spec, status='queued', records=None):
    row = {'_id': spec.key, 'account_id': spec.account_id, 'target_date': spec.target_date,
           'spec': asdict(spec), 'status': status, 'message': 'durable message', 'attempted_seat_id': '1'}
    row['spec']['seat_ids'] = list(spec.seat_ids)  # Mongo/BSON stores tuples as arrays.
    if records is not None:
        row['result'] = {'status': 'success', 'records': records, 'verified_at': '2026-10-29T07:00:01+08:00'}
    return row


class BookingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadStore()
        self.service = BookingCalendarService(self.store, CATALOG, wall_clock=lambda: NOW)

    def get(self, start='2026-10-30', end='2026-11-02'):
        return self.service.get('a', start, end)

    def entries(self, payload, day='2026-10-30'):
        return next(row['entries'] for row in payload['days'] if row['date'] == day)

    def spec(self):
        return resolve_runs('a', 3, validate_plan(self.store.account['plan'], CATALOG), date(2026, 10, 29))[0]

    def test_use_dates_cross_month_friday_and_saved_exceptions_without_writes(self):
        self.store.account['date_exceptions'] = {'2026-10-31': {'action': 'skip'}, '2026-11-01': {'action': 'skip'},
            '2026-11-02': {'action': 'override', 'begin': '14:00', 'end': '18:00', 'seat_ids': ['2', '1']}}
        original = copy.deepcopy(self.store.account)
        result = self.get()
        friday = self.entries(result)[0]
        self.assertEqual((friday['begin'], friday['end'], friday['state']), ('09:00', '20:00', 'planned'))
        self.assertTrue(friday['release_at'].startswith('2026-10-29T07:00:00'))
        self.assertEqual(self.entries(result, '2026-10-31')[0]['state'], 'skipped')
        adjusted = self.entries(result, '2026-11-02')[0]
        self.assertEqual((adjusted['begin'], adjusted['end'], adjusted['seat_ids'], adjusted['exception']),
                         ('14:00', '18:00', ['2', '1'], 'override'))
        self.assertEqual(original, self.store.account)
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('acc_no', repr(result))
        for query in self.store.runs.reads + self.store.db.campus_today_batches.reads + self.store.db.campus_cancellations.reads:
            self.assertEqual(query['account_id'], 'a')

    def test_all_saved_segments_and_weekdays_are_projected(self):
        segments = [{'id': 'morning', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['1']},
                    {'id': 'afternoon', 'begin': '14:00', 'end': '18:00', 'seat_ids': ['2', '1']}]
        self.store.account['plan'] |= {'segments': segments}
        self.assertEqual([e['begin'] for e in self.entries(self.get())], ['09:00', '14:00'])
        self.store.account['plan'] = PLAN | {'mode': 'week', 'week_segments': {'1': segments}, 'week_times': {}}
        result = self.get()
        self.assertEqual(self.entries(result), [])
        self.assertEqual(len(self.entries(result, '2026-11-02')), 2)

    def test_exact_success_receipt_merges_plan_run_and_snapshot(self):
        self.store.account.update(snapshot=[receipt(), receipt()], snapshot_at='2026-10-29T05:00:00+08:00')
        self.store.runs.rows = [run_row(self.spec(), 'success', [receipt()])]
        entries = self.entries(self.get())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['state'], 'confirmed')
        self.assertEqual(entries[0]['run_ids'], [self.spec().key])
        self.assertFalse(entries[0]['stale'])

    def test_success_without_exact_identity_interval_status_and_attempt_is_uncertain(self):
        cases = [[], [receipt(student_id='foreign')], [receipt(end='2026-10-30 19:00:00')],
                 [receipt(seat_id='2')], [receipt(status='1027')], [receipt(status={'unknown': True})]]
        for records in cases:
            with self.subTest(records=records):
                self.store.runs.rows = [run_row(self.spec(), 'success', records)]
                result = self.get()
                self.assertEqual(self.entries(result)[0]['state'], 'uncertain')
                self.assertIn('unverified_success', [warning['code'] for warning in result['warnings']])

    def test_unknown_and_stale_snapshot_are_visible_and_never_promoted_by_old_run(self):
        self.store.account.update(snapshot=[receipt(status='cancelled')], snapshot_at='2026-10-20T08:00:00+08:00', query_error='timeout')
        self.store.runs.rows = [run_row(self.spec(), 'success', [receipt()])]
        result = self.get()
        self.assertTrue(all(entry['state'] == 'uncertain' for entry in self.entries(result)))
        self.assertIn('stale_snapshot', [w['code'] for w in result['warnings']])
        self.assertIn('snapshot_query_failed', [w['code'] for w in result['warnings']])

    def test_foreign_or_malformed_records_warn_without_leaking_foreign_data(self):
        self.store.account['snapshot'] = [receipt(student_id='private-other'), receipt(begin='2026-10-30')]
        result = self.get()
        self.assertIn('invalid_school_record', [warning['code'] for warning in result['warnings']])
        self.assertNotIn('private-other', repr(result))
        self.assertTrue(all(entry['state'] != 'confirmed' for entry in self.entries(result)))

    def test_contradictory_same_receipt_and_malformed_status_are_visible_uncertainties(self):
        self.store.account.update(plan=None, snapshot=[receipt(), receipt(end='2026-10-30 19:00:00')])
        result = self.get()
        self.assertEqual([entry['state'] for entry in self.entries(result)], ['uncertain', 'uncertain'])
        self.assertIn('conflicting_school_record', [warning['code'] for warning in result['warnings']])
        self.store.account['snapshot'] = []
        self.store.account['plan'] = copy.deepcopy(PLAN)
        self.store.runs.rows = [run_row(self.spec(), ['malformed'])]
        self.assertEqual(self.entries(self.get())[0]['state'], 'uncertain')

    def test_timezone_aware_receipts_use_beijing_day(self):
        self.store.account.update(plan=None, snapshot=[receipt(begin='2026-10-29T23:30:00Z', end='2026-10-30T01:30:00Z')])
        entry = self.entries(self.get())[0]
        self.assertEqual((entry['begin'], entry['end'], entry['state']), ('07:30', '09:30', 'confirmed'))

    def test_durable_states_disabled_plan_and_past_history_do_not_invent_success(self):
        spec = self.spec()
        self.store.account['plan']['enabled'] = False
        for status, expected in [('queued', 'planned'), ('preparing', 'planned'), ('submitting', 'uncertain'),
                                 ('uncertain', 'uncertain'), ('failed', 'failed'), ('skipped', 'skipped'), ('bogus', 'uncertain')]:
            with self.subTest(status=status):
                self.store.runs.rows = [run_row(spec, status)]
                self.assertEqual(self.entries(self.get())[0]['state'], expected)
        self.store.runs.rows = []
        self.store.account['plan']['enabled'] = True
        self.assertEqual(self.entries(self.get('2026-10-28', '2026-10-30'), '2026-10-28'), [])

    def test_invalid_plan_is_a_visible_warning_while_saved_receipts_remain(self):
        self.store.account.update(plan=PLAN | {'day_offset': 2}, snapshot=[receipt()])
        result = self.get()
        self.assertFalse(result['plan_valid'])
        self.assertIn('invalid_plan', [warning['code'] for warning in result['warnings']])
        self.assertEqual([entry['state'] for entry in self.entries(result)], ['confirmed'])

    def test_invalid_run_is_visible_and_not_bound_to_saved_plan(self):
        row = run_row(self.spec(), 'success', [receipt()])
        row['spec']['account_id'] = 'foreign'
        self.store.runs.rows = [row]
        result = self.get()
        self.assertIn('invalid_run', [warning['code'] for warning in result['warnings']])
        self.assertEqual(sorted(e['state'] for e in self.entries(result)), ['planned', 'uncertain'])

    def test_batch_dedup_requires_exact_run_binding_and_no_success_without_run(self):
        self.store.account['plan'] = None
        operation = uuid.uuid5(uuid.NAMESPACE_URL, 'batch-1:0').hex
        spec = RunSpec('a', 3, '2026-10-30', '09:00', '20:00', ('1',), NOW, 'manual', operation)
        segment = {'index': 0, 'operation_id': operation, 'run_id': spec.key, 'begin': '09:00', 'end': '20:00',
                   'seat_ids': ['1'], 'status': 'uncertain', 'message': '待核对'}
        self.store.db.campus_today_batches.rows = [{'_id': 'batch-1', 'account_id': 'a', 'target_date': '2026-10-30',
                                                    'revision': 3, 'segments': [segment]}]
        self.store.runs.rows = [run_row(spec, 'success', [receipt()])]
        entries = self.entries(self.get())
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['state'], entries[0]['batch_id']), ('confirmed', 'batch-1'))
        self.store.runs.rows = []
        segment['status'] = 'success'
        self.assertEqual(self.entries(self.get())[0]['state'], 'uncertain')
        segment['status'] = 'not_submitted'
        self.assertEqual(self.entries(self.get())[0]['state'], 'skipped')

    def test_cancellation_only_exact_acknowledged_success_changes_same_receipt(self):
        self.store.account.update(plan=None, snapshot=[receipt(), receipt(id='other', begin='2026-10-30 07:30:00', end='2026-10-30 09:30:00')])
        cancellation = {'_id': 'cancel-1', 'account_id': 'a', 'record': receipt(), 'state': 'success', 'ack': True,
                        'completed_at': NOW - 1, 'requested_at': NOW - 2}
        self.store.db.campus_cancellations.rows = [cancellation]
        entries = self.entries(self.get())
        self.assertEqual([entry['state'] for entry in entries], ['confirmed', 'cancelled'])
        self.assertEqual(entries[1]['previous_state'], 'confirmed')
        for state in ('submitting', 'uncertain', 'failed'):
            cancellation['state'] = state
            self.assertTrue(all(entry['state'] == 'confirmed' for entry in self.entries(self.get())))
        cancellation.update(state='success', ack=False)
        self.assertTrue(all(entry['state'] == 'confirmed' for entry in self.entries(self.get())))
        cancellation.update(ack=True, record=receipt(student_id='other'))
        self.assertTrue(all(entry['state'] == 'confirmed' for entry in self.entries(self.get())))

    def test_strict_ranges_rejected_before_account_or_evidence_reads(self):
        cases = [('2026-10-1', '2026-10-30'), ('2026-02-30', '2026-03-01'), ('2026-11-02', '2026-11-01'),
                 ('2026-10-01', '2026-11-12'), (None, '2026-11-01')]
        for start, end in cases:
            with self.subTest(start=start, end=end), self.assertRaises(AppError):
                self.get(start, end)
        self.assertEqual(self.store.account_reads, 0)
        self.assertEqual(len(self.get('2026-10-01', '2026-11-11')['days']), 42)

    def test_limits_fail_closed_instead_of_returning_incomplete_empty_calendar(self):
        self.store.runs.rows = [run_row(self.spec())] * 2
        with patch('local_app.booking_calendar.MAX_RUNS', 1), self.assertRaises(AppError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, 'calendar_limit')
        self.store.runs.rows = []
        self.store.account['snapshot'] = [receipt()] * 2
        with patch('local_app.booking_calendar.MAX_RECORDS', 1), self.assertRaises(AppError):
            self.get()


if __name__ == '__main__':
    unittest.main()
