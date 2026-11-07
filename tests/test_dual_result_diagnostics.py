"""Fixed codes and numeric timing only; no school response text crosses RPC."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'AL-BackEnd'), str(ROOT/'tests')]
from local_app.dual_node.agent import NodeAgent
from local_app.dual_node.executor import HeadlessExecutor
from local_app.models import Submission
import test_coordinator_ledger as fixtures
import test_coordinator_rpc as rpc_fixtures
import test_result_bridge as bridge_fixtures
from test_coordinator_contracts import task


def diagnostic():
    return {'stage': 'submit', 'reason': 'rate_limited', 'prepare_ms': 35.5,
        'attempts': [{'seat_index': 0, 'outcome': 'rejected', 'reason': 'rate_limited',
                      'sent': True, 'request_ms': 101.5, 'request_started_at': task()['release_at']+.01}]}


class ResultDiagnosticsTests(unittest.TestCase):
    def test_executor_classifies_fixed_rejection_without_leaking_response_text(self):
        executor = HeadlessExecutor.__new__(HeadlessExecutor)
        executor.wall_clock = lambda: task()['release_at']+2
        run = Mock(); run.seat_ids = ('3422', '3423')
        for message, expected in [('请求频繁,请稍后重试','rate_limited'),
                ('private-token-sentinel unknown rejection','school_rejected')]:
            context = {'task':task(), 'run':run, 'client':Mock()}
            response = Submission('rejected', False, 1, message, True,
                {'request_ms':101.5, 'request_started_at':task()['release_at']+.01,
                 'untrusted':'private-token-sentinel'})
            executor._continue(context, response)
            self.assertEqual(context['result_diagnostics']['reason'], expected)
            self.assertEqual(context['result_diagnostics']['attempts'][0]['request_ms'], 101.5)
            self.assertNotIn('private-token-sentinel', str(context['result_diagnostics']))

    def test_agent_transmits_only_valid_bounded_diagnostics(self):
        agent = NodeAgent.__new__(NodeAgent); agent.node_id='mac'; agent.lock=threading.RLock()
        agent.outcomes={}; agent.rpc=Mock(); agent.verifier=Mock()
        agent.verifier.verify_grant.return_value={'grant_id':'a'*32}
        value = diagnostic()
        agent._record_outcome(task(), ('task','binding'), {},
            {'status':'failed','attempt_count':1,'diagnostics':value})
        self.assertEqual(agent.rpc.call.call_args.kwargs['diagnostics'], value)
        for invalid in (value|{'raw':'private-token-sentinel'}, value|{'reason':'private-token-sentinel'},
                value|{'attempts':value['attempts']*7}):
            agent._record_outcome(task(), ('task','binding'), {},
                {'status':'failed','attempt_count':1,'diagnostics':invalid})
            self.assertNotIn('diagnostics', agent.rpc.call.call_args.kwargs)

    def test_oversized_or_nonfinite_diagnostics_cannot_block_the_result(self):
        from local_app.dual_node.contracts import result_diagnostics
        for number in (10**400, float('inf'), float('nan')):
            value=diagnostic();value['attempts'][0]['request_ms']=number
            with self.assertRaises(ValueError):result_diagnostics(value,task())
            agent=NodeAgent.__new__(NodeAgent);agent.node_id='mac';agent.lock=threading.RLock()
            agent.outcomes={};agent.rpc=Mock();agent.verifier=Mock()
            agent.verifier.verify_grant.return_value={'grant_id':'a'*32}
            agent._record_outcome(task(),('task','binding'),{},
                {'status':'failed','attempt_count':1,'diagnostics':value})
            self.assertEqual(agent.rpc.call.call_args.kwargs['status'],'failed')
            self.assertNotIn('diagnostics',agent.rpc.call.call_args.kwargs)
        for field in ('raw','password','token'):
            with self.assertRaises(ValueError):result_diagnostics(diagnostic()|{field:'private-sentinel'},task())

    def test_executor_omits_oversized_observations_without_changing_rejection(self):
        executor=HeadlessExecutor.__new__(HeadlessExecutor)
        executor.wall_clock=lambda:task()['release_at']+2
        run=Mock();run.seat_ids=('3422','3423')
        context={'task':task(),'run':run,'client':Mock()}
        response=Submission('rejected',False,1,'请求频繁,请稍后重试',True,
            {'request_ms':10**400,'request_started_at':10**400})
        outcome=executor._continue(context,response)
        self.assertEqual(outcome['status'],'failed')
        observed=context['result_diagnostics']['attempts'][0]
        self.assertNotIn('request_ms',observed)
        self.assertNotIn('request_started_at',observed)

    def test_rpc_persists_diagnostics_and_rejects_unrecognized_text(self):
        helper=fixtures.LedgerTests(); rpc=rpc_fixtures.RPCTests()
        with tempfile.TemporaryDirectory(prefix='njfu-diagnostic-fixture-') as directory:
            clock=[fixtures.NOW]
            with helper.initial(Path(directory).resolve()/'authority', clock) as authority:
                helper.reports(authority);authority.select(task());helper.ready(authority,'mac',clock)
                grant=authority.authorize('mac',task())['grant']['payload']['grant_id']
                body={'task':task(),'grant_id':grant,'status':'failed','attempt_count':1,'diagnostics':diagnostic()}
                bad=copy.deepcopy(body);bad['diagnostics']['reason']='private-token-sentinel'
                reply=rpc.send(authority,'result',bad)
                self.assertFalse(reply['ok']);self.assertNotIn('private-token-sentinel',str(reply))
                result=rpc.send(authority,'result',body)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['result']['result']['diagnostics'], diagnostic())


class DiagnosticsMirrorTests(unittest.TestCase):
    setUpClass = classmethod(bridge_fixtures.BridgeTests.setUpClass.__func__)
    tearDownClass = classmethod(bridge_fixtures.BridgeTests.tearDownClass.__func__)
    setUp = bridge_fixtures.BridgeTests.setUp
    bridge = bridge_fixtures.BridgeTests.bridge
    def test_diagnostics_become_existing_dashboard_attempts_without_raw_messages(self):
        response=bridge_fixtures.status(task(),'failed'); response['result']['diagnostics']=diagnostic()
        self.bridge().apply(task(),response)
        row=self.store.runs.find_one({'dual_binding':{'$exists':True}})
        self.assertIn('请求频繁', row['message'])
        self.assertEqual(row['attempts'][0]['timings']['request_ms'],101.5)
        self.assertEqual(row['attempts'][0]['seat_id'],task()['seat_priorities'][0])
        self.assertEqual(row['scheduler_timings']['prepare']['elapsed_ms'],35.5)


if __name__=='__main__':unittest.main()
