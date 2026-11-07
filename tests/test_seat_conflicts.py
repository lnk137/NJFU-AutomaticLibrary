"""Pure reminders use the same effective schedules as execution, with no writes."""
import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import Seat, ZONE, validate_plan

NOW = datetime(2026, 10, 24, 6, tzinfo=ZONE)
CATALOG = {str(i): Seat(str(i), 'A00' + str(i), '2F') for i in range(1, 16)}


def prepared(key, *, seats=None, begin='09:00', end='12:00', week=None, trigger='07:00:00', exceptions=None):
    raw = {'seat_ids': seats or ['1'], 'mode': 'week', 'week_times': week or {'1': [begin, end]},
           'begin': '09:00', 'end': '12:00', 'trigger': trigger, 'enabled': True}
    return {'account_id': key, 'student_id': '9900000' + key, 'name': 'Person ' + key, 'revision': 3,
            'plan': validate_plan(raw, CATALOG), 'date_exceptions': exceptions or {}}


class SeatConflictTests(unittest.TestCase):
    def detect(self, plans, now=NOW):
        try:
            from local_app.seat_conflicts import detect_seat_conflicts
        except ModuleNotFoundError:
            self.fail('Seat conflict detection has not been implemented')
        return detect_seat_conflicts(plans, CATALOG, now)

    def test_primary_overlap_reports_actual_date_interval_and_both_people(self):
        result = self.detect([prepared('1'), prepared('2', begin='10:00', end='13:00')])
        self.assertEqual((result['start_date'], result['end_date'], result['checked_accounts']),
                         ('2026-10-25', '2026-10-31', 2))
        self.assertFalse(result['has_more'])
        self.assertEqual(result['items'], [{'target_date': '2026-10-26',
            'seat': {'id': '1', 'name': 'A001', 'location': '2F'}, 'begin': '10:00', 'end': '12:00',
            'kind': 'preferred', 'accounts': [
                {'account_id': '1', 'student_id': '99000001', 'name': 'Person 1', 'begin': '09:00', 'end': '12:00', 'rank': 1},
                {'account_id': '2', 'student_id': '99000002', 'name': 'Person 2', 'begin': '10:00', 'end': '13:00', 'rank': 1}]}])

    def test_shared_backups_and_primary_backup_have_different_ranks(self):
        result = self.detect([prepared('1', seats=['1', '3']), prepared('2', seats=['2', '3'])])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['kind'], 'backup')
        self.assertEqual(result['items'][0]['seat']['id'], '3')
        self.assertEqual([a['rank'] for a in result['items'][0]['accounts']], [2, 2])
        mixed = self.detect([prepared('1', seats=['1', '2']), prepared('2', seats=['2'])])
        self.assertEqual(mixed['items'][0]['kind'], 'backup')
        self.assertEqual([a['rank'] for a in mixed['items'][0]['accounts']], [2, 1])

    def test_touching_intervals_and_separate_dates_or_seats_do_not_conflict(self):
        others = [prepared('2', begin='12:00', end='15:00'), prepared('2', seats=['2']),
                  prepared('2', week={'2': ['09:00', '12:00']})]
        for other in others:
            with self.subTest(other=other):
                self.assertEqual(self.detect([prepared('1'), other])['items'], [])

    def test_different_release_trigger_times_do_not_hide_use_time_conflict(self):
        result = self.detect([prepared('1'), prepared('2', trigger='08:00:00')])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['target_date'], '2026-10-26')

    def test_skips_and_changed_one_day_seats_remove_base_plan_conflicts(self):
        for rule in ({'action': 'skip'}, {'action': 'override', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['2']}):
            with self.subTest(rule=rule):
                a = prepared('1', exceptions={'2026-10-26': rule})
                self.assertEqual(self.detect([a, prepared('2')])['items'], [])

    def test_override_can_add_an_otherwise_inactive_weekday_and_use_its_actual_times(self):
        a = prepared('1', exceptions={'2026-10-27': {'action': 'override', 'begin': '13:00', 'end': '16:00', 'seat_ids': ['2']}})
        b = prepared('2', seats=['2'], week={'2': ['14:00', '17:00']})
        row = self.detect([a, b])['items'][0]
        self.assertEqual((row['target_date'], row['seat']['id'], row['begin'], row['end']),
                         ('2026-10-27', '2', '14:00', '16:00'))

    def test_friday_uses_effective_closing_time(self):
        a = prepared('1', week={'5': ['18:00', '22:00']})
        b = prepared('2', week={'5': ['17:00', '20:00']})
        row = self.detect([a, b])['items'][0]
        self.assertEqual((row['target_date'], row['begin'], row['end']), ('2026-10-30', '18:00', '20:00'))
        self.assertEqual(row['accounts'][0]['end'], '20:00')

    def test_horizon_includes_seventh_use_day_but_not_the_next_day(self):
        rule = {'action': 'override', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['1']}
        plans = [prepared(key, week={'7': ['09:00', '12:00']}, exceptions={'2026-10-31': rule}) for key in ('1', '2')]
        self.assertEqual([row['target_date'] for row in self.detect(plans)['items']], ['2026-10-25', '2026-10-31'])

    def test_already_released_plan_is_not_presented_as_a_future_attempt(self):
        plans = [prepared('1'), prepared('2')]
        before = datetime(2026, 10, 25, 6, tzinfo=ZONE)
        after = datetime(2026, 10, 25, 8, tzinfo=ZONE)
        self.assertEqual(len(self.detect(plans, before)['items']), 1)
        self.assertEqual(self.detect(plans, after)['items'], [])

    def test_bounded_results_prioritize_primary_overlap_and_mark_more_results(self):
        # 12 Sunday backup users produce 66 pairs; one Monday primary pair must remain first.
        plans = [prepared(str(i), seats=[str(i + 3), '3'], week={'7': ['09:00', '12:00']}) for i in range(1, 13)]
        plans += [prepared('primary-a'), prepared('primary-b')]
        result = self.detect(plans)
        self.assertEqual(len(result['items']), 50)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['items'][0]['target_date'], '2026-10-26')
        self.assertEqual(result['items'][0]['kind'], 'preferred')
        self.assertEqual(result['checked_accounts'], 14)

    def test_reminders_neither_mutate_inputs_nor_return_unrelated_fields(self):
        plans = [prepared('1'), prepared('2')]
        plans[0]['secret'] = 'must-not-appear'
        plans[0]['seat_preferences'] = {'groups': [{'seat_ids': ['2']}], 'favorites': []}
        before = copy.deepcopy(plans)
        result = self.detect(plans)
        self.assertEqual(plans, before)
        self.assertNotIn('must-not-appear', json.dumps(result))
        self.assertNotIn('seat_preferences', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
