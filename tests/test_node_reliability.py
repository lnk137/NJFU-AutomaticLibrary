"""Regression fixtures never create real transports or school connections."""
from concurrent.futures import Future
from pathlib import Path
import json
import socket
import tempfile
import unittest
from unittest.mock import patch
from local_app.dual_node import public_probe
from local_app.dual_node.agent import NodeAgent, RPCError
from local_app.dual_node.coordinator import _report


def task(ref='fixture-one'):
    return {'schema':1,'account_ref':ref,'day':'2026-11-08','kind':'S','operation':'-',
            'revision':1,'credential_revision':1,'release_at':1794006000.,
            'start_minute':480,'end_minute':1320,'seat_priorities':['1']}


class NoNetworkCase(unittest.TestCase):
    def setUp(self):
        self.network=patch.object(socket.socket,'connect',side_effect=AssertionError('external network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)


class ProbeSpacingTests(NoNetworkCase):
    def sample(self, delays, start=None):
        value=task();wall=[value['release_at']-420 if start is None else start];calls=[0]
        def advance(seconds):wall[0]+=seconds
        def provider():
            advance(delays[min(calls[0],len(delays)-1)]);calls[0]+=1
            return ['07:00:00']
        def curl(*args,**kwargs):
            advance(.12)
            return 0,json.dumps({'code':0,'data':[{'sysKey':'resvCode','sysValue':'0'}]}).encode()+public_probe.MARKER+b'200,0.001,0.004,0.010,0.020,0.100'
        with patch.object(public_probe.time,'time',lambda:wall[0]),patch.object(public_probe.time,'monotonic',lambda:wall[0]),patch.object(public_probe.time,'sleep',advance),patch.object(public_probe.shutil,'which',lambda _:'/fixture/curl'),patch.object(public_probe,'command_info',lambda _:{'version':'fixture'}),patch.object(public_probe,'bounded_run',side_effect=curl) as network:
            report=public_probe.sample_node('mac',6,10,release_provider=provider)
        return report,wall[0],network.call_count

    def test_rpc_latency_change_still_yields_accepted_six_samples(self):
        for delays in ([.2,0],[.01,.5,.1,.4,0,.05]):
            with self.subTest(delays=delays):
                report,now,calls=self.sample(delays)
                self.assertEqual(calls,6)
                self.assertTrue(all(x['public_response_valid'] for x in report['samples']))
                _report(report,'mac',task(),now,False)
                self.assertTrue(all(b['at']-a['at']>=9.95 for a,b in zip(report['samples'],report['samples'][1:])))

    def test_rpc_crossing_quiet_boundary_makes_no_public_request(self):
        report,_,calls=self.sample([3],start=task()['release_at']-307)
        self.assertEqual(calls,0)
        self.assertEqual(report['samples'][0]['error'],'release_window')


class AccountIsolationTests(NoNetworkCase):
    def agent(self, failure=None, capability=None, now=None, sample=None, diagnostics=None):
        self.rows=[task('fixture-bad'),task('fixture-good')];calls=[]
        class RPC:
            def call(_self,op,**body):
                ref=body.get('task',{}).get('account_ref');calls.append((op,ref))
                if op=='tasks':return {'tasks':self.rows}
                if op=='status':return {'phase':'collecting','owner':None}
                if op=='report' and ref=='fixture-bad' and failure:raise failure
                return {}
            def close(_self):pass
        class Executor:
            def abort(self,*args):pass
            def close(self):pass
        kwargs={} if diagnostics is None else {'diagnostics':diagnostics}
        agent=NodeAgent(RPC(),'mac',Executor(),None,capability or (lambda _:{}),
                        clock=lambda:task()['release_at']-420 if now is None else now,
                        sample=sample or (lambda *a:{}),boot='fixture',**kwargs)
        self.addCleanup(agent.close);self.calls=calls
        return agent

    def completed(self,agent,value=None):
        future=Future();future.set_result({} if value is None else value)
        agent.probes[task()['release_at']]=future

    def test_bad_report_does_not_block_healthy_accounts(self):
        for code in ('node_not_ready','invalid_report','account_inactive','unknown_task'):
            with self.subTest(code=code):
                agent=self.agent(failure=RPCError(code));self.completed(agent)
                for _ in range(3):agent.step()
                self.assertIn(('report','fixture-good'),self.calls)
                agent.close()

    def test_shared_clock_and_ledger_faults_still_stop_current_step(self):
        for code in ('clock_untrusted','ledger_unavailable','anchor_mismatch','rpc_result_unknown'):
            with self.subTest(code=code):
                agent=self.agent(failure=RPCError(code));self.completed(agent)
                with self.assertRaises(RPCError):agent.step()
                self.assertNotIn(('status','fixture-good'),self.calls)
                agent.close()

    def test_shared_capability_clock_error_is_not_account_failure(self):
        def capability(_):raise ValueError('signed time evidence invalid')
        agent=self.agent(capability=capability);self.completed(agent)
        with self.assertRaises(ValueError):agent.step()
        self.assertNotIn(('status','fixture-good'),self.calls)

    def test_probe_failure_gets_only_one_bounded_resample(self):
        now=task()['release_at']-420;agent=self.agent(failure=RPCError('invalid_report'),now=now)
        self.completed(agent)
        agent.probe_attempts={task()['release_at']:1}
        agent.step()
        agent.clock=lambda:now+3
        agent.step()
        agent.probes[task()['release_at']].result(timeout=2)
        self.assertEqual(agent.probe_attempts[task()['release_at']],2)
        agent.clock=lambda:now+6;agent.step();agent.clock=lambda:now+9;agent.step()
        self.assertEqual(agent.probe_attempts[task()['release_at']],2)

    def test_account_or_captcha_rejection_never_resamples_public_endpoint(self):
        for code in ('node_not_ready','captcha_required','account_inactive'):
            agent=self.agent(failure=RPCError(code));self.completed(agent)
            agent.probe_attempts={task()['release_at']:1}
            agent.step()
            self.assertFalse(agent.probe_retry_at)
            agent.close()

    def test_resample_budget_does_not_extend_into_quiet_window(self):
        now=task()['release_at']-366
        agent=self.agent(failure=RPCError('invalid_report'),now=now);self.completed(agent)
        agent.probe_attempts={task()['release_at']:1};agent.step()
        self.assertFalse(agent.probe_retry_at)
        self.assertEqual(agent.probe_attempts[task()['release_at']],1)

    def test_failing_diagnostic_storage_does_not_block_healthy_account(self):
        from local_app.dual_node.diagnostics import NodeDiagnostics
        diag=NodeDiagnostics('/not-created/diagnostics',node_id='mac')
        with patch.object(diag,'_write',side_effect=OSError('private fixture error')):
            agent=self.agent(failure=RPCError('node_not_ready'),diagnostics=diag);self.completed(agent)
            agent.step()
        self.assertIn(('report','fixture-good'),self.calls)

    def test_late_recovery_and_expired_release_never_launch_probe(self):
        for offset in (-359,1,31):
            agent=self.agent(now=task()['release_at']+offset);agent.step()
            self.assertFalse(agent.probes)
            self.assertNotIn(('report','fixture-good'),self.calls)
            agent.close()


class DiagnosticTests(NoNetworkCase):
    def test_only_fixed_codes_and_hashed_task_reference_are_persisted(self):
        from local_app.dual_node.diagnostics import NodeDiagnostics
        with tempfile.TemporaryDirectory() as tmp:
            clock=[1000.]
            diag=NodeDiagnostics(Path(tmp)/'diagnostics',node_id='mac',clock=lambda:clock[0])
            for _ in range(900):
                clock[0]+=31
                diag.record('report','invalid_report',task=task('fixture-private-student-secret'))
            diag.record('raw-secret-phase','raw-secret-code',task={'secret':'never'})
            body=''.join(p.read_text() for p in (Path(tmp)/'diagnostics').iterdir() if p.is_file())
            self.assertNotIn('private-student-secret',body);self.assertNotIn('raw-secret',body);self.assertNotIn('never',body)
            self.assertLess(len(body.encode()),131072)
            rows=[json.loads(v) for v in body.splitlines()]
            self.assertTrue(rows);self.assertIn('task_ref',rows[0]);self.assertEqual(len(list((Path(tmp)/'diagnostics').iterdir())),2)
            self.assertTrue(all(set(v)<= {'at','node','phase','code','task_ref','count','sample_count','success_count'} for v in rows))

    def test_diagnostic_write_failure_is_nonfatal_and_does_not_follow_symlink(self):
        from local_app.dual_node.diagnostics import NodeDiagnostics
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sentinel=root/'sentinel';sentinel.write_text('retained')
            (root/'diagnostics').symlink_to(sentinel)
            diag=NodeDiagnostics(root/'diagnostics',node_id='mac');diag.record('report','invalid_report',task=task())
            self.assertEqual(sentinel.read_text(),'retained')
