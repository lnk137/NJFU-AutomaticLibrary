"""Real client/service/Mongo replay of a busy response followed by rate limiting."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from test_busy_recovery import BUSY, BusyRecoveryTests, RecoveryTransport, record

RATE_LIMIT={'code':1,'message':'请求频繁,请稍后重试'}


class PacedSchool(RecoveryTransport):
    """Isolated dual-floor model; this does not claim the school's actual configuration."""
    def __init__(self,key,*,floor=1.2,response_floor=1,post_ms=111.728,query_ms=42.175,always_busy=False,first_busy=True):
        super().__init__(key)
        self.floor,self.response_floor,self.post_ms,self.query_ms=floor,response_floor,post_ms,query_ms
        self.starts,self.ends,self.query_starts,self.gaps,self.response_gaps=[],[],[],[],[]
        self.always_busy=always_busy
        self.first_busy=first_busy
        self.key_reads=0

    def request(self,method,url,**kwargs):
        if url.endswith('/login/publicKey'):self.key_reads+=1
        if method=='POST':
            now=time.perf_counter()
            self.starts.append(now)
            if self.always_busy or (len(self.starts)==1 and self.first_busy):
                reply=BUSY
            elif len(self.starts)==1:
                reply={'code':0}
            else:
                gap=now-self.starts[-2];self.gaps.append(gap)
                response_gap=now-self.ends[-1];self.response_gaps.append(response_gap)
                reply=RATE_LIMIT if gap+0.00001<self.floor or response_gap+0.00001<self.response_floor else {'code':0}
            time.sleep(self.post_ms/1000)
            self.replies.append(reply)
        elif url.endswith('/reserve/resvInfo'):
            self.query_starts.append(time.perf_counter())
            time.sleep(self.query_ms/1000)
        response=super().request(method,url,**kwargs)
        if method=='POST':self.ends.append(time.perf_counter())
        return response


class RetryPacingTests(BusyRecoveryTests):
    # Only the new cases are selected for this subclass by load_tests below.
    def paced(self,**kwargs):
        transport=PacedSchool(self.key,**kwargs)
        client=self.accounts.client(self.account.id)
        client.http=transport
        self.transport=transport
        client.prepare_submission(self.run,'1')
        return transport

    def test_fast_busy_response_observes_both_floors_without_adding_them(self):
        school=self.paced()
        result=self.service().execute(self.run)
        self.assertEqual(result.status,'success','Immediate backup reproduces the saved rate-limit rejection')
        self.assertEqual(school.posts,['1','2'])
        self.assertGreaterEqual(school.gaps[0],1.2)
        self.assertGreaterEqual(school.response_gaps[0],1)
        self.assertLess(school.gaps[0],1.5,'Use the later deadline rather than adding the two floors')
        self.assertGreaterEqual(school.query_starts[0]-school.starts[0],1.19,
                                'Query after cooldown so a delayed receipt is not missed')

    def test_slow_response_still_requires_a_full_second_after_its_completion(self):
        school=self.paced(post_ms=650,query_ms=10)
        self.assertEqual(self.service().execute(self.run).status,'success')
        self.assertGreaterEqual(school.gaps[0],1.65)
        self.assertGreaterEqual(school.response_gaps[0],1)
        self.assertLess(school.gaps[0],1.95,'Do not add a full 1.2 seconds after the slow response')

    def test_first_request_has_no_new_pacing_delay(self):
        school=self.paced(post_ms=0,query_ms=0,first_busy=False)
        started=time.perf_counter()
        self.service().execute(self.run)
        self.assertLess(school.starts[0]-started,.1)

    def test_rate_limit_above_policy_floors_still_stops_at_two_posts(self):
        school=self.paced(floor=10,post_ms=0,query_ms=0)
        result=self.service().execute(self.run)
        self.assertEqual(result.status,'failed')
        self.assertEqual(school.posts,['1','2'])
        self.assertFalse(self.store.get_run(self.run.key)['attempts'][-1]['retryable'])

    def test_late_receipt_is_read_after_cooldown_without_backup(self):
        school=self.paced(post_ms=0,query_ms=0)
        # School query returns a just-visible receipt only once 400ms elapsed.
        original=school.request
        def request(method,url,**kwargs):
            if url.endswith('/reserve/resvInfo') and time.perf_counter()-school.starts[0]>.4:
                school.query_replies.append({'code':0,'data':[record()]})
            return original(method,url,**kwargs)
        school.request=request
        result=self.service().execute(self.run)
        self.assertEqual(result.status,'success')
        self.assertEqual(school.posts,['1'])

    def test_spacing_diagnostics_saved_without_changing_http_duration(self):
        school=self.paced(post_ms=0,query_ms=0)
        self.service().execute(self.run)
        row=self.store.get_run(self.run.key)
        check=row['attempts'][0]['retry_check']
        self.assertGreaterEqual(check['wait_ms'],1100)
        self.assertEqual(check['spacing_ms'],1200)
        self.assertEqual(check['response_cooldown_ms'],1000)
        self.assertEqual(check['pacing_policy'],'dual_interval_v1')
        self.assertLess(row['attempts'][1]['timings']['request_ms'],50)

    def test_retry_deadline_uses_the_later_of_start_and_response_constraints(self):
        from local_app.models import Submission
        service=self.service();service.monotonic=lambda:100
        for duration,wait in [(0,1.2),(111.728,1.088272),(200,1),(650,1),(3242.781,1)]:
            for attempt in (1,2,5):
                with self.subTest(request_ms=duration,attempt=attempt):
                    deadline,check=service._busy_retry_window(Submission('rejected',timings={'request_ms':duration}),attempt)
                    self.assertAlmostEqual(deadline,100+wait)
                    self.assertEqual(check['spacing_ms'],1200)
                    self.assertEqual(check['response_cooldown_ms'],1000)

    def test_missing_or_invalid_duration_preserves_the_full_start_interval(self):
        from local_app.models import Submission
        service=self.service();service.monotonic=lambda:100
        for duration in (None,-1,float('nan'),float('inf'),True,'112'):
            with self.subTest(request_ms=duration):
                deadline,_=service._busy_retry_window(Submission('rejected',timings={'request_ms':duration}),1)
                self.assertAlmostEqual(deadline,101.2)

    def stop_while_waiting(self,change,run=None):
        school=self.paced(post_ms=0,query_ms=0)
        service=self.service()
        service.wait=lambda seconds:change(service)
        result=service.execute(run or self.run)
        self.assertEqual(result.status,'skipped')
        self.assertEqual(school.posts,['1'])
        self.assertEqual(school.query_starts,[])
        self.assertEqual(self.store.get_run((run or self.run).key)['attempts'][0]['retry_check']['state'],'stopped')

    def test_gate_closure_during_wait_stops_before_query_and_backup(self):
        self.stop_while_waiting(lambda s:setattr(s,'allow_live',False))

    def test_clock_pause_during_wait_stops_before_backup(self):
        self.stop_while_waiting(lambda s:setattr(s,'guard',lambda:False))

    def test_revocation_during_wait_stops_before_backup(self):
        self.stop_while_waiting(lambda s:setattr(self.accounts,'is_allowed',lambda student:False))

    def test_revision_change_during_wait_stops_before_backup(self):
        self.stop_while_waiting(lambda s:self.store.accounts.update_one(
            {'_id':self.account.id},{'$inc':{'revision':1}}))

    def test_deadline_during_wait_stops_without_extending_window(self):
        self.stop_while_waiting(lambda s:setattr(self,'now',self.run.due_at+31))

    def test_manual_preview_expiry_during_wait_stops_backup(self):
        from datetime import datetime
        from local_app.models import ZONE
        self.now=datetime(2026,10,23,8,tzinfo=ZONE).timestamp()
        run=replace(self.run,due_at=self.now,kind='manual',operation_id='fixture',expires_at=self.now+.2)
        self.stop_while_waiting(lambda s:setattr(self,'now',self.now+1),run)

    def test_repeated_busy_keeps_both_floors_and_original_six_attempt_limit(self):
        school=self.paced(always_busy=True,post_ms=0,query_ms=0)
        run=replace(self.run,seat_ids=('1','2','3','4','5'))
        service=self.service()
        self.assertEqual(service.execute(run).status,'failed')
        self.assertEqual(school.posts,['1','2','3','4','5','1'])
        gaps=[b-a for a,b in zip(school.starts,school.starts[1:])]
        for gap,response_gap in zip(gaps,school.response_gaps):
            self.assertGreaterEqual(gap,1.2)
            self.assertGreaterEqual(response_gap,1)
            self.assertLess(gap,1.5)
        service.execute(run)
        self.assertEqual(len(school.posts),6,'A historical failure must not replay')
        self.assertNotIn('retry_check',self.store.get_run(run.key)['attempts'][-1],
                         'Exhausted attempts must not be recorded as waiting for another retry')

    def test_cooldown_expired_payload_gets_fresh_key_before_backup(self):
        from local_app.elapsed_clock import elapsed_time
        offset=[0]
        with patch('local_app.school.elapsed_time',side_effect=lambda:elapsed_time()+offset[0]):
            school=self.paced(post_ms=0,query_ms=0)
            service=self.service(); original=service.wait
            def wait(seconds):
                offset[0]=3
                original(seconds)
            service.wait=wait
            self.assertEqual(service.execute(self.run).status,'success')
        attempts=self.store.get_run(self.run.key)['attempts']
        self.assertTrue(attempts[0]['timings']['prepared'])
        self.assertFalse(attempts[1]['timings']['prepared'])
        self.assertEqual(school.key_reads,2)

    def test_one_accounts_cooldown_does_not_delay_another_accounts_first_post(self):
        from local_app.models import Identity
        from local_app.school import CampusClient
        self.paced(post_ms=0,query_ms=0)
        other=PacedSchool(self.key,post_ms=0,query_ms=0,first_busy=False)
        class OtherCampus(CampusClient):
            def __init__(self,student,password,**kwargs):
                super().__init__(student,password,transport=other,**kwargs)
            def login(self):
                self.identity=Identity(self.student_id,'Fixture',456)
                return self.identity
        self.accounts.client_factory=OtherCampus
        account=self.accounts.verify_and_save('fixture02','pass','Fixture',None)
        account=self.accounts.save_plan(account.id,self.store.get_account(self.account.id)['plan'],account.revision)
        other_run=replace(self.run,account_id=account.id,revision=account.revision)
        other.query_replies.append({'code':0,'data':[record(logonName='fixture02')]})
        self.accounts.client(account.id).prepare_submission(other_run,'1')
        service=self.service(); waiting=threading.Event(); original=service.wait
        def wait(seconds):
            waiting.set();original(seconds)
        service.wait=wait
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(service.execute,self.run)
            self.assertTrue(waiting.wait(2))
            started=time.perf_counter()
            result=service.execute(other_run)
            self.assertEqual(result.status,'success')
            self.assertLess(other.starts[0]-started,.15)
            self.assertFalse(pending.done(),'The other account must submit during the first cooldown')
            self.assertEqual(pending.result().status,'success')


def load_tests(loader,standard,pattern):
    names=[n for n in RetryPacingTests.__dict__ if n.startswith('test_')]
    return unittest.TestSuite(RetryPacingTests(n) for n in names)


if __name__=='__main__':unittest.main()
