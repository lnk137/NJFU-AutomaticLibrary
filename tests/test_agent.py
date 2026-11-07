from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
from concurrent.futures import Future
import threading
import unittest
from local_app.dual_node.agent import RPCClient, RPCError, NodeAgent


def task(ref='fixture-one', due=2000):
    return {'schema':1,'account_ref':ref,'day':'2026-11-07','kind':'S','operation':'-',
            'revision':1,'credential_revision':1,'release_at':due,'start_minute':540,
            'end_minute':720,'seat_priorities':['1']}


class AgentTests(unittest.TestCase):
    def test_rpc_matches_response_and_never_retries_unknown_mutation(self):
        class Transport:
            closed=False; count=0; timeout=5
            def call(self, request):
                self.count+=1
                raise OSError('not forwarded')
            def close(self): self.closed=True
        t=Transport(); rpc=RPCClient(lambda:t)
        with self.assertRaisesRegex(RPCError,'rpc_result_unknown'):rpc.call('authorize',task=task())
        self.assertEqual(t.count,1);self.assertTrue(t.closed)
        class Bad(Transport):
            def call(self,request):return {'schema':1,'ok':True,'request_id':'wrong','result':{}}
        rpc=RPCClient(Bad)
        with self.assertRaisesRegex(RPCError,'rpc_response_invalid'):rpc.call('status',task=task())

    def test_twenty_tasks_share_one_probe(self):
        calls=[]; rows=[task('fixture-'+str(i)) for i in range(20)]
        class RPC:
            def call(self,op,**body):
                if op=='tasks':return {'tasks':rows}
                if op=='status':return {'phase':'collecting','owner':None}
                calls.append((op,body));return {}
            def close(self):pass
        class Executor:
            def close(self):pass
            def abort(self,*args):pass
        sample_calls=[]
        def sample(*args):
            sample_calls.append(args);return {'synthetic':True}
        clock=[1580]
        agent=NodeAgent(RPC(),'mac',Executor(),None,lambda task:{'schema':1},
                        sample=sample,clock=lambda:clock[0],boot='fixture-boot')
        self.addCleanup(agent.close)
        agent.step()
        list(agent.probes.values())[0].result(timeout=2)
        agent.step()
        self.assertEqual(len(sample_calls),1)
        self.assertEqual(len([x for x in calls if x[0]=='report']),20)
        # Refresh capability against cached public evidence, without new GETs.
        clock[0]+=21;agent.step()
        self.assertEqual(len(sample_calls),1)
        self.assertEqual(len([x for x in calls if x[0]=='report']),40)

    def test_loser_does_not_prepare_or_login(self):
        class RPC:
            def call(self,op,**body):
                return {'tasks':[task()]} if op=='tasks' else {'phase':'selected','owner':'ubuntu'}
            def close(self):pass
        class Executor:
            calls=0
            def prepare(self,*_):self.calls+=1
            def abort(self,*args):pass
            def close(self):pass
        e=Executor();a=NodeAgent(RPC(),'mac',e,None,lambda _: {},clock=lambda:1950,boot='fixture')
        self.addCleanup(a.close);a.step();self.assertEqual(e.calls,0)

    def test_authorized_restart_reports_uncertain_and_never_executes(self):
        calls=[]
        class RPC:
            def call(self,op,**body):
                calls.append(op)
                if op=='tasks':return {'tasks':[task()]}
                if op=='status':return {'phase':'authorized','owner':'mac','grant':{'public':'grant'}}
                return {}
            def close(self):pass
        class Executor:
            def status(self,_):return {'execution':{'status':'armed'}}
            def abort(self,*args):pass
            def close(self):pass
            def execute(self,*_):raise AssertionError('must never reexecute')
        class Verifier:
            def verify_grant(self,*args,**kwargs):return {'grant_id':'a'*32}
        a=NodeAgent(RPC(),'mac',Executor(),Verifier(),lambda _: {},clock=lambda:2001,boot='fixture')
        self.addCleanup(a.close);a.step();self.assertIn('result',calls)


if __name__=='__main__':unittest.main()
