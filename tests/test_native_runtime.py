"""Existing Runtime composition, no school/production network or data."""
from datetime import datetime
import math
import time
import threading
from support.native_runtime import NativeRuntimeCase
from local_app.inbox import InboxEngine,_digest


class NativeRuntimeTests(NativeRuntimeCase):
    def test_actual_runtime_authenticated_plan_uses_native_first_and_matching_record(self):
        run=self.plan();self.runtime.start()
        row=self.wait(run.key,{'success','failed','skipped'})
        self.assertEqual(row['status'],'success',row)
        self.assertEqual(row['execution_source'],'native_campus_first')
        self.assertEqual(row['attempts'][0]['timings']['timing_version'],3)
        self.assertEqual(len(self.fixture.posts()),1)
        status=self.runtime.scheduler.status()
        self.assertEqual(status['executor'],'cpp');self.assertEqual(status['capacity'],20)
        self.assertTrue(status['ready_to_submit'])

    def test_release_prewarms_resident_native_worker_instead_of_relying_on_startup(self):
        native=self.runtime.native.process
        self.assertEqual(self.fixture.warm_count,0)
        native.workers[0].warm(1000)
        before=self.fixture.warm_count
        run=self.plan();self.runtime.start()
        row=self.wait(run.key,{'success','failed','skipped'})
        self.assertEqual(row['status'],'success',row)
        self.assertGreater(self.fixture.warm_count,before)
        self.assertGreater(row['native_schedule']['transport_warmed_ns'],0)
        self.assertLessEqual(row['native_schedule']['due_ns']-row['native_schedule']['transport_warmed_ns'],5_000_000_000)

    def test_20_accounts_keep_independent_warmed_first_sends(self):
        accounts=[self.account]+[self.runtime.accounts.verify_and_save(student,'fixture-password') for student in self.students[1:]]
        due=math.ceil(time.time()+7);runs=[self.plan(account,due=due) for account in accounts]
        self.runtime.start();rows=[self.wait(run.key,{'success','failed','skipped'},14) for run in runs]
        self.assertTrue(all(row['status']=='success' for row in rows),[(row['status'],row.get('message')) for row in rows])
        self.assertEqual(len(self.fixture.posts()),20)
        self.assertTrue(all(row.get('native_binding') for row in rows))

    def test_unrelated_new_student_permission_does_not_fence_armed_accounts_globally(self):
        run=self.plan();self.runtime.start();self.wait_arm(run)
        self.runtime.auth.access.insert_one({'_id':'2312000099','enabled':True,'revision':1})
        row=self.wait(run.key,{'success','failed','skipped'})
        self.assertEqual(row['status'],'success',row)
        self.assertEqual(len(self.fixture.posts()),1)

    def test_disabling_own_plan_after_arm_stops_only_unsent_request(self):
        run=self.plan();self.runtime.start();self.wait_arm(run)
        current=self.runtime.store.get_account(run.account_id)
        self.runtime.accounts.save_plan(run.account_id,current['plan']|{'enabled':False},current['revision'])
        self.assertEqual(self.wait(run.key,{'skipped','failed'})['status'],'skipped')
        self.assertEqual(self.fixture.posts(),[])
        self.assertFalse(self.runtime.store.get_account(run.account_id)['plan']['enabled'])

    def test_busy_response_queries_before_switching_backup_with_same_runtime(self):
        run=self.plan();self.fixture.faults[(self.students[0],1)]={'busy':True}
        self.runtime.start();row=self.wait(run.key,{'success','failed','skipped'})
        self.assertEqual(row['status'],'success',row)
        self.assertEqual([row['seat_id'] for row in self.fixture.posts()],['1','26'])
        self.assertEqual(len(row['attempts']),2)
        events=self.fixture.events;first=events.index(next(row for row in events if row['kind']=='post'))
        self.assertEqual(events[first+1]['kind'],'query')

    def test_inbox_commit_uses_same_authority_without_extra_booking(self):
        self.plan(delay=300)
        engine=InboxEngine(self.runtime.accounts,self.runtime.store,self.runtime.auth)
        row=self.runtime.store.get_account(self.account.id)
        access=self.runtime.auth.access.find_one({'_id':self.students[0]})
        verifier=engine.verifier(row,access)
        job={'request_id':'isolated-inbox-1','student_id':self.students[0],'version':1,
            'generation':verifier['generation'],'expected_revision':row['revision'],
            'plan':row['plan']|{'seat_ids':['2'],'trigger':'07:00:00'},'created_at':time.time(),
            'expires_at':time.time()+600,'attempt':1}
        result=engine.apply(job)
        self.assertEqual(result['state'],'applied',result)
        current=self.runtime.store.get_account(self.account.id)
        self.assertEqual(current['plan']['seat_ids'],['2'])
        self.assertEqual(current['last_inbox']['digest'],_digest(job))
        applied=self.runtime.native.bridge.outbox.applied(self.account.id)
        self.assertEqual(applied['credential'],current['credential_revision'])
        self.assertEqual(self.fixture.posts(),[])

    def test_runtime_close_fences_armed_request_before_draining(self):
        run=self.plan();self.runtime.start();self.wait_arm(run)
        self.runtime.close()
        row=self.db.campus_runs.find_one({'_id':run.key})
        self.assertEqual(row['status'],'skipped',row)
        self.assertEqual(self.fixture.posts(),[])

    def test_shutdown_fences_before_waiting_for_an_in_progress_form_worker(self):
        run=self.plan();self.runtime.start();self.wait_arm(run)
        entered=threading.Event();release=threading.Event()
        def processing():entered.set();release.wait(3)
        future=self.runtime.forms.pool.submit(processing)
        self.assertTrue(entered.wait(1))
        timer=threading.Timer(2,release.set);timer.start()
        try:self.runtime.close()
        finally:release.set();timer.join()
        self.assertTrue(future.done())
        self.assertEqual(self.fixture.posts(),[])
