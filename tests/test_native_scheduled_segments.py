"""Native legacy ownership and bounded multi-window execution share one Runtime."""
from datetime import datetime
import math
import time
from unittest.mock import patch

from local_app.models import AppError, ZONE, resolve_runs, validate_plan
from support.native_runtime import NativeRuntimeCase


class NativeScheduledSegmentTests(NativeRuntimeCase):
    def segmented(self, due=None):
        due = due or math.ceil(time.time() + 6)
        values = [dict(id='morning', begin='09:00', end='12:00', seat_ids=['1', '26']),
                  dict(id='afternoon', begin='12:00', end='15:00', seat_ids=['2', '26']),
                  dict(id='evening', begin='16:00', end='19:00', seat_ids=['3', '26'])]
        with patch.object(self.runtime.accounts, 'wall_clock', return_value=due - 600):
            self.account = self.runtime.accounts.save_plan(self.account.id, {
                'seat_ids': ['1'], 'day_offset': 1, 'mode': 'day', 'begin': '09:00',
                'end': '12:00', 'trigger': datetime.fromtimestamp(due, ZONE).strftime('%H:%M:%S'),
                'enabled': True, 'segments': values}, self.account.revision)
        return resolve_runs(self.account.id, self.account.revision,
            validate_plan(self.account.plan, self.catalog), datetime.fromtimestamp(due, ZONE).date())

    def test_discovery_persists_all_members_and_routes_only_leader_to_python(self):
        self.segmented(due=math.ceil(time.time() + 60))
        self.runtime.native.bridge.set_control(live=True, clock_ok=True)
        self.runtime.scheduler.refresh()
        rows = list(self.db.campus_runs.find({'account_id': self.account.id}))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['status'] == 'queued' for row in rows))
        self.assertEqual(self.runtime.scheduler.jobs, {})
        self.assertEqual([job['run'].operation_id for job in self.runtime.scheduler.fallback.jobs.values()], ['morning'])

    def test_mixed_release_preserves_legacy_native_and_independent_segment_receipts(self):
        other = self.runtime.accounts.verify_and_save(self.students[1], 'fixture-password')
        due = math.ceil(time.time() + 6)
        runs = self.segmented(due=due)
        legacy = self.plan(other, due=due)
        self.runtime.start()
        legacy_row = self.wait(legacy.key, {'success', 'failed', 'skipped'}, 15)
        rows = [self.wait(run.key, {'success', 'failed', 'skipped'}, 15) for run in runs]
        self.assertEqual(legacy_row['status'], 'success', legacy_row)
        self.assertEqual(legacy_row['execution_source'], 'native_campus_first')
        self.assertTrue(legacy_row.get('native_binding'))
        self.assertEqual([row['status'] for row in rows], ['success'] * 3)
        self.assertTrue(all(not row.get('native_binding') for row in rows))
        self.assertEqual([row['seat_id'] for row in self.fixture.posts() if row['student_id'] == self.students[0]], ['1', '2', '3'])
        self.assertEqual(len(self.fixture.posts()), 4)
        timings = [row['attempts'][0]['timings'] for row in rows]
        for previous, current in zip(timings, timings[1:]):
            # These are actual loopback HTTP timestamps captured by CampusClient,
            # not scheduler dispatch times or synthetic pacing diagnostics.
            self.assertGreaterEqual(current['request_started_at'] - previous['request_started_at'], 1.195)
            self.assertGreaterEqual(current['request_started_at'] - previous['request_finished_at'], .995)
        receipts = [[record for record in row['result']['records']
            if record['begin'] == row['target_date'] + ' ' + row['spec']['begin'] + ':00'
            and record['end'] == row['target_date'] + ' ' + row['spec']['end'] + ':00'
            and record['seat_id'] == row['attempted_seat_id']] for row in rows]
        self.assertEqual([len(matches) for matches in receipts], [1, 1, 1])
        self.assertEqual(len({matches[0]['id'] for matches in receipts}), 3)

    def test_unknown_first_reply_stops_siblings_and_changed_ids_cannot_reopen(self):
        runs = self.segmented()
        self.fixture.faults[(self.students[0], 1)] = {'reject': True, 'code': 999, 'message': 'unknown fixture rule'}
        self.runtime.start()
        rows = [self.wait(run.key, {'success', 'failed', 'skipped'}, 15) for run in runs]
        self.assertEqual([row['status'] for row in rows], ['failed', 'skipped', 'skipped'])
        current = self.runtime.store.get_account(self.account.id)
        later = math.ceil(time.time() + 60)
        plan = current['plan'] | {'trigger': datetime.fromtimestamp(later, ZONE).strftime('%H:%M:%S'),
            'segments': [segment | {'id': 'edited-' + segment['id']} for segment in current['plan']['segments']]}
        with patch.object(self.runtime.accounts, 'wall_clock', return_value=later - 600):
            saved = self.runtime.accounts.save_plan(self.account.id, plan, current['revision'])
        self.runtime.scheduler.refresh()
        changed = list(self.db.campus_runs.find({'account_id': self.account.id, 'spec.revision': saved.revision}))
        self.assertEqual(len(changed), 3)
        self.assertTrue(all(row['status'] == 'skipped' for row in changed))
        self.assertEqual(len(self.fixture.posts()), 1)

    def test_empty_route_prunes_disabled_segment_and_marks_all_siblings(self):
        self.segmented(due=math.ceil(time.time() + 60))
        self.runtime.native.bridge.set_control(live=True, clock_ok=True)
        self.runtime.scheduler.refresh()
        current = self.runtime.store.get_account(self.account.id)
        self.runtime.accounts.save_plan(self.account.id, current['plan'] | {'enabled': False}, current['revision'])
        self.runtime.scheduler.refresh()
        self.assertFalse(self.runtime.scheduler.fallback.jobs)
        self.assertEqual([row['status'] for row in self.db.campus_runs.find({'account_id': self.account.id})], ['skipped'] * 3)

    def test_native_recovery_marks_interrupted_python_segment_uncertain_without_reset(self):
        run = self.segmented(due=math.ceil(time.time() + 60))[0]
        self.runtime.store.ensure_run(run)
        self.db.campus_runs.update_one({'_id': run.key}, {'$set': {
            'status': 'submitting', 'attempted_seat_id': '1', 'submitted_at': time.time()}})
        self.runtime.scheduler._reconcile_unregistered()
        row = self.runtime.store.get_run(run.key)
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['attempted_seat_id'], '1')
        self.assertNotIn('native_binding', row)

    def test_native_prepare_cannot_arm_segment_even_when_called_directly(self):
        run = self.segmented(due=math.ceil(time.time() + 60))[0]
        with self.assertRaises(AppError) as error:
            self.runtime.native.dispatch._prepare([run], [0], 'test', allow_rebind=False)
        self.assertEqual(error.exception.code, 'native_segmented_unsupported')
        self.assertEqual(self.fixture.posts(), [])

    def test_native_prepare_cannot_reopen_segmented_date_as_scalar(self):
        due = math.ceil(time.time() + 1)
        run = self.plan(due=due)
        self.db.campus_scheduled_cohorts.insert_one({'_id': run.account_id + ':' + run.target_date})
        self.runtime.native.bridge.set_control(live=True, clock_ok=True)
        with self.assertRaises(AppError) as error:
            self.runtime.native.dispatch._prepare([run], [0], 'test', allow_rebind=False)
        self.assertEqual(error.exception.code, 'native_cohort_handled')
        self.assertEqual(self.fixture.posts(), [])
        self.assertFalse((self.runtime.store.get_run(run.key) or {}).get('native_binding'))

    def test_legacy_native_binding_prevents_segmented_takeover(self):
        due = math.ceil(time.time() + 60)
        legacy = self.plan(due=due)
        self.runtime.store.ensure_run(legacy)
        binding = {'job': 'prior-native-owner', 'epoch': 1, 'session': 1}
        self.db.campus_runs.update_one({'_id': legacy.key}, {'$set': {'status': 'preparing', 'native_binding': binding}})
        self.segmented(due=due)
        self.runtime.native.bridge.set_control(live=True, clock_ok=True)
        self.runtime.scheduler.refresh()
        self.assertEqual(self.runtime.store.get_run(legacy.key)['native_binding'], binding)
        self.assertEqual(self.runtime.store.get_run(legacy.key)['status'], 'preparing')
        segments = list(self.db.campus_runs.find({'spec.operation_id': {'$ne': None}}))
        self.assertEqual(len(segments), 3)
        self.assertTrue(all(row['status'] == 'skipped' for row in segments))
        self.assertFalse(self.runtime.scheduler.fallback.jobs)
        self.assertEqual(self.fixture.posts(), [])
