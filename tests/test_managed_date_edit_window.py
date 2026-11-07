"""Date edits preserve a dual task's preparation and authorization window."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

import test_date_exception_service as fixture
from local_app.models import AppError, RunSpec, ZONE


class ManagedDateEditWindowTests(unittest.TestCase):
    setUpClass = classmethod(fixture.DateExceptionServiceTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.DateExceptionServiceTests.tearDownClass.__func__)
    tearDown = fixture.DateExceptionServiceTests.tearDown

    def setUp(self):
        fixture.DateExceptionServiceTests.setUp(self)
        self.clock = [datetime(2026, 10, 24, 6, tzinfo=ZONE).timestamp()]
        self.service.wall_clock = lambda: self.clock[0]
        self.due = datetime(2026, 10, 24, 7, tzinfo=ZONE).timestamp()
        gate = Mock(); gate.is_managed.side_effect = lambda account: account == self.account.id
        self.service.managed_gate = gate
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'date_exceptions': {
            '2026-10-23': {'action': 'skip'}, '2026-10-25': {'action': 'override',
                'begin': '09:00', 'end': '12:00', 'seat_ids': ['1']},
            '2026-10-27': {'action': 'skip'}}}})

    def update(self, data):
        row = self.store.get_account(self.account.id)
        return self.service.update_date_exceptions(self.account.id, data, row['revision'])

    def assert_rejected_unchanged(self, data):
        before = deepcopy(self.store.get_account(self.account.id))
        history = list(self.store.runs.find())
        with self.assertRaises(AppError) as rejected:
            self.update(data)
        self.assertEqual(rejected.exception.code, 'account_busy')
        self.assertEqual(self.store.get_account(self.account.id), before)
        self.assertEqual(list(self.store.runs.find()), history)

    def test_target_override_skip_and_remove_are_blocked_at_inclusive_window_edges(self):
        actions = [dict(action='override', date='2026-10-25', begin='14:00', end='18:00', seat_ids=['2']),
                   dict(action='skip_range', start_date='2026-10-25', end_date='2026-10-25'),
                   dict(action='remove', start_date='2026-10-25', end_date='2026-10-25')]
        for seconds in (-183, -10, -3, 0, 30, 40):
            self.clock[0] = self.due + seconds
            for action in actions:
                with self.subTest(seconds=seconds, action=action['action']):
                    self.assert_rejected_unchanged(action)

    def test_distant_edits_preserve_authorized_current_task_and_other_accounts(self):
        run = RunSpec(self.account.id, self.account.revision, '2026-10-25', '09:00', '12:00',
                      ('1',), self.due, operation_id='dual:' + 'a' * 64)
        self.store.ensure_run(run)
        self.store.runs.update_one({'_id': run.key}, {'$set': {'status': 'submitting',
            'dual_progress': {'phase': 'authorized', 'owner': 'ubuntu', 'grant_id': 'b' * 32}}})
        history = list(self.store.runs.find()); other = self.store.get_account(self.other.id)
        for seconds in (-183, -10, -3, 0, 30, 40):
            self.clock[0] = self.due + seconds
            for action in (dict(action='override', date='2026-10-27', begin='14:00', end='18:00', seat_ids=['2']),
                           dict(action='skip_range', start_date='2026-10-27', end_date='2026-10-27'),
                           dict(action='remove', start_date='2026-10-27', end_date='2026-10-27')):
                with self.subTest(seconds=seconds, action=action['action']):
                    before = self.store.get_account(self.account.id)
                    changed = self.update(action)
                    self.assertEqual(changed.revision, before['revision'] + 1)
                    self.assertEqual(changed.date_exceptions['2026-10-25'], before['date_exceptions']['2026-10-25'])
                    self.assertEqual(changed.plan, before['plan'])
                    self.assertEqual(list(self.store.runs.find()), history)
                    self.assertEqual(self.store.get_account(self.other.id), other)

    def test_remove_checks_the_entire_range_but_allows_past_cleanup(self):
        self.clock[0] = self.due - 10
        self.assert_rejected_unchanged(dict(action='remove', start_date='2026-10-23', end_date='2026-10-27'))
        changed = self.update(dict(action='remove', start_date='0001-01-01', end_date='0001-01-01'))
        changed = self.update(dict(action='remove', start_date='2026-10-23', end_date='2026-10-23'))
        self.assertNotIn('2026-10-23', changed.date_exceptions)
        self.assertIn('2026-10-25', changed.date_exceptions)

    def test_window_rechecks_before_commit_for_edits_and_removals(self):
        original = self.service._require_ordinary_scope
        def enter_window(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock[0] = self.due - 183
            return result
        for action in (dict(action='skip_range', start_date='2026-10-25', end_date='2026-10-25'),
                       dict(action='remove', start_date='2026-10-25', end_date='2026-10-25')):
            self.clock[0] = self.due - 184
            with self.subTest(action=action['action']), patch.object(self.service, '_require_ordinary_scope', side_effect=enter_window):
                self.assert_rejected_unchanged(action)

    def test_immediately_outside_window_allows_future_edit_and_past_release_removal(self):
        self.clock[0] = self.due - 183.001
        changed = self.update(dict(action='skip_range', start_date='2026-10-25', end_date='2026-10-25'))
        self.assertEqual(changed.date_exceptions['2026-10-25'], {'action': 'skip'})
        self.clock[0] = self.due + 40.001
        changed = self.update(dict(action='remove', start_date='2026-10-25', end_date='2026-10-25'))
        self.assertNotIn('2026-10-25', changed.date_exceptions)

    def test_midnight_window_is_bound_to_target_date(self):
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'plan.trigger': '23:59:50'}})
        self.clock[0] = datetime(2026, 10, 25, 0, 0, 5, tzinfo=ZONE).timestamp()
        self.assert_rejected_unchanged(dict(action='remove', start_date='2026-10-25', end_date='2026-10-25'))
        changed = self.update(dict(action='remove', start_date='2026-10-23', end_date='2026-10-23'))
        self.assertNotIn('2026-10-23', changed.date_exceptions)
        changed = self.update(dict(action='skip_range', start_date='2026-10-27', end_date='2026-10-27'))
        self.assertEqual(changed.date_exceptions['2026-10-27'], {'action': 'skip'})


if __name__ == '__main__':
    unittest.main()
