"""Independent cutoff and slow-diagnostic regressions for warmup recovery."""
from concurrent.futures import Future
from types import SimpleNamespace
import math
import threading
import time
import unittest
from unittest.mock import Mock, patch

from local_app.models import AppError
from local_app.native.scheduler import CampusScheduler
from support.native_runtime import NativeRuntimeCase


class WarmRetryCutoffTests(unittest.TestCase):
    def setup_job(self, remaining, *, pending=False):
        scheduler = CampusScheduler.__new__(CampusScheduler)
        scheduler.warm_pool = object()
        scheduler.preflights = set()
        scheduler._warm = Mock()
        replacement = Future()
        scheduler._queue_stage = Mock(return_value=replacement)
        run = SimpleNamespace(due_at=100.)
        warm = Future()
        if not pending:
            warm.set_result(False)
        phase = {'attempt_count': 2, 'error_code': 'network_error', 'finished_at': 99.5}
        job = {'run': run, 'warm': warm, 'timings': {'warm': phase}}
        return scheduler, job, 100.-remaining, replacement

    def test_exact_quarter_second_budget_does_not_start_another_warmup(self):
        scheduler, job, now, _ = self.setup_job(.25)
        self.assertFalse(scheduler.payload_ready(job, now))
        scheduler._queue_stage.assert_not_called()

    def test_recovery_can_start_just_outside_the_cutoff(self):
        scheduler, job, now, replacement = self.setup_job(.251)
        self.assertFalse(scheduler.payload_ready(job, now))
        scheduler._queue_stage.assert_called_once()
        self.assertIs(job['warm'], replacement)

    def test_in_progress_warmup_never_starts_an_overlapping_retry(self):
        scheduler, job, now, _ = self.setup_job(1., pending=True)
        self.assertFalse(scheduler.payload_ready(job, now))
        scheduler._queue_stage.assert_not_called()


class SlowStopDiagnosticTests(NativeRuntimeCase):
    def test_blocked_failed_account_diagnostic_does_not_hold_peer_submission(self):
        bad = self.runtime.accounts.verify_and_save(self.students[1], 'fixture-password')
        due = math.ceil(time.time()+8)
        good_run = self.plan(self.account, due=due)
        bad_run = self.plan(bad, due=due)
        entered = threading.Event()
        release = threading.Event()
        original_warm = self.runtime.scheduler._warm
        original_save = self.runtime.store.save_scheduler_timings

        def warm(run, phase=None):
            if run.account_id == bad.id:
                raise AppError('identity_mismatch', 'synthetic identity fault', 502)
            return original_warm(run, phase)

        def save(run, phases):
            if run.account_id == bad.id and 'stop' in phases:
                entered.set()
                release.wait(5)
            return original_save(run, phases)

        try:
            with patch.object(self.runtime.scheduler, '_warm', side_effect=warm), \
                    patch.object(self.runtime.store, 'save_scheduler_timings', side_effect=save):
                self.runtime.start()
                row = self.wait(good_run.key, {'success', 'failed', 'skipped'}, 15)
                self.assertEqual(row['status'], 'success')
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.runtime.store.get_run(bad_run.key)['status'], 'skipped')
                self.assertEqual(len(self.fixture.posts()), 1)
                post = self.fixture.posts()[0]
                self.assertEqual(post['student_id'], self.account.student_id)
                self.assertGreaterEqual(post['received_ns'], row['native_schedule']['due_ns'])
                release.set()
                self.runtime.scheduler.diagnostic_pool.shutdown(wait=True)
        finally:
            release.set()
