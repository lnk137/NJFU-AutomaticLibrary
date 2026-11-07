"""Deterministic worker recovery without sockets, Mongo, or application runtime."""
from concurrent.futures import Future
from datetime import datetime
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
from local_app import inbox
from local_app.models import AppError


class Clock:
    def __init__(self, hour=14, minute=0, second=0):
        self.start = self.now = datetime(2026, 11, 7, hour, minute, second, tzinfo=inbox.ZONE).timestamp()
    def time(self): return self.now


class Engine:
    triggers = ('07:00:00',)
    def __init__(self): self.sequence = 0
    def snapshot(self, *_args, **_kwargs):
        self.sequence += 1
        return {'generation':self.sequence, 'accounts':[]}


class Pool:
    def __init__(self, behavior='success'): self.behavior = behavior
    def submit(self, fn, *args, **kwargs):
        future = Future()
        if self.behavior == 'pending': return future
        if self.behavior == 'error': future.set_exception(RuntimeError('fixture-secret-export'))
        else:
            try: future.set_result(fn(*args, **kwargs))
            except Exception as error: future.set_exception(error)
        return future


class Stop:
    def __init__(self, clock, duration): self.clock, self.end = clock, clock.now+duration
    def is_set(self): return self.clock.now >= self.end
    def wait(self, seconds): self.clock.now += seconds


class InlineThread:
    def __init__(self, *, target, **_kwargs): self.target = target
    def start(self): self.target()


def fixture(*, duration=30, failure=None, recovery_at=None, lost_response=False,
            failure_seconds=0, export='success', clock=None, gate=None, request_override=None,
            setup=None, ack_error=None):
    clock = clock or Clock()
    worker = inbox.InboxWorker.__new__(inbox.InboxWorker)
    worker.engine = Engine()
    worker.scheduler = SimpleNamespace(status=lambda:{'clock_ok':gate(clock.time()-clock.start) if gate else True,'next_due':None})
    worker.stop_event, worker.sync_requested = Stop(clock,duration), threading.Event()
    worker.snapshot_pool, worker.ack_pool, worker.pool = Pool(export), Pool(), Pool()
    worker.acks, worker.ack_retry, worker.jobs = {}, {}, {}
    worker.snapshot_future = None
    worker.next_publish = worker.next_poll = 0
    worker._lock = threading.Lock()
    worker._status = {'configured':True,'connected':False,'accounts':[]}
    calls, polls, publications = [], [], []
    cloud = {'generation':0,'heartbeat':None}
    def request(path, data, *, budget):
        relative = clock.time()-clock.start
        calls.append({'at':relative,'path':path,'budget':budget,'generation':data.get('generation')})
        failing = failure is not None and (recovery_at is None or relative < recovery_at)
        if path == 'snapshot':
            if not failing or lost_response:
                if data['generation'] > cloud['generation']:
                    cloud.update(generation=data['generation'],heartbeat=relative)
                    publications.append(relative)
            if failing:
                clock.now += failure_seconds
                raise failure
            return {'ok':True}
        if path == 'poll':
            polls.append(relative); cloud['heartbeat'] = relative
            return {'jobs':[],'summary':{'accounts':[],'pending':0,'failed':0,'heartbeat_at':relative,'quiet_until':None}}
        if path == 'ack':
            if ack_error: raise ack_error
            return {'ok':True}
        raise AssertionError('unexpected_fixture_path')
    worker.request = request_override or request
    if setup: setup(worker)
    with patch.object(inbox,'time',clock), patch.object(inbox.threading,'Thread',InlineThread): worker.start()
    return SimpleNamespace(worker=worker,clock=clock,calls=calls,polls=polls,publications=publications,cloud=cloud)


class InboxWorkerRecoveryTests(unittest.TestCase):
    def test_snapshot_failure_keeps_polling_and_retries_same_generation(self):
        result = fixture(failure=requests.ReadTimeout('fixture-secret-token'))
        self.assertGreaterEqual(len(result.polls),10)
        self.assertEqual(result.worker.engine.sequence,1)
        attempts = [call for call in result.calls if call['path']=='snapshot']
        self.assertEqual({call['generation'] for call in attempts},{1})
        self.assertTrue(all(b['at']-a['at']>=3 for a,b in zip(attempts,attempts[1:])))
        self.assertIsNotNone(result.worker._status.get('export_error'))
        self.assertIsNone(result.worker._status.get('last_sync_at'))
        with patch.object(inbox,'time',result.clock): self.assertTrue(result.worker.status()['connected'])

    def test_lost_response_after_commit_does_not_freeze_heartbeat(self):
        result = fixture(failure=requests.ConnectionError('fixture-secret'),lost_response=True)
        self.assertEqual(result.cloud['generation'],result.worker.engine.sequence)
        self.assertGreaterEqual(result.cloud['heartbeat'],25)
        self.assertGreaterEqual(len(result.polls),10)

    def test_recovered_snapshot_clears_export_error_without_restart(self):
        result = fixture(duration=50,failure=requests.ReadTimeout('fixture-secret'),recovery_at=20,lost_response=True)
        self.assertGreaterEqual(len(result.publications),3)
        self.assertIsNone(result.worker._status.get('export_error'))
        self.assertGreater(result.worker.engine.sequence,1)

    def test_failed_snapshot_does_not_expand_cycle_budget(self):
        result = fixture(failure=requests.ReadTimeout('fixture-secret'),failure_seconds=3.6)
        self.assertGreater(len(result.polls),1)
        self.assertTrue(all(.5 <= call['budget'] <= 3 for call in result.calls))
        for index,call in enumerate(result.calls):
            if call['path']=='snapshot' and index+1<len(result.calls):
                self.assertGreaterEqual(result.calls[index+1]['at']-call['at'],4.1-1e-6)

    def test_quiet_window_still_forbids_network_after_failure(self):
        clock = Clock(6,59,44)
        result = fixture(duration=70,clock=clock,failure=requests.ReadTimeout('fixture-secret'))
        self.assertTrue(all(not inbox.io_quiet(clock.start+call['at'],('07:00:00',)) for call in result.calls))
        self.assertTrue(any(call['at']>57 for call in result.calls))

    def test_clock_gate_remains_closed_until_healthy(self):
        result = fixture(gate=lambda elapsed:elapsed>=10,failure=requests.ReadTimeout('fixture-secret'))
        self.assertTrue(result.calls)
        self.assertTrue(all(call['at']>=10 for call in result.calls))

    def test_pending_export_still_polls(self):
        self.assertGreaterEqual(len(fixture(export='pending').polls),14)

    def test_failed_export_has_fixed_diagnostic_and_keeps_polling(self):
        result = fixture(export='error')
        self.assertGreaterEqual(len(result.polls),10)
        self.assertEqual(result.worker._status.get('last_failure',{}).get('stage'),'export')
        self.assertNotIn('fixture-secret',json.dumps(result.worker._status))

    def test_snapshot_failure_diagnostic_is_fixed_and_has_no_exception_text(self):
        for error, code in ((requests.ReadTimeout('fixture-secret'),'timeout'),
                            (requests.exceptions.SSLError('fixture-secret'),'tls_error'),
                            (requests.ConnectionError('fixture-secret'),'connection_error'),
                            (requests.exceptions.JSONDecodeError('fixture-secret','',0),'response_invalid'),
                            (RuntimeError('fixture-secret'),'internal_error')):
            with self.subTest(code=code):
                result = fixture(duration=5,failure=error)
                diagnostic = result.worker._status.get('last_failure',{})
                self.assertEqual(diagnostic.get('stage'),'snapshot')
                self.assertEqual(diagnostic.get('code'),code)
                self.assertEqual(set(diagnostic),{'stage','code','at'})
                self.assertNotIn('fixture-secret',json.dumps(result.worker._status))

    def test_http_failure_keeps_only_numeric_status(self):
        response = SimpleNamespace(status_code=413)
        session = SimpleNamespace(post=lambda *args,**kwargs:response,close=lambda:None)
        worker = inbox.InboxWorker.__new__(inbox.InboxWorker)
        worker.config = {'origin':'https://fixture.invalid','token':'fixture-secret-token'}
        with patch.object(inbox.requests,'Session',return_value=session):
            with self.assertRaises(AppError) as caught: worker.request('snapshot',{})
        result = fixture(duration=5,failure=caught.exception)
        diagnostic = result.worker._status.get('last_failure',{})
        self.assertEqual(diagnostic.get('stage'),'snapshot')
        self.assertEqual(diagnostic.get('code'),'http_status')
        self.assertEqual(diagnostic.get('http_status'),413)
        self.assertNotIn('fixture-secret',json.dumps(result.worker._status))

    def test_poll_failure_has_poll_stage(self):
        def unavailable(*args,**kwargs): raise requests.ReadTimeout('fixture-secret')
        result = fixture(duration=5,request_override=unavailable)
        self.assertEqual(result.worker._status['last_failure']['stage'],'poll')
        self.assertEqual(result.worker._status['last_failure']['code'],'timeout')
        self.assertFalse(result.worker._status['connected'])
        self.assertNotIn('fixture-secret',json.dumps(result.worker._status))

    def test_failed_ack_keeps_completed_job_and_independent_polling(self):
        completed = Future(); completed.set_result({'state':'applied','revision':2})
        def setup(worker): worker.jobs['fixture-id']=({'lease':'fixture-lease'},completed)
        result = fixture(setup=setup,ack_error=requests.ReadTimeout('fixture-secret'))
        self.assertIs(result.worker.jobs['fixture-id'][1],completed)
        self.assertGreaterEqual(len(result.polls),10)
        self.assertEqual(result.worker._status['last_failure']['stage'],'ack')
        self.assertEqual(result.worker._status['last_failure']['code'],'timeout')
        self.assertNotIn('fixture-secret',json.dumps(result.worker._status))

    def test_sync_window_is_control_flow_not_failure(self):
        result = fixture(duration=10,clock=Clock(6,59,44))
        self.assertNotIn('last_failure',result.worker._status)
        self.assertTrue(all(call['at']<1 for call in result.calls))

    def test_failed_snapshot_crossing_quiet_boundary_does_not_start_poll(self):
        def ready(worker):
            worker.snapshot_future=Future()
            worker.snapshot_future.set_result(worker.engine.snapshot({}))
        result = fixture(duration=2,clock=Clock(6,59,44),setup=ready,
                         failure=requests.ReadTimeout('fixture-secret'),failure_seconds=1.1)
        self.assertEqual([call['path'] for call in result.calls],['snapshot'])
        self.assertEqual(result.worker._status['last_failure']['stage'],'snapshot')

    def test_untrusted_exception_attributes_are_not_reported(self):
        for status in ('fixture-secret',True,99,600,10**400):
            with self.subTest(status_type=type(status).__name__):
                error = AppError('inbox_connection','fixture-secret')
                error.inbox_http_status = status
                result = fixture(duration=5,failure=error)
                self.assertEqual(set(result.worker._status['last_failure']),{'stage','code','at'})
                self.assertNotIn('fixture-secret',json.dumps(result.worker._status))
        result = fixture(duration=5,failure=AppError('fixture-secret','fixture-secret'))
        self.assertEqual(result.worker._status['last_failure']['code'],'application_error')
        self.assertNotIn('fixture-secret',json.dumps(result.worker._status))


if __name__=='__main__':unittest.main()
