"""New domains retain existing session, owner and CSRF boundaries."""
from copy import deepcopy
import unittest
import test_today_booking as helper


class CalendarMailApiTests(unittest.TestCase):
    setUpClass = classmethod(helper.TodayApiTests.setUpClass.__func__)
    tearDownClass = classmethod(helper.TodayApiTests.tearDownClass.__func__)
    setUp = helper.TodayApiTests.setUp
    tearDown = helper.TodayApiTests.tearDown

    def path(self, suffix, account=None):
        return f'/api/local/accounts/{account or self.account.id}/' + suffix

    def send(self, suffix, data, method='POST'):
        return self.client.open(self.path(suffix), method=method, json=data,
            headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)

    def student(self):
        self.client.set_cookie('njfu_autolib_session', self.runtime.auth._issue('student',
            account_id=self.account.id, student_id='fixture01'), domain='127.0.0.1')

    def test_calendar_read_only_and_bounded(self):
        before = deepcopy(self.runtime.store.get_account(self.account.id))
        response = self.client.get(self.path('calendar?start=2026-10-23&end=2026-11-06'), base_url=self.origin)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['days']), 15)
        self.assertEqual(before, self.runtime.store.get_account(self.account.id))
        self.assertEqual(self.client.get(self.path('calendar?start=2026-10-01&end=2026-12-01'), base_url=self.origin).status_code, 400)
        self.student()
        self.assertEqual(self.client.get(self.path('calendar?start=2026-10-23&end=2026-10-24', 'another'), base_url=self.origin).status_code, 404)

    def test_mutations_require_csrf_and_ownership(self):
        self.student()
        self.assertEqual(self.client.get('/api/local/admin/mail',base_url=self.origin).status_code,403)
        for suffix in ('notifications','notifications/verification','cancellations/preview','cancellations/submit'):
            response=self.client.open(self.path(suffix),method='PUT' if suffix=='notifications' else 'POST',json={},headers={'Origin':self.origin},base_url=self.origin)
            self.assertEqual(response.status_code,403)
        for suffix in ('notifications','cancellations/arbitrary'):
            self.assertEqual(self.client.get(self.path(suffix,'another'),base_url=self.origin).status_code,404)

    def test_mail_binding_is_independent_of_plan(self):
        captured=[]; service=self.runtime.notifications
        service.sender=captured.append; service.clock=lambda:self.now
        before=deepcopy(self.runtime.store.get_account(self.account.id))
        response=self.send('notifications/verification',{'address':'fixture@example.com'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('code',response.json)
        service.run_once()
        import re
        code=re.search(r'\b\d{6}\b',captured[0]['body']).group()
        confirmed=self.send('notifications/verification/confirm',{'challenge_id':response.json['challenge_id'],'code':code})
        self.assertEqual(confirmed.status_code,200,confirmed.json)
        self.assertTrue(confirmed.json['verified'])
        data={'revision':confirmed.json['revision'],'results_enabled':True,'reminders_enabled':True,'reminder_minutes':30}
        self.assertEqual(self.send('notifications',data,'PUT').status_code,200)
        self.assertEqual(before,self.runtime.store.get_account(self.account.id))
        self.assertEqual(self.send('notifications',data,'PUT').status_code,409)

    def test_cancellation_requires_explicit_confirmation(self):
        from local_app.models import SchoolRecord
        self.school.records=[SchoolRecord('cancel-api','fixture01',self.seat,'2026-10-23 09:00:00','2026-10-23 12:00:00',1027)]
        calls=[]
        def cancel(record_id):
            self.assertTrue(self.school.cancellation_guard());calls.append(record_id)
            self.school.records=[];return {'code':0}
        self.school.cancel=cancel
        preview=self.send('cancellations/preview',{'record_id':'cancel-api','expected_revision':self.account.revision})
        self.assertEqual(preview.status_code,200,preview.json);self.assertEqual(calls,[])
        self.assertEqual(self.client.get(self.path('cancellations/submit'),base_url=self.origin).status_code,404)
        result=self.send('cancellations/submit',{'preview_token':preview.json['preview_token']})
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['state'],'success')
        self.assertEqual(self.send('cancellations/submit',{'preview_token':preview.json['preview_token']}).json,result.json)
        self.assertEqual(calls,['cancel-api'])
