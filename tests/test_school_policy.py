import sys
import unittest
from datetime import date
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from local_app.models import validate_plan,resolve_run,AppError


class PolicyTests(unittest.TestCase):
    def plan(self,**changes):
        data=dict(seat_ids=['1'],day_offset=1,mode='day',begin='09:00',end='12:00',
            trigger='07:00:00',enabled=True)
        return validate_plan(data|changes,{'1':object()})

    def test_official_fixed_rules_have_a_source(self):
        from local_app.policy import school_policy
        p=school_policy()
        self.assertEqual((p['booking_open'],p['day_offset'],p['open'],p['close'],p['friday_close']),
            ('07:00:00',1,'07:30','22:00','20:00'))
        self.assertEqual((p['min_minutes'],p['max_minutes'],p['sign_in_minutes']),(120,900,30))
        self.assertEqual(p['source_url'],'https://libseat.njfu.edu.cn/#/ic/userhelp')

    def test_full_day_uses_actual_target_weekday_and_year_rollover(self):
        plan=self.plan(time_mode='full_day')
        friday=resolve_run('a',1,plan,date(2026,10,22))
        self.assertEqual((friday.target_date,friday.begin,friday.end),('2026-10-23','07:30','20:00'))
        normal=resolve_run('a',1,plan,date(2026,10,23))
        self.assertEqual((normal.begin,normal.end),('07:30','22:00'))
        rollover=resolve_run('a',1,plan,date(2026,12,31))
        self.assertEqual((rollover.target_date,rollover.end),('2027-01-01','20:00'))

    def test_custom_intervals_respect_hours_duration_and_friday(self):
        for changes in ({'begin':'10:30'},{'begin':'07:00'},{'end':'22:30'},
                        {'time_mode':'unknown'},{'day_offset':2}):
            with self.subTest(changes=changes),self.assertRaises(AppError):self.plan(**changes)
        self.assertEqual(self.plan(begin='09:00',end='11:00').end,'11:00')

    def test_saved_custom_plan_is_not_rewritten_by_new_defaults(self):
        plan=self.plan(trigger='20:00:00')
        self.assertEqual((plan.time_mode,plan.trigger,plan.begin),('custom','20:00:00','09:00'))

    def test_custom_daily_plan_clamps_only_actual_friday_use(self):
        plan=self.plan(begin='07:30',end='22:00')
        friday=resolve_run('a',1,plan,date(2026,10,22))
        self.assertEqual((friday.target_date,friday.begin,friday.end),('2026-10-23','07:30','20:00'))
        saturday=resolve_run('a',1,plan,date(2026,10,23))
        self.assertEqual(saturday.end,'22:00')
        earlier=resolve_run('a',1,self.plan(begin='09:00',end='18:00'),date(2026,10,22))
        self.assertEqual(earlier.end,'18:00')
        self.assertEqual(plan.end,'22:00')

    def test_weekly_friday_end_is_normalized_and_too_short_windows_still_fail(self):
        plan=self.plan(mode='week',week_times={'5':['09:00','22:00'],'6':['09:00','22:00']})
        self.assertEqual(plan.week_times,{'5':['09:00','20:00'],'6':['09:00','22:00']})
        with self.assertRaises(AppError):self.plan(mode='week',week_times={'5':['19:00','22:00']})

    def test_daily_custom_save_rejects_under_two_hours_after_friday_cap(self):
        for begin in ('18:01','19:00','20:00'):
            with self.subTest(begin=begin),self.assertRaises(AppError):
                self.plan(begin=begin,end='22:00')
        self.assertEqual(self.plan(begin='18:00',end='22:00').end,'22:00')

    def test_weekly_evening_is_valid_when_friday_is_not_selected(self):
        plan=self.plan(mode='week',begin='19:00',end='22:00',week_times={'6':['19:00','22:00']})
        self.assertEqual(plan.week_times,{'6':['19:00','22:00']})
