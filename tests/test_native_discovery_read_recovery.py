"""A failed discovery read must not erase prepared, still-current work."""
from datetime import datetime
import math
import time
from unittest.mock import patch

from pymongo.errors import AutoReconnect
from local_app.models import ZONE, resolve_runs, validate_plan
from support.native_runtime import NativeRuntimeCase


class NativeDiscoveryReadRecoveryTests(NativeRuntimeCase):
    def prepared(self, *, segmented=False):
        due = math.ceil(time.time() + 60)
        if segmented:
            with patch.object(self.runtime.accounts, 'wall_clock', return_value=due - 600):
                self.account = self.runtime.accounts.save_plan(self.account.id, {
                    'seat_ids': ['1'], 'day_offset': 1, 'mode': 'day', 'begin': '09:00',
                    'end': '12:00', 'trigger': datetime.fromtimestamp(due, ZONE).strftime('%H:%M:%S'),
                    'enabled': True, 'segments': [
                        dict(id='morning', begin='09:00', end='12:00', seat_ids=['1']),
                        dict(id='afternoon', begin='13:00', end='15:00', seat_ids=['2'])]}, self.account.revision)
            runs = resolve_runs(self.account.id, self.account.revision,
                validate_plan(self.account.plan, self.catalog), datetime.fromtimestamp(due, ZONE).date())
        else:
            runs = (self.plan(due=due),)
        scheduler = self.runtime.scheduler
        self.runtime.native.bridge.set_control(live=True, clock_ok=True)
        scheduler.refresh()
        route = scheduler.fallback if segmented else scheduler
        route.tick()
        job = route.jobs[(runs[0].key, runs[0].revision)]
        self.assertTrue(job['prep'].result(timeout=5))
        self.assertIsNone(job['submit'])
        self.assertEqual(self.runtime.store.get_run(runs[0].key)['status'], 'preparing')
        return runs, route, job

    def transient_read(self):
        original = self.runtime.store.get_account
        failed = False
        def read(account_id):
            nonlocal failed
            if account_id == self.account.id and not failed:
                failed = True
                raise AutoReconnect('isolated discovery read failure')
            return original(account_id)
        return patch.object(self.runtime.store, 'get_account', side_effect=read)

    def assert_retained_after_read_failure(self, *, segmented=False):
        runs, route, job = self.prepared(segmented=segmented)
        before = [self.runtime.store.get_run(run.key) for run in runs]
        with self.transient_read():
            self.runtime.scheduler.refresh()
        self.assertEqual([self.runtime.store.get_run(run.key) for run in runs], before)
        self.assertIs(route.jobs.get((runs[0].key, runs[0].revision)), job)
        self.assertIn(self.account.id, self.runtime.accounts.busy)
        self.runtime.scheduler.refresh()
        self.assertIs(route.jobs.get((runs[0].key, runs[0].revision)), job)
        self.assertEqual(self.fixture.posts(), [])

    def test_prepared_native_work_survives_one_failed_account_read(self):
        self.assert_retained_after_read_failure()

    def test_prepared_segment_route_survives_one_failed_account_read(self):
        self.assert_retained_after_read_failure(segmented=True)

    def test_actual_plan_disable_still_prunes_after_read_recovery(self):
        runs, route, _ = self.prepared()
        with self.transient_read():
            self.runtime.scheduler.refresh()
        current = self.runtime.store.get_account(self.account.id)
        self.runtime.accounts.save_plan(self.account.id, current['plan'] | {'enabled': False}, current['revision'])
        self.runtime.scheduler.refresh()
        self.assertNotIn((runs[0].key, runs[0].revision), route.jobs)
        self.assertEqual(self.runtime.store.get_run(runs[0].key)['status'], 'skipped')
        self.assertEqual(self.fixture.posts(), [])

    def test_invalid_plan_is_not_retained_as_a_temporary_read_failure(self):
        runs, route, _ = self.prepared()
        self.db.campus_accounts.update_one({'_id': self.account.id}, {'$set': {'plan.seat_ids': ['not-a-seat']}})
        self.runtime.scheduler.refresh()
        self.assertNotIn((runs[0].key, runs[0].revision), route.jobs)
        self.assertEqual(self.runtime.store.get_run(runs[0].key)['status'], 'skipped')
        self.assertEqual(self.fixture.posts(), [])
