"""Lost warmup diagnostics and recoverable second-attempt faults, fixture only."""
from collections import Counter
import math
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from local_app.models import AppError
from support.native_runtime import NativeRuntimeCase
from support.native_school import FixtureTransport


class NativeWarmRecoveryTests(NativeRuntimeCase):
    def diagnostics(self, run, predicate=lambda value: bool(value)):
        until = time.monotonic()+3
        while time.monotonic() < until:
            value = self.runtime.store.get_run(run.key).get('scheduler_timings', {})
            if predicate(value):return value
            time.sleep(.005)
        self.fail('Expected terminal diagnostics were not saved')

    def test_fourteen_accounts_recover_two_second_warm_failures_without_extra_posts(self):
        accounts = [self.account]+[self.runtime.accounts.verify_and_save(student, 'fixture-password')
                                   for student in self.students[1:14]]
        due = math.ceil(time.time()+8)
        runs = [self.plan(account, due=due) for account in accounts]
        targets = {accounts[0].id, accounts[1].id}
        calls = Counter();original = self.runtime.scheduler._warm
        before_auth = dict(self.fixture.auth_posts)

        def warm(run, phase=None):
            calls[run.account_id] += 1
            if run.account_id in targets and calls[run.account_id] == 2:
                raise AppError('network_error', 'fixture transient connection failure', 503)
            return original(run, phase)

        with patch.object(self.runtime.scheduler, '_warm', side_effect=warm):
            self.runtime.start()
            rows = [self.wait(run.key, {'success', 'failed', 'skipped'}, 16) for run in runs]
        self.assertTrue(all(row['status'] == 'success' for row in rows),
                        [(row['status'], row.get('message')) for row in rows])
        self.assertEqual(len(self.fixture.posts()), 14)
        self.assertTrue(all(count == 1 for count in self.fixture.post_counts.values()))
        for account, run, row in zip(accounts, runs, rows):
            self.assertEqual(self.fixture.auth_posts[account.student_id], before_auth[account.student_id]+1)
            post = next(p for p in self.fixture.posts() if p['student_id'] == account.student_id)
            self.assertGreaterEqual(post['received_ns'], row['native_schedule']['due_ns'])
            if account.id in targets:
                phase = self.diagnostics(run)['warm']
                self.assertGreaterEqual(phase['attempt_count'], 3)
                self.assertIn('network_error', [a.get('error_code') for a in phase['attempts']])

    def test_permanent_identity_fault_is_not_retried_and_error_text_is_not_saved(self):
        run = self.plan();calls = []

        def fail(spec, phase=None):
            calls.append(spec.key)
            raise AppError('identity_mismatch', 'fixture-password cookie=secret-token', 502)

        with patch.object(self.runtime.scheduler, '_warm', side_effect=fail):
            self.runtime.start()
            self.assertEqual(self.wait(run.key, {'skipped', 'failed'})['status'], 'skipped')
        phase = self.diagnostics(run)['warm']
        self.assertEqual(len(calls), 1)
        self.assertEqual(phase['error_code'], 'identity_mismatch')
        self.assertIn('elapsed_ms', phase)
        self.assertNotIn('secret-token', str(self.runtime.store.get_run(run.key)))
        self.assertEqual(self.fixture.posts(), [])

    def test_running_warm_at_cutoff_preserves_stop_and_late_completion_timings(self):
        run = self.plan();entered = threading.Event();release = threading.Event()
        self.addCleanup(release.set)

        def slow(spec, phase=None):
            entered.set();release.wait(10)
            return False

        with patch.object(self.runtime.scheduler, '_warm', side_effect=slow):
            self.runtime.start();self.assertTrue(entered.wait(4))
            row = self.wait(run.key, {'skipped', 'failed'})
            self.assertEqual(row['status'], 'skipped')
            first = self.diagnostics(run)
            self.assertEqual(first['stop']['waiting_stage'], 'warm')
            self.assertEqual(first['warm']['outcome'], 'running')
            release.set()
            final = self.diagnostics(run, lambda d: d.get('warm', {}).get('outcome') == 'failed')
            self.assertGreater(final['warm']['elapsed_ms'], 100)
            self.assertGreaterEqual(final['warm']['finished_at'], final['stop']['stopped_at'])
        self.assertEqual(self.fixture.posts(), [])
        self.assertNotIn('native_binding', self.runtime.store.get_run(run.key))

    def test_payload_expiry_is_rechecked_after_connection_warmup(self):
        run = self.plan(delay=1)
        self.runtime.native.bridge.set_control(live=True,clock_ok=True)
        client = self.runtime.accounts.client(run.account_id)
        phase = {'attempt_count': 1}

        def age_payload(spec, budget):
            client._prepared = [(key, created-3, fields) for key, created, fields in client._prepared]

        with patch.object(self.runtime.native.dispatch, 'warm_transport', side_effect=age_payload):
            self.runtime.scheduler._warm(run, phase)
        self.assertTrue(phase.get('refresh_needed'), phase)
        self.assertIn('transport_ms', phase)
        self.assertEqual(self.fixture.posts(), [])

    def test_failed_prepare_keeps_sanitized_error_and_duration(self):
        run = self.plan()
        original = self.runtime.accounts.client

        def client(account_id, **kwargs):
            if kwargs.get('force'):raise AppError('login_failed', 'do not persist fixture-password', 401)
            return original(account_id, **kwargs)

        with patch.object(self.runtime.accounts, 'client', side_effect=client):
            self.runtime.start()
            self.assertEqual(self.wait(run.key, {'failed', 'skipped'})['status'], 'failed')
        phase = self.diagnostics(run)['prepare']
        self.assertEqual(phase['error_code'], 'login_failed')
        self.assertIn('school_login_ms', phase)
        self.assertEqual(self.fixture.posts(), [])

    def test_stop_diagnostics_cannot_overwrite_success_or_accept_arbitrary_text(self):
        run = self.plan();self.runtime.store.ensure_run(run)
        self.runtime.store.transition_run(run.key, ('queued',), 'skipped', {})
        phases = {'warm': {'elapsed_ms': 1.0, 'outcome': 'failed', 'error_code': 'network_error',
                          'password': 'never-save-this', 'payload_remaining_ms': float('nan'),
                          'attempts': [{'attempt_count': i, 'error_code': 'network_error',
                                        'message': 'never-save-this'} for i in range(30)]},
                  'stop': {'reason': 'prepare_window_missed', 'waiting_stage': 'warm', 'stopped_at': time.time()}}
        self.assertTrue(self.runtime.store.save_scheduler_timings(run, phases))
        saved = self.runtime.store.get_run(run.key)['scheduler_timings']
        self.assertEqual(saved['warm']['error_code'], 'network_error')
        self.assertLessEqual(len(saved['warm']['attempts']), 11)
        self.assertNotIn('never-save-this', str(saved))
        self.assertNotIn('payload_remaining_ms', saved['warm'])
        self.runtime.store.transition_run(run.key, ('skipped',), 'success', {})
        self.assertFalse(self.runtime.store.save_scheduler_timings(run, phases))

    def test_running_retry_does_not_report_previous_attempt_as_its_completion(self):
        run = self.plan();entered = threading.Event();release = threading.Event()
        phase = {'attempt_count': 1, 'finished_at': 10., 'elapsed_ms': 123.,
                 'attempts': [{'attempt_count': 1, 'finished_at': 10., 'elapsed_ms': 123.}]}
        job = {'run': run, 'timings': {'warm': phase}}

        def slow(spec, values):
            entered.set();release.wait(3)
            return True

        future = self.runtime.scheduler._queue_stage(job, 'warm', self.runtime.scheduler.warm_pool, slow)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(phase['outcome'], 'running')
            self.assertNotIn('finished_at', phase)
            self.assertNotIn('elapsed_ms', phase)
        finally:
            release.set();future.result(timeout=3)
        self.assertEqual(len(phase['attempts']), 2)
        self.assertEqual(phase['attempts'][0]['elapsed_ms'], 123.)

    def test_persistent_transient_fault_has_bounded_paced_attempts_and_no_post(self):
        run = self.plan();calls = []

        def fail(spec, phase=None):
            calls.append(time.monotonic())
            raise AppError('network_error', 'synthetic temporary connection fault', 503)

        with patch.object(self.runtime.scheduler, '_warm', side_effect=fail):
            self.runtime.start()
            self.assertEqual(self.wait(run.key, {'skipped', 'failed'})['status'], 'skipped')
        phase = self.diagnostics(run)['warm']
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(b-a >= .09 for a, b in zip(calls, calls[1:])))
        self.assertEqual(len(phase['attempts']), 4)
        self.assertTrue(all(a['error_code'] == 'network_error' for a in phase['attempts']))
        self.assertEqual(self.fixture.posts(), [])

    def test_diagnostic_write_failure_does_not_change_skip_or_cause_a_post(self):
        run = self.plan()
        with patch.object(self.runtime.scheduler, '_warm', side_effect=AppError('identity_mismatch', 'synthetic', 502)), \
                patch.object(self.runtime.store, 'save_scheduler_timings', side_effect=OSError('synthetic IO failure')), \
                self.assertLogs('local_app.scheduler', level='WARNING'):
            self.runtime.start()
            self.assertEqual(self.wait(run.key, {'skipped', 'failed'})['status'], 'skipped')
            self.runtime.scheduler.diagnostic_pool.shutdown(wait=True)
        self.assertEqual(self.runtime.store.get_run(run.key)['status'], 'skipped')
        self.assertEqual(self.fixture.posts(), [])

    def test_stale_plan_cannot_implicitly_login_during_warmup(self):
        run = self.plan()
        self.runtime.native.bridge.set_control(live=True,clock_ok=True)
        row = self.runtime.store.get_account(run.account_id)
        self.runtime.accounts.save_plan(run.account_id,row['plan']|{'enabled':False},row['revision'])
        self.runtime.accounts.discard(run.account_id)
        before = dict(self.fixture.auth_posts)
        with patch.object(self.runtime.accounts,'client',wraps=self.runtime.accounts.client) as client:
            try:self.runtime.scheduler._warm(run,{})
            except AppError:pass
            self.assertFalse(client.called,'A stale plan reached the implicit-login path')
        self.assertEqual(self.fixture.auth_posts,before)
        self.assertEqual(self.fixture.posts(),[])

    def test_unknown_false_warm_result_does_not_gain_automatic_retries(self):
        run = self.plan()
        with patch.object(self.runtime.scheduler,'_warm',return_value=False) as warm:
            self.runtime.start()
            self.assertEqual(self.wait(run.key,{'failed','skipped'})['status'],'skipped')
        self.assertEqual(warm.call_count,1)
        self.assertEqual(self.fixture.posts(),[])

    def test_second_payload_http_failure_recovers_through_canonical_client(self):
        run = self.plan();original = FixtureTransport.send;keys = []

        def send(transport,request,**kwargs):
            response = original(transport,request,**kwargs)
            if urlsplit(request.url).path == '/ic-web/login/publicKey':
                keys.append(time.monotonic())
                if len(keys) == 2:
                    response.status_code = 503
                    response._content = b'{"code":-1}'
            return response

        with patch.object(FixtureTransport,'send',new=send):
            self.runtime.start()
            self.assertEqual(self.wait(run.key,{'failed','skipped','success'})['status'],'success')
        phase = self.diagnostics(run)['warm']
        failed = next(a for a in phase['attempts'] if a.get('error_code') == 'school_http')
        self.assertEqual(failed['error_stage'],'payload')
        self.assertGreaterEqual(failed['payload_prepare_ms'],0)
        self.assertNotIn('transport_ms',failed)
        self.assertEqual(len(self.fixture.posts()),1)
        self.assertEqual(self.fixture.auth_posts[self.students[0]],2)
