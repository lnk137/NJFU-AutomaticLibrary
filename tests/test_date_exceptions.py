import sys
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Plan, Seat, ZONE, resolve_run

CATALOG = {'1': Seat('1', 'A001', '2F'), '2': Seat('2', 'A002', '2F')}


class DateExceptionRulesTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan(('1',), 1, 'day', '09:00', '12:00', {}, '07:00:00', True)

    def resolve(self, release, exceptions, plan=None):
        return resolve_run('a', 3, plan or self.plan, release, date_exceptions=exceptions)

    def test_skipped_use_date_resumes_unchanged_on_the_following_date(self):
        rules = {'2026-10-25': {'action': 'skip'}}
        self.assertIsNone(self.resolve(date(2026, 10, 24), rules))
        run = self.resolve(date(2026, 10, 25), rules)
        self.assertEqual((run.target_date, run.begin, run.end, run.seat_ids),
                         ('2026-10-26', '09:00', '12:00', ('1',)))
        self.assertEqual(self.plan.begin, '09:00')

    def test_override_can_add_an_inactive_weekday_without_changing_the_week(self):
        weekly = replace(self.plan, mode='week', week_times={'1': ['09:00', '12:00']})
        rules = {'2026-10-25': {'action': 'override', 'begin': '14:00', 'end': '18:00', 'seat_ids': ['2', '1']}}
        run = self.resolve(date(2026, 10, 24), rules, weekly)
        self.assertEqual((run.target_date, run.begin, run.end, run.seat_ids),
                         ('2026-10-25', '14:00', '18:00', ('2', '1')))
        self.assertIsNone(self.resolve(date(2026, 10, 24), {}, weekly))
        self.assertEqual(weekly.week_times, {'1': ['09:00', '12:00']})

    def test_disabled_master_stays_disabled_even_with_an_override(self):
        rule = {'2026-10-25': {'action': 'override', 'begin': '14:00', 'end': '18:00', 'seat_ids': ['2']}}
        self.assertIsNone(self.resolve(date(2026, 10, 24), rule, replace(self.plan, enabled=False)))

    def test_exception_uses_target_date_across_the_year_boundary(self):
        self.assertIsNone(self.resolve(date(2026, 12, 31), {'2027-01-01': {'action': 'skip'}}))
        self.assertEqual(self.resolve(date(2027, 1, 1), {}).target_date, '2027-01-02')

    def test_friday_override_reuses_school_clipping_and_duration_rules(self):
        from local_app.date_exceptions import validate_date_exceptions
        raw = {'2026-10-30': {'action': 'override', 'begin': '18:00', 'end': '22:00', 'seat_ids': ['2']}}
        rules = validate_date_exceptions(raw, CATALOG)
        run = self.resolve(date(2026, 10, 29), rules)
        self.assertEqual((run.begin, run.end), ('18:00', '20:00'))
        raw['2026-10-30']['begin'] = '19:00'
        with self.assertRaises(AppError):
            validate_date_exceptions(raw, CATALOG)

    def test_strict_dates_actions_and_catalog_seats_are_required(self):
        from local_app.date_exceptions import validate_date_exceptions
        invalid = [[], {'2026-2-01': {'action': 'skip'}}, {'2026-02-30': {'action': 'skip'}},
                   {'2026-10-25': {'action': 'enable'}}, {'2026-10-25': {'action': 'skip', 'enabled': True}},
                   {'2026-10-25': {'action': 'override', 'begin': '09:00', 'end': '12:00', 'seat_ids': []}},
                   {'2026-10-25': {'action': 'override', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['missing']}},
                   {'2026-10-25': {'action': 'override', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['1', '1']}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AppError):
                validate_date_exceptions(value, CATALOG)

    def test_long_leave_search_reaches_the_next_valid_week(self):
        from datetime import timedelta
        from local_app.date_exceptions import next_scheduled_run
        rules = {(date(2026, 10, 25) + timedelta(days=i)).isoformat(): {'action': 'skip'} for i in range(20)}
        now = datetime(2026, 10, 24, 6, tzinfo=ZONE)
        run = next_scheduled_run('a', 3, self.plan, now, rules)
        self.assertEqual((run.target_date, run.due_at),
                         ('2026-11-14', datetime(2026, 11, 13, 7, tzinfo=ZONE).timestamp()))

    def test_after_release_next_run_does_not_point_to_a_missed_date(self):
        from local_app.date_exceptions import next_scheduled_run
        run = next_scheduled_run('a', 3, self.plan, datetime(2026, 10, 24, 7, 1, tzinfo=ZONE), {})
        self.assertEqual(run.target_date, '2026-10-26')

    def test_removing_exception_restores_the_original_order_and_interval(self):
        rules = {'2026-10-25': {'action': 'override', 'begin': '14:00', 'end': '18:00', 'seat_ids': ['2']}}
        self.assertEqual(self.resolve(date(2026, 10, 24), rules).seat_ids, ('2',))
        run = self.resolve(date(2026, 10, 24), {})
        self.assertEqual((run.begin, run.end, run.seat_ids), ('09:00', '12:00', ('1',)))


if __name__ == '__main__':
    unittest.main()
