"""Single-date cohorts preserve recurring plans and existing run ownership."""
import copy
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.date_exceptions import validate_date_exceptions, next_scheduled_runs
from local_app.models import AppError, Seat, ZONE, resolve_runs, validate_plan
import test_date_exception_service as fixture
import test_scheduled_segments as flow_fixture

CATALOG = {'1': Seat('1', 'A001', '2F'), '2': Seat('2', 'A002', '2F')}
PLAN = dict(mode='day', begin='09:00', end='12:00', seat_ids=['1'],
            enabled=True, trigger='07:00:00')
DAY = '2026-10-25'


def segments():
    return [dict(id='morning', begin='09:00', end='12:00', seat_ids=['1', '2']),
            dict(id='afternoon', begin='14:00', end='18:00', seat_ids=['2', '1'])]


class DateSegmentRulesTests(unittest.TestCase):
    def normalized(self, values=None, day=DAY):
        return validate_date_exceptions({day: dict(action='override', segments=values or segments())}, CATALOG)

    def test_ordinary_or_weekly_base_keeps_all_segments_and_other_dates(self):
        for base in (PLAN, PLAN | dict(mode='week', week_times={'1': ['10:00', '13:00']}),
                     PLAN | dict(segments=segments())):
            with self.subTest(base=base):
                plan = validate_plan(base, CATALOG)
                rules = self.normalized(); before = copy.deepcopy((plan, rules))
                runs = resolve_runs('a', 3, plan, date(2026, 10, 24), rules)
                self.assertEqual([(r.begin, r.end, r.seat_ids) for r in runs],
                    [('09:00', '12:00', ('1', '2')), ('14:00', '18:00', ('2', '1'))])
                self.assertEqual([r.operation_id for r in runs],
                    ['override-2026-10-25-morning', 'override-2026-10-25-afternoon'])
                self.assertEqual(len({r.key for r in runs}), 2)
                self.assertEqual(resolve_runs('a', 3, plan, date(2026, 10, 25), rules),
                                 resolve_runs('a', 3, plan, date(2026, 10, 25)))
                self.assertEqual((plan, rules), before)
                self.assertEqual(resolve_runs('a', 3, replace(plan, enabled=False), date(2026, 10, 24), rules), ())

    def test_single_actual_date_does_not_inherit_daily_friday_limits(self):
        values = [dict(id='evening', begin='19:00', end='22:00', seat_ids=['1'])]
        self.assertEqual(self.normalized(values)[DAY]['segments'][0]['end'], '22:00')
        with self.assertRaises(AppError): self.normalized(values, '2026-10-30')
        friday = self.normalized([values[0] | {'begin': '18:00'}], '2026-10-30')
        self.assertEqual(friday['2026-10-30']['segments'][0]['end'], '20:00')

    def test_ids_seat_priority_and_order_survive_time_edits(self):
        values = list(reversed(segments())); before = copy.deepcopy(values)
        normalized = self.normalized(values)[DAY]['segments']
        self.assertEqual([r['id'] for r in normalized], ['morning', 'afternoon'])
        self.assertEqual(values, before)
        normalized[0]['begin'] = '10:00'
        edited = self.normalized(normalized)[DAY]['segments']
        self.assertEqual([r['id'] for r in edited], ['morning', 'afternoon'])
        self.assertEqual(edited[0]['seat_ids'], ['1', '2'])

    def test_invalid_or_mixed_segments_are_rejected(self):
        first = segments()[0]
        invalid = [[], segments() * 4, [first, first], [first | {'id': 'override-other'}],
                   [{k:v for k,v in first.items() if k != 'id'}], [first | {'id': '../x'}],
                   [first | {'seat_ids': ['unknown']}], [first | {'seat_ids': ['1','1']}],
                   [first | {'begin':'10:30', 'end':'11:00'}],
                   [first, segments()[1] | {'begin':'11:00'}]]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(AppError):
                validate_date_exceptions({DAY: dict(action='override', segments=values)}, CATALOG)
        with self.assertRaises(AppError):
            validate_date_exceptions({DAY: dict(action='override',segments=segments(),
                begin='09:00',end='12:00',seat_ids=['1'])}, CATALOG)

    def test_legacy_scalar_skip_remove_and_next_run_stay_compatible(self):
        base = validate_plan(PLAN, CATALOG)
        scalar = {DAY: dict(action='override',begin='10:00',end='12:00',seat_ids=['2'])}
        self.assertEqual(validate_date_exceptions(scalar, CATALOG), scalar)
        self.assertIsNone(resolve_runs('a',3,base,date(2026,10,24),scalar)[0].operation_id)
        rules = self.normalized()
        self.assertEqual(len(next_scheduled_runs('a',3,base,
            datetime(2026,10,24,6,tzinfo=ZONE), rules)), 2)
        rules[DAY] = {'action':'skip'}
        self.assertEqual(resolve_runs('a',3,base,date(2026,10,24),rules), ())


class DateSegmentServiceTests(unittest.TestCase):
    setUpClass = classmethod(fixture.DateExceptionServiceTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.DateExceptionServiceTests.tearDownClass.__func__)
    setUp = fixture.DateExceptionServiceTests.setUp
    tearDown = fixture.DateExceptionServiceTests.tearDown

    def save(self, **changes):
        request = dict(action='override', date=DAY, segments=segments()) | changes
        return self.service.update_date_exceptions(self.account.id, request, self.account.revision)

    def test_save_keeps_base_other_account_history_and_cas(self):
        before = self.store.get_account(self.account.id)
        other = self.store.get_account(self.other.id)
        changed = self.save()
        self.assertEqual(changed.date_exceptions[DAY]['segments'], segments())
        self.assertEqual(changed.revision, self.account.revision+1)
        after = self.store.get_account(self.account.id)
        for key in ('plan','secret','credential_revision','snapshot'):
            self.assertEqual(before.get(key),after.get(key))
        self.assertEqual(other,self.store.get_account(self.other.id))
        self.assertEqual(self.store.runs.count_documents({}),0)
        with self.assertRaises(AppError) as stale: self.save()
        self.assertEqual(stale.exception.code,'conflict')
        self.assertEqual(after,self.store.get_account(self.account.id))
        restored = self.service.update_date_exceptions(changed.id,
            dict(action='remove',start_date=DAY,end_date=DAY),changed.revision)
        self.assertEqual(restored.date_exceptions,{})
        self.assertEqual(restored.plan,before['plan'])

    def test_mixed_shapes_and_managed_scope_fail_without_mutation(self):
        before = self.store.get_account(self.account.id)
        with self.assertRaises(AppError):self.save(begin='09:00',end='12:00',seat_ids=['1'])
        self.assertEqual(before,self.store.get_account(self.account.id))
        gate = Mock();gate.is_managed.return_value=True;self.service.managed_gate=gate
        with self.assertRaises(AppError) as managed:self.save()
        self.assertEqual(managed.exception.code,'dual_segmented_unsupported')
        self.assertEqual(before,self.store.get_account(self.account.id))


class DateSegmentExecutionTests(unittest.TestCase):
    setUpClass = classmethod(flow_fixture.ScheduledSegmentFlowTests.setUpClass.__func__)
    tearDownClass = classmethod(flow_fixture.ScheduledSegmentFlowTests.tearDownClass.__func__)
    tearDown = flow_fixture.ScheduledSegmentFlowTests.tearDown
    execute = flow_fixture.ScheduledSegmentFlowTests.execute

    def setUp(self):
        flow_fixture.ScheduledSegmentFlowTests.setUp(self)
        self.now=flow_fixture.DUE-600
        self.account=self.accounts.save_plan(self.account.id,PLAN,self.account.revision)
        self.account=self.accounts.update_date_exceptions(self.account.id,
            dict(action='override',date=DAY,segments=segments()),self.account.revision)
        self.now=flow_fixture.DUE-60;self.scheduler.refresh()
        self.runs=resolve_runs(self.account.id,self.account.revision,
            validate_plan(self.account.plan,self.accounts.catalog),flow_fixture.RELEASE,self.account.date_exceptions)

    def test_ordinary_base_override_executes_each_segment_once(self):
        self.assertEqual(self.execute(),['success','success'])
        self.assertEqual(self.calls,[('09:00','1'),('14:00','2')])
        before=list(self.calls);self.scheduler._submit(self.runs[0]);self.assertEqual(self.calls,before)

    def test_unknown_first_segment_stops_later_override(self):
        self.behavior['09:00']='uncertain'
        self.assertEqual(self.execute(),['uncertain','skipped'])
        self.assertEqual(self.calls,[('09:00','1')])


if __name__ == '__main__':unittest.main()
