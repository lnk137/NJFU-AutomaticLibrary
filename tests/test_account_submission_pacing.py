"""Account-wide pacing through real scheduler/service/storage; fake school only."""
from dataclasses import replace
from local_app.models import Submission
from test_scheduled_segments import ScheduledSegmentFlowTests, DUE


class AccountSubmissionPacingTests(ScheduledSegmentFlowTests):
    def setUp(self):
        super().setUp()
        self.elapsed = 100.
        self.response_seconds = .2
        self.starts, self.ends = [], []
        self.reservations.monotonic = lambda: self.elapsed
        self.reservations.wait = self.advance
        client = self.accounts.client(self.account.id)
        original = client.submit
        def submit(run, seat):
            started = self.elapsed
            self.starts.append(started)
            too_fast = len(self.starts) > 1 and (
                started - self.starts[-2] < 1.2 - .00001 or started - self.ends[-1] < 1 - .00001)
            self.advance(self.response_seconds)
            self.ends.append(self.elapsed)
            if too_fast:
                self.calls.append((run.begin, seat))
                return Submission('rejected', False, 1, '请求频繁,请稍后重试', True,
                                  {'request_ms': self.response_seconds*1000})
            return replace(original(run, seat), timings={'request_ms': self.response_seconds*1000})
        client.submit = submit

    def advance(self, seconds):
        self.elapsed += seconds
        self.now += seconds

    def assert_floors(self):
        for start, prior_start, end in zip(self.starts[1:], self.starts, self.ends):
            self.assertGreaterEqual(start - prior_start + .000001, 1.2)
            self.assertGreaterEqual(start - end + .000001, 1)

    def test_successful_scheduled_segments_avoid_modelled_rate_limit(self):
        self.assertEqual(self.execute(), ['success'] * 3)
        self.assertEqual(len(self.starts), 3)
        self.assertEqual(self.starts[0], 100, 'The first request must not be delayed')
        self.assert_floors()
        self.assertLess(self.starts[-1] - self.starts[0], 2.5, 'Use max, not sum, of both floors')
        self.assertEqual(self.store.get_run(self.runs[1].key)['account_pacing']['state'], 'ready')
        self.assertEqual(self.store.get_run(self.runs[1].key)['account_pacing']['pacing_policy'], 'account_interval_v1')

    def test_exhausted_busy_segment_cools_before_following_segment(self):
        self.behavior['09:00'] = 'busy'
        self.assertEqual(self.execute(), ['failed', 'success', 'success'])
        self.assert_floors()
        self.assertEqual(len(self.starts), 6)

    def test_slow_response_requires_full_response_cooldown_across_segments(self):
        self.response_seconds = .65
        self.assertEqual(self.execute(), ['success'] * 3)
        self.assert_floors()
        self.assertAlmostEqual(self.starts[1] - self.starts[0], 1.65)

    def test_manual_segments_share_the_same_account_cooldown(self):
        self.now = DUE
        runs = [replace(r, target_date='2026-10-24', kind='manual',
                        operation_id='manual-' + str(i), expires_at=DUE + 20)
                for i, r in enumerate(self.runs[:2])]
        for run in runs:
            self.assertEqual(self.reservations.execute(run).status, 'success')
        self.assert_floors()
        self.assertEqual(len(self.starts), 2)

    def test_another_accounts_first_request_runs_during_cooldown(self):
        other = self.accounts.verify_and_save('fixture02', 'pass', 'Fixture', None)
        other = self.accounts.save_plan(other.id, self.account.plan, other.revision)
        other_run = replace(self.runs[0], account_id=other.id, revision=other.revision)
        self.now = DUE
        self.assertEqual(self.reservations.execute(self.runs[0]).status, 'success')
        ran_other = []
        def wait(seconds):
            if not ran_other:
                before = self.elapsed
                result = self.reservations.execute(other_run)
                ran_other.append((result.status, self.elapsed - before))
            self.advance(seconds)
        self.reservations.wait = wait
        self.assertEqual(self.reservations.execute(self.runs[1]).status, 'success')
        self.assertEqual(ran_other, [('success', 0.)])
        self.assert_floors()

    def test_manual_guard_closure_during_cooldown_stops_next_segment(self):
        self.now = DUE
        first, second = [replace(r, target_date='2026-10-24', kind='manual',
                                 operation_id='manual-' + str(i), expires_at=DUE + 20)
                         for i, r in enumerate(self.runs[:2])]
        self.assertEqual(self.reservations.execute(first).status, 'success')
        permitted = [True]
        def wait(seconds):
            self.advance(seconds)
            permitted[0] = False
        self.reservations.wait = wait
        result = self.reservations.execute(second, manual_guard=lambda: permitted[0])
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(len(self.starts), 1)

    def stop_during_cooldown(self, change, *, expires_at=None, manual_guard=None):
        self.now = DUE
        self.assertEqual(self.reservations.execute(self.runs[0]).status, 'success')
        def wait(seconds):
            self.advance(seconds)
            change()
        self.reservations.wait = wait
        second = replace(self.runs[1], expires_at=expires_at)
        result = self.reservations.execute(second, manual_guard=manual_guard)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.store.get_run(second.key)['account_pacing']['state'], 'stopped')

    def test_pause_during_account_cooldown_stops_without_another_post(self):
        self.stop_during_cooldown(lambda: setattr(self.reservations, 'guard', lambda: False))

    def test_live_gate_during_account_cooldown_stops_without_another_post(self):
        self.stop_during_cooldown(lambda: setattr(self.reservations, 'allow_live', False))

    def test_revision_change_during_account_cooldown_stops_without_another_post(self):
        self.stop_during_cooldown(lambda: self.store.accounts.update_one(
            {'_id': self.account.id}, {'$inc': {'revision': 1}}))

    def test_access_revocation_during_account_cooldown_stops_without_another_post(self):
        self.stop_during_cooldown(lambda: setattr(self.accounts, 'is_allowed', lambda _: False))

    def test_expiry_during_cooldown_does_not_extend_execution_window(self):
        self.stop_during_cooldown(lambda: None, expires_at=DUE + .3)

    def test_existing_successful_or_failed_runs_are_not_replayed(self):
        self.behavior['12:00'] = 'rate'
        self.assertEqual(self.execute(), ['success', 'failed', 'skipped'])
        self.assert_floors()
        before = list(self.starts)
        self.scheduler._submit(self.runs[0])
        self.assertEqual(self.starts, before)

    def test_submit_exception_is_uncertain_and_cannot_replay_the_original_task(self):
        self.behavior['09:00'] = 'crash'
        self.assertEqual(self.execute(), ['uncertain', 'skipped', 'skipped'])
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.records, [])
        # No reliable response duration exists after a transport exception.
        self.assertAlmostEqual(self.reservations._account_not_before[self.account.id],
                               self.elapsed + 1.2)
        self.scheduler._submit(self.runs[0])
        self.assertEqual(self.reservations.execute(self.runs[0]).status, 'uncertain')
        self.assertEqual(len(self.starts), 1)

    def fail_cooldown_persistence_at(self, state):
        self.now = DUE
        self.assertEqual(self.reservations.execute(self.runs[0]).status, 'success')
        original = self.store.transition_run
        def transition(key, states, target, fields=None, **kwargs):
            if fields and (fields.get('account_pacing') or {}).get('state') == state:
                return False
            return original(key, states, target, fields, **kwargs)
        self.store.transition_run = transition
        second = self.runs[1]
        self.assertEqual(self.reservations.execute(second).status, 'uncertain')
        self.assertEqual(len(self.starts), 1, 'No next POST without durable pacing state')
        self.assertEqual(self.reservations.execute(second).status, 'uncertain')
        self.assertEqual(len(self.starts), 1, 'An uncertain task cannot re-enter after a failed write')

    def test_waiting_pacing_state_write_failure_blocks_the_next_segment(self):
        self.fail_cooldown_persistence_at('waiting')

    def test_ready_pacing_state_write_failure_blocks_the_next_segment(self):
        self.fail_cooldown_persistence_at('ready')


def load_tests(loader, standard, pattern):
    return loader.loadTestsFromNames([
        'test_account_submission_pacing.AccountSubmissionPacingTests.' + name
        for name in AccountSubmissionPacingTests.__dict__ if name.startswith('test_')])
