"""Batch API uses the actual governed Runtime and loopback TLS school only."""
from datetime import datetime
import hashlib
import time

from local_app.models import ZONE
from local_app.recovery_policy import OCCUPIED_REJECTION
from support.auth_fixture import root_session
from support.native_runtime import NativeRuntimeCase


class NativeTodayBatchTests(NativeRuntimeCase):
    def start_manual(self):
        self.now = datetime.now(ZONE).replace(hour=8, minute=10, second=0, microsecond=0)
        self.runtime.start()
        self.runtime.reservations.wall_clock = lambda: self.now.timestamp()

    def preview(self):
        data = {'expected_revision': self.account.revision, 'date': self.now.date().isoformat(),
                'segments': [{'begin': '09:00', 'end': '11:00', 'seat_ids': ['2']},
                             {'begin': '13:00', 'end': '15:00', 'seat_ids': ['3', '4']},
                             {'begin': '17:00', 'end': '19:00', 'seat_ids': ['5']}]}
        response = self.request('POST', f'/api/local/accounts/{self.account.id}/today/batches/preview', data)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def submit(self, preview):
        return self.request('POST', f'/api/local/accounts/{self.account.id}/today/batches/submit',
                            {'preview_token': preview['preview_token']})

    def finished(self, response):
        self.assertIn(response.status_code, (200, 202), response.json)
        batch = response.json
        until = time.monotonic() + 8
        while batch['state'] == 'running' and time.monotonic() < until:
            time.sleep(.02)
            response = self.client.get(f'/api/local/accounts/{self.account.id}/today/batches/{batch["batch_id"]}',
                                       base_url=self.origin)
            self.assertEqual(response.status_code, 200, response.json)
            batch = response.json
        self.assertNotEqual(batch['state'], 'running', batch)
        return batch

    def test_native_runtime_batch_keeps_scheduled_plan_and_never_arms_manual_runs(self):
        self.plan(delay=600)
        before = self.runtime.store.get_account(self.account.id)
        protected = {key: before.get(key) for key in ('plan', 'date_exceptions', 'seat_preferences',
                                                     'revision', 'credential_revision', 'secret')}
        self.start_manual()
        preview = self.preview()
        self.assertEqual(self.fixture.posts(), [])
        result = self.finished(self.submit(preview))
        self.assertEqual([s['status'] for s in result['segments']], ['success'] * 3)
        self.assertEqual([p['seat_id'] for p in self.fixture.posts()], ['2', '3', '5'])
        again = self.finished(self.submit(preview))
        self.assertEqual(again, result)
        self.assertEqual(len(self.fixture.posts()), 3)
        for row in result['segments']:
            run = self.runtime.store.get_run(row['run_id'])
            self.assertEqual(run['spec']['kind'], 'manual')
            self.assertNotIn('native_binding', run)
            self.assertEqual(self.runtime.native.process.job_status('r' + hashlib.sha256(run['_id'].encode()).hexdigest()),
                             'NOTFOUND')
        after = self.runtime.store.get_account(self.account.id)
        self.assertEqual({key: after.get(key) for key in protected}, protected)
        self.assertEqual(self.runtime.scheduler.status()['capacity'], 20)

    def test_native_runtime_occupied_middle_segment_does_not_end_later_intervals(self):
        self.start_manual()
        for index in (2, 3):
            self.fixture.faults[(self.students[0], index)] = {
                'reject': True, 'code': OCCUPIED_REJECTION[0], 'message': OCCUPIED_REJECTION[1]}
        result = self.finished(self.submit(self.preview()))
        self.assertEqual([s['status'] for s in result['segments']], ['success', 'failed', 'success'])
        self.assertEqual([p['seat_id'] for p in self.fixture.posts()], ['2', '3', '4', '5'])

    def test_native_global_revocation_blocks_batch_before_any_post(self):
        self.start_manual()
        preview = self.preview()
        self.runtime.native.bridge.set_control(live=False, clock_ok=True)
        response = self.submit(preview)
        self.assertEqual(response.status_code, 403, response.json)
        self.assertEqual(response.json['code'], 'live_disabled')
        self.assertEqual(self.fixture.posts(), [])
        self.assertEqual(self.db.campus_today_batches.count_documents({}), 0)

    def test_native_runtime_restart_keeps_stopped_batch_and_never_resubmits(self):
        from local_app.runtime import create_runtime
        self.start_manual()
        self.fixture.faults[(self.students[0], 1)] = {'missing_receipt': True}
        preview = self.preview()
        first = self.finished(self.submit(preview))
        self.assertEqual([s['status'] for s in first['segments']], ['uncertain', 'not_submitted', 'not_submitted'])
        self.runtime.close()
        self.runtime = create_runtime(self.settings, client_factory=self.client_factory, native_factory=self.native_factory)
        self.addCleanup(self.runtime.close)
        self.client = self.runtime.app.test_client()
        root_session(self.runtime.auth, self.client)
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']
        self.start_manual()
        again = self.finished(self.submit(preview))
        self.assertEqual(again, first)
        self.assertEqual(len(self.fixture.posts()), 1)
