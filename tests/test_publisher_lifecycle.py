from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Publisher per-account failure isolation and pre-grant task retirement."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_app.dual_node.contracts import task_identity
from local_app.dual_node.publisher import plan_tasks
import test_coordinator_ledger as fixtures
import test_dual_control as controls


class PublisherLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.harness=controls.PublisherControlTests();self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_one_credential_mismatch_cancels_its_old_task_without_blocking_healthy_account(self):
        h=self.harness
        h.reg['local-bad']={'account_ref':'fixture-bad','credential_revision':1};h.write_config()
        bad=SimpleNamespace(id='local-bad',student_id='FixtureBad',revision=1,credential_revision=2,
                            plan=dict(h.plan),date_exceptions={})
        old={'_id':'local-bad','revision':1,'credential_revision':1,'plan':bad.plan,'date_exceptions':{}}
        h.known=[plan_tasks([old],h.reg,h.runtime.accounts.catalog,now=h.now,days=2)[0]]
        h.runtime.store.list_accounts.return_value=[bad,h.account]
        h.sync()
        cancelled=[body['task'] for op,body in h.operations if op=='cancel']
        published=[body['task'] for op,body in h.operations if op=='publish']
        self.assertTrue(any(row['account_ref']=='fixture-bad' for row in cancelled))
        self.assertTrue(any(row['account_ref']=='fixture-one' for row in published))
        self.assertFalse(any(row['account_ref']=='fixture-bad' for row in published))
        self.assertEqual(h.loop.state['status'],'blocked')

    def test_changed_binding_is_cancelled_before_republish_and_issued_owner_is_preserved(self):
        h=self.harness;helper=fixtures.LedgerTests()
        for issued in (False,True):
            with self.subTest(issued=issued),TemporaryDirectory(prefix='njfu-publisher-replan-') as temporary:
                clock=[fixtures.NOW]
                with helper.initial(Path(temporary).resolve()/'authority',clock) as authority:
                    old=fixtures.task();new=old|{'revision':2,'end_minute':old['end_minute']+1}
                    grant=None
                    if issued:
                        helper.reports(authority);authority.select(old);helper.ready(authority,'mac',clock)
                        grant=authority.authorize('mac',old)['grant']
                    h.reg={'local-one':{'account_ref':old['account_ref'],'credential_revision':1}}
                    h.config['registrations']=h.reg;h.write_config();h.account.revision=2
                    h.now=clock[0];h.operations=[]
                    def call(op,**body):
                        h.operations.append((op,body))
                        if op=='tasks':return authority.tasks(include_unmirrored=True)
                        if op=='publish':return authority.publish(body['task'])
                        if op=='cancel':return authority.cancel(body['task'],reason=body.get('reason','operator_cancelled'))
                        if op=='control':return authority.control(action=body['action'])
                        raise AssertionError('unexpected fixture operation')
                    h.client.call.side_effect=call
                    with patch('local_app.dual_node.publisher.plan_tasks',return_value=[new]):h.sync()
                    cancels=[index for index,(op,body) in enumerate(h.operations)
                             if op=='cancel' and task_identity(body['task'])==task_identity(old)]
                    publishes=[index for index,(op,body) in enumerate(h.operations)
                               if op=='publish' and task_identity(body['task'])==task_identity(new)]
                    self.assertTrue(cancels,'old binding must be cancelled')
                    self.assertTrue(publishes,'new binding must be attempted after cancellation')
                    self.assertLess(cancels[0],publishes[0])
                    if issued:
                        remaining=authority.inspect_status(old)
                        self.assertEqual(remaining['owner'],'mac');self.assertEqual(remaining['grant'],grant)
                        self.assertTrue(remaining['cancel_requested'])
                        self.assertEqual(h.loop.state['status'],'blocked')
                        with self.assertRaises(ValueError):authority.inspect_status(new)
                    else:
                        accepted=authority.inspect_status(new)
                        self.assertEqual(accepted['phase'],'collecting');self.assertIsNone(accepted['grant'])


class ExpiredUnissuedTests(unittest.TestCase):
    def test_paused_collecting_selected_and_ready_retire_from_both_status_and_task_listing(self):
        helper=fixtures.LedgerTests()
        for phase in ('collecting','selected','ready'):
            for entry in ('status','tasks'):
                with self.subTest(phase=phase,entry=entry),TemporaryDirectory(prefix='njfu-unissued-expiry-') as temporary:
                    clock=[fixtures.NOW]
                    with helper.initial(Path(temporary).resolve()/'authority',clock) as authority:
                        if phase!='collecting':
                            helper.reports(authority);authority.select(fixtures.task())
                        if phase=='ready':helper.ready(authority,'mac',clock)
                        self.assertEqual(authority.inspect_status(fixtures.task())['phase'],phase)
                        authority.control(action='pause');clock[0]=fixtures.task()['release_at']+1
                        if entry=='tasks':authority.tasks('mac')
                        else:authority.status(fixtures.task())
                        expired=authority.inspect_status(fixtures.task())
                        self.assertEqual(expired['phase'],'expired_unissued')
                        self.assertIsNone(expired['grant']);self.assertTrue(expired['operator_paused'])


if __name__=='__main__':unittest.main()
