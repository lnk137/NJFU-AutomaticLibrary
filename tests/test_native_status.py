"""Existing UI role boundary, truthful native status and phase diagnostics."""
from unittest.mock import patch
from support.native_runtime import NativeRuntimeCase


class NativeStatusTests(NativeRuntimeCase):
    def test_root_recovery_is_csrf_protected_and_rejects_clock_bypass(self):
        self.runtime.start()
        path='/api/local/admin/executor'
        response=self.client.post(path,json={'action':'recover'},base_url=self.origin,
            headers={'Origin':self.origin})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.request('POST',path,{'action':'recover','trust_clock':True}).status_code,400)
        self.assertEqual(self.request('POST',path,{'action':'pause'}).status_code,200)
        self.assertEqual(self.runtime.scheduler.status()['executor_state'],'paused')
        self.assertEqual(self.request('POST',path,{'action':'recover'}).status_code,200)
        self.assertFalse(self.runtime.scheduler.status()['allow_live'])
        self.assertEqual(self.request('POST',path,{'action':'resume'}).status_code,200)
        self.assertTrue(self.runtime.scheduler.status()['allow_live'])
        self.assertEqual(self.fixture.posts(),[])

    def test_student_only_sees_actionable_status_and_cannot_control_executor(self):
        self.runtime.start()
        self.client.set_cookie('njfu_autolib_session',self.runtime.auth._issue('student',
            account_id=self.account.id,student_id=self.students[0]),domain='127.0.0.1')
        response=self.client.get('/api/local/status',base_url=self.origin)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('executor',response.json)
        self.assertNotIn('last_wake_at',response.json)
        self.assertEqual(self.request('POST','/api/local/admin/executor',{'action':'pause'}).status_code,403)
        self.runtime.native.pause()
        status=self.client.get('/api/local/status',base_url=self.origin).json
        self.assertFalse(status['allow_live'])
        self.assertIn('管理员',status['execution_message'])

    def test_dashboard_has_native_health_and_completed_phase_measurements(self):
        run=self.plan();self.runtime.start()
        self.assertEqual(self.wait(run.key,{'success'})['status'],'success')
        response=self.client.get('/api/local/admin/dashboard',base_url=self.origin)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['runtime']['executor'],'cpp')
        self.assertEqual(response.json['runtime']['executor_state'],'ready')
        import time
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            row=self.db.campus_runs.find_one({'_id':run.key})
            if 'scheduler_timings' in row:break
            time.sleep(.01)
        self.assertIn('prepare',row.get('scheduler_timings',{}))
        self.assertIn('warm',row['scheduler_timings'])
        self.assertIn('transport_warmed_ns',row['native_schedule'])
        self.assertEqual(row['attempts'][0]['timings']['timing_version'],3)
        self.assertNotIn('fixture-password',str(response.json))
