"""Formal Runtime regressions for preserved bounded/fallback/manual behavior."""
from datetime import datetime
import hashlib
import math
import time
from unittest.mock import patch
from local_app.models import ZONE
from support.native_runtime import NativeRuntimeCase

class NativeBoundaryTests(NativeRuntimeCase):
    def candidate_pins(self):
        return patch.multiple('local_app.cas',
            NEW_LOGIN_SCRIPT_SHA256=hashlib.sha256(self.fixture.new_login_script.encode()).hexdigest(),
            NEW_LOGIN_CSS_SHA256=hashlib.sha256(self.fixture.new_login_css.encode()).hexdigest(),
            NEW_LOGIN_KEY_SHA256=self.fixture.new_login_key_digest)

    def test_candidate_new_form_through_loopback_tls_and_native_sender(self):
        self.fixture.new_login=True
        with self.candidate_pins():
            due=math.ceil(time.time()+9);run=self.plan(due=due);self.runtime.start()
            row=self.wait(run.key,{'success','failed','skipped'},15)
            self.assertEqual(row['status'],'success',row.get('message'))
            self.assertEqual(row['execution_source'],'native_campus_first')
            self.assertGreaterEqual(row['attempts'][0]['timings']['request_started_at'],due)
            self.assertEqual(len(self.fixture.posts()),1)
            self.assertEqual(list(self.fixture.post_counts.values()),[1])

    def test_candidate_fourteen_new_forms_keep_accounts_independent(self):
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password')
                                 for student in self.students[1:14]]
        self.fixture.new_login=True
        baseline=dict(self.fixture.auth_posts)
        for student in self.students[:14]:self.fixture.prelogin_failures[student]=3
        with self.candidate_pins():
            due=math.ceil(time.time()+24);runs=[self.plan(account,due=due) for account in accounts]
            plans={a.id:self.runtime.store.get_account(a.id)['plan'] for a in accounts}
            self.runtime.start();rows=[self.wait(run.key,{'success','failed','skipped'},33) for run in runs]
            self.assertTrue(all(row['status']=='success' for row in rows),
                            [(row['status'],row.get('message')) for row in rows])
            self.assertEqual(len(self.fixture.posts()),14)
            self.assertTrue(all(count==1 for count in self.fixture.post_counts.values()))
            self.assertTrue(all(row['attempts'][0]['timings']['request_started_at']>=due for row in rows))
            self.assertTrue(all(self.fixture.auth_posts[student]==baseline[student]+1 for student in self.students[:14]))
            self.assertEqual({a.id:self.runtime.store.get_account(a.id)['plan'] for a in accounts},plans)

    def test_candidate_challenge_stops_only_its_own_account(self):
        other=self.runtime.accounts.verify_and_save(self.students[1],'fixture-password')
        self.fixture.new_login=True;self.fixture.auth_challenges.add(self.students[0])
        baseline=dict(self.fixture.auth_posts)
        with self.candidate_pins():
            due=math.ceil(time.time()+8);first=self.plan(due=due);second=self.plan(other,due=due)
            self.runtime.start()
            failed=self.wait(first.key,{'failed','skipped'},5)
            good=self.wait(second.key,{'success','failed','skipped'},12)
            self.assertEqual(failed['status'],'failed');self.assertIn('人工验证',failed['message'])
            self.assertEqual(self.fixture.auth_posts.get(self.students[0]),baseline[self.students[0]])
            self.assertEqual(good['status'],'success')
            self.assertEqual([event['student_id'] for event in self.fixture.posts()],[self.students[1]])

    def test_candidate_fourteen_accounts_recover_from_prelogin_outage(self):
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password')
                                 for student in self.students[1:14]]
        due=math.ceil(time.time()+24);runs=[self.plan(account,due=due) for account in accounts]
        plans={a.id:self.runtime.store.get_account(a.id)['plan'] for a in accounts}
        for student in self.students[:14]:self.fixture.prelogin_failures[student]=3
        self.runtime.start();rows=[self.wait(run.key,{'success','failed','skipped'},33) for run in runs]
        self.assertTrue(all(row['status']=='success' for row in rows),
                        [(row['status'],row.get('message')) for row in rows])
        self.assertTrue(all(row.get('execution_source')=='native_campus_first' for row in rows))
        self.assertTrue(all(row['attempts'][0]['timings']['request_started_at']>=due for row in rows))
        self.assertEqual(len(self.fixture.posts()),14)
        self.assertTrue(all(value==1 for value in self.fixture.post_counts.values()))
        self.assertEqual({a.id:self.runtime.store.get_account(a.id)['plan'] for a in accounts},plans)

    def test_candidate_unknown_login_result_is_not_retried(self):
        due=math.ceil(time.time()+8);run=self.plan(due=due)
        self.fixture.login_failures[self.students[0]]=2
        self.runtime.start();row=self.wait(run.key,{'failed','skipped'},5)
        self.assertEqual(row['status'],'failed')
        self.assertIn('结果未确认',row['message'])
        time.sleep(.3)
        self.assertEqual(self.fixture.login_failures[self.students[0]],1)
        self.assertFalse(row.get('native_binding'))
        self.assertEqual(self.fixture.posts(),[])

    def test_candidate_public_preflight_retry_reuses_authenticated_session(self):
        student=self.students[0];baseline=self.fixture.auth_posts[student]
        due=math.ceil(time.time()+9);run=self.plan(due=due)
        self.fixture.public_failures[student]=1
        self.runtime.start();row=self.wait(run.key,{'success','failed','skipped'},15)
        self.assertEqual(row['status'],'success',row.get('message'))
        self.assertEqual(self.fixture.auth_posts[student],baseline+1)
        self.assertEqual(self.fixture.public_failures[student],0)
        self.assertEqual(len(self.fixture.posts()),1)

    def test_candidate_plan_disabled_during_outage_never_posts(self):
        due=math.ceil(time.time()+9);run=self.plan(due=due)
        self.fixture.prelogin_failures[self.students[0]]=30
        self.runtime.start();deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            row=self.runtime.store.get_run(run.key)
            if row and row['status']=='queued' and '重试' in row.get('message',''):break
            time.sleep(.01)
        else:self.fail('No waiting preflight observed')
        current=self.runtime.store.get_account(self.account.id)
        self.runtime.accounts.save_plan(self.account.id,current['plan']|{'enabled':False},current['revision'])
        while time.time()<due+.3:time.sleep(.03)
        self.assertEqual(self.fixture.posts(),[])
        row=self.runtime.store.get_run(run.key)
        self.assertNotIn(row['status'],('success','submitting','uncertain'))
        self.assertFalse(row.get('native_binding'))

    def test_one_failed_login_does_not_delay_another_account(self):
        other=self.runtime.accounts.verify_and_save(self.students[1],'fixture-password')
        due=math.ceil(time.time()+6);first=self.plan(due=due);second=self.plan(other,due=due)
        self.fixture.accounts[self.students[0]]['password']='changed-only-in-fixture'
        self.runtime.start()
        self.assertEqual(self.wait(second.key,{'success','failed'})['status'],'success')
        self.assertEqual(self.wait(first.key,{'failed','skipped'})['status'],'failed')
        self.assertEqual([event['student_id'] for event in self.fixture.posts()],[self.students[1]])

    def test_transient_preflight_failure_retries_without_affecting_peer(self):
        other=self.runtime.accounts.verify_and_save(self.students[1],'fixture-password')
        due=math.ceil(time.time()+7);first=self.plan(due=due);second=self.plan(other,due=due)
        self.fixture.prelogin_failures[self.students[0]]=2
        self.runtime.start()
        self.assertEqual(self.wait(first.key,{'success','failed'},11)['status'],'success')
        self.assertEqual(self.wait(second.key,{'success','failed'})['status'],'success')
        self.assertEqual(len(self.fixture.posts()),2)

    def test_manual_booking_keeps_original_python_path_and_scheduled_plan(self):
        self.plan(delay=20);self.runtime.start()
        morning=datetime.now(ZONE).replace(hour=9,minute=0,second=0,microsecond=0).timestamp()
        self.runtime.native.dispatch.service.wall_clock=lambda:morning
        plan=self.runtime.store.get_account(self.account.id)['plan']
        today=datetime.now(ZONE).date().isoformat()
        preview=self.runtime.today.preview(self.account.id,{'expected_revision':self.account.revision,'date':today,
            'seat_ids':['2'],'begin':'14:00','end':'16:00'})
        result=self.runtime.today.submit(self.account.id,preview['preview_token'])
        self.assertEqual(result['result']['status'],'success')
        row=self.runtime.store.get_run(result['run_id'])
        self.assertEqual(row['spec']['kind'],'manual');self.assertNotIn('native_binding',row)
        self.assertEqual(self.runtime.store.get_account(self.account.id)['plan'],plan)

    def test_21_accounts_keep_all_plans_and_fall_back_to_bounded_python_queue(self):
        extra='2312000021';self.fixture.register(extra,1021)
        self.runtime.auth.access.insert_one({'_id':extra,'enabled':True})
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password') for student in self.students[1:]+[extra]]
        due=math.ceil(time.time()+6);runs=[self.plan(account,due=due) for account in accounts]
        self.runtime.start()
        rows=[self.wait(run.key,{'success','failed','skipped'},14) for run in runs]
        self.assertTrue(all(row['status']=='success' for row in rows),[(row['status'],row.get('message')) for row in rows])
        self.assertEqual(len(self.fixture.posts()),21)
        self.assertTrue(all(not row.get('native_binding') for row in rows))
        self.assertEqual(self.runtime.scheduler.status()['capacity'],20)

    def test_late_21st_account_never_replays_20_native_owners(self):
        extra='2312000021';self.fixture.register(extra,1021)
        self.runtime.auth.access.insert_one({'_id':extra,'enabled':True})
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password') for student in self.students[1:]]
        due=math.ceil(time.time()+6);runs=[self.plan(account,due=due) for account in accounts]
        self.runtime.start()
        for run in runs:self.wait_arm(run)
        account=self.runtime.accounts.verify_and_save(extra,'fixture-password')
        last=self.plan(account,due=due)
        rows=[self.wait(run.key,{'success','failed','skipped'},14) for run in runs+[last]]
        self.assertTrue(all(row['status']=='success' for row in rows),[(row['status'],row.get('message')) for row in rows])
        self.assertTrue(all(row.get('native_binding') for row in rows[:-1]));self.assertFalse(rows[-1].get('native_binding'))
        self.assertEqual(len(self.fixture.posts()),21)
        self.assertTrue(all(value==1 for value in self.fixture.post_counts.values()))

    def test_slow_payload_read_does_not_delay_peer_first_send(self):
        other=self.runtime.accounts.verify_and_save(self.students[1],'fixture-password')
        due=math.ceil(time.time()+6);first=self.plan(due=due);second=self.plan(other,due=due)
        self.fixture.key_delays[self.students[0]]=2.6
        self.runtime.start()
        row=self.wait(second.key,{'success','failed','skipped'},12)
        self.assertEqual(row['status'],'success')
        self.assertLess(row['attempts'][0]['timings']['request_started_at']-due,.2)
        self.assertEqual(self.wait(first.key,{'success','failed','skipped'},12)['status'],'success')
