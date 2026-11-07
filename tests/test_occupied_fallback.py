"""Exact Oct26 rejection replay; real services/Mongo/crypto, fake school only."""
from dataclasses import replace
from datetime import datetime
import json
import math
import time
import unittest

from local_app.models import ZONE,resolve_run,validate_plan
from local_app.native.request import decode_result
from test_busy_recovery import BusyRecoveryTests,BUSY,record
from support.native_runtime import NativeRuntimeCase

OCCUPIED={'code':1,'message':'设备在该时间段内已被预约'}
RATE_LIMIT={'code':1,'message':'请求频繁,请稍后重试'}


class PythonOccupiedFallbackTests(BusyRecoveryTests):
    def test_occupied_after_busy_continues_to_remaining_fourth_candidate(self):
        run=replace(self.run,seat_ids=('1','2','3','4'))
        self.transport.replies.extend([BUSY,OCCUPIED,OCCUPIED,{'code':0}])
        original=self.store.get_account(self.account.id)['plan']
        result=self.service().execute(run)
        self.assertEqual(result.status,'success')
        self.assertEqual(self.transport.posts,['1','2','3','4'])
        self.assertEqual(self.transport.events,[('POST','1'),('QUERY',1),('POST','2'),('QUERY',1),
            ('POST','3'),('QUERY',1),('POST','4'),('QUERY',1)])
        self.assertEqual(result.records[0].seat_id,'4')
        self.assertEqual(self.store.get_account(self.account.id)['plan'],original)

    def test_occupied_candidates_never_consume_the_later_busy_retry_budget(self):
        run=replace(self.run,seat_ids=('1','2','3','4'))
        self.transport.replies.extend([OCCUPIED,OCCUPIED,BUSY,BUSY,BUSY,{'code':0}])
        result=self.service().execute(run)
        self.assertEqual(result.status,'success')
        self.assertEqual(self.transport.posts,['1','2','3','4','3','4'])
        self.service().execute(run)
        self.assertEqual(len(self.transport.posts),6)

    def test_occupied_all_candidates_finish_only_after_each_distinct_seat(self):
        run=replace(self.run,seat_ids=('1','2','3','4'))
        self.transport.replies.extend([OCCUPIED]*8)
        service=self.service();self.assertEqual(service.execute(run).status,'failed')
        self.assertEqual(self.transport.posts,['1','2','3','4'])
        service.execute(run);self.assertEqual(len(self.transport.posts),4)

    def test_occupied_matching_receipt_stops_without_another_submission(self):
        self.transport.replies.append(OCCUPIED)
        self.transport.query_replies.append({'code':0,'data':[record()]})
        self.assertEqual(self.service().execute(self.run).status,'success')
        self.assertEqual(self.transport.posts,['1'])

    def test_occupied_query_error_keeps_uncertainty_without_a_backup_post(self):
        import requests
        self.transport.replies.append(OCCUPIED)
        self.transport.query_replies.append(requests.Timeout())
        self.assertEqual(self.service().execute(self.run).status,'uncertain')
        self.assertEqual(self.transport.posts,['1'])

    def test_occupied_then_rate_limit_still_stops_all_later_submissions(self):
        self.transport.replies.extend([OCCUPIED,RATE_LIMIT,{'code':0}])
        self.assertEqual(self.service().execute(replace(self.run,seat_ids=('1','2','3'))).status,'failed')
        self.assertEqual(self.transport.posts,['1','2'])


class NativeOccupiedFallbackTests(NativeRuntimeCase):
    def four_seat_plan(self):
        due=math.ceil(time.time()+6)
        saved=self.runtime.accounts.save_plan(self.account.id,{'seat_ids':['1','2','3','4'],
            'enabled':True,'day_offset':1,'mode':'day','begin':'09:00','end':'12:00',
            'trigger':datetime.fromtimestamp(due,ZONE).strftime('%H:%M:%S')},self.account.revision)
        return resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.catalog),datetime.fromtimestamp(due,ZONE).date())

    def test_native_busy_then_occupied_reaches_fourth_seat_with_readbacks(self):
        run=self.four_seat_plan()
        self.fixture.faults[(self.students[0],1)]={'busy':True}
        for n in (2,3):self.fixture.faults[(self.students[0],n)]={'reject':True,**OCCUPIED}
        self.runtime.start();row=self.wait(run.key,{'success','failed','skipped'},16)
        self.assertEqual(row['status'],'success',row.get('message'))
        self.assertEqual([p['seat_id'] for p in self.fixture.posts()],['1','2','3','4'])
        self.assertEqual(row['attempts'][0]['timings']['timing_version'],3)
        self.assertTrue(all(a.get('retry_check',{}).get('state')=='empty' for a in row['attempts'][:-1]))
        self.assertEqual(row['native_continuation']['state'],'done')

    def test_native_occupied_first_uses_backup_and_matching_receipt(self):
        run=self.four_seat_plan()
        self.fixture.faults[(self.students[0],1)]={'reject':True,**OCCUPIED}
        self.runtime.start();row=self.wait(run.key,{'success','failed','skipped'},14)
        self.assertEqual(row['status'],'success',row.get('message'))
        self.assertEqual([p['seat_id'] for p in self.fixture.posts()],['1','2'])
        self.assertEqual(row['attempts'][0]['retry_check']['state'],'empty')

    def test_native_all_occupied_stops_after_four_distinct_seats(self):
        run=self.four_seat_plan()
        for n in range(1,9):self.fixture.faults[(self.students[0],n)]={'reject':True,**OCCUPIED}
        self.runtime.start();row=self.wait(run.key,{'success','failed','skipped'},16)
        self.assertEqual(row['status'],'failed')
        self.assertEqual([p['seat_id'] for p in self.fixture.posts()],['1','2','3','4'])
        self.assertEqual(len(row['attempts']),4)


class NativeRejectionClassificationTests(unittest.TestCase):
    def test_observed_occupied_reply_is_retryable_but_global_errors_are_not(self):
        for reply,want in [(OCCUPIED,True),(BUSY,True),(RATE_LIMIT,False),
                ({'code':1,'message':'您已有预约'},False),({'code':1,'message':'系统繁忙'},False),
                ({'code':2,'message':OCCUPIED['message']},False),
                ({'code':1,'message':OCCUPIED['message']+'，需要人工核对'},False)]:
            with self.subTest(reply=reply):
                result=decode_result(200,json.dumps(reply).encode(),{'timing_version':3})
                self.assertEqual(result.retryable,want)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in PythonOccupiedFallbackTests.__dict__:
        if name.startswith('test_'):suite.addTest(PythonOccupiedFallbackTests(name))
    suite.addTests(loader.loadTestsFromTestCase(NativeOccupiedFallbackTests))
    suite.addTests(loader.loadTestsFromTestCase(NativeRejectionClassificationTests))
    return suite
