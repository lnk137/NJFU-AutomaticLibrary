"""Production-shaped proof projection and per-account status isolation."""
from copy import deepcopy
from unittest.mock import Mock, patch
import time
from test_dual_receipt_view import DualReceiptViewTests
from test_calendar_mail_api import CalendarMailApiTests
from local_app.dual_node.result_bridge import run_spec
from local_app.execution_health import execution_health


class CalendarProgressTests(DualReceiptViewTests):
    def test_every_valid_progress_deduplicates_and_preserves_state(self):
        for phase, expected in [('collecting','planned'),('selected','planned'),('ready','planned'),
                                ('authorized','uncertain'),('cancel_requested','uncertain'),
                                ('cancelled','skipped'),('expired_unissued','skipped'),('not_sent','skipped')]:
            with self.subTest(phase=phase):
                self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
                if phase == 'not_sent':
                    self.save_result(phase, attempts=0)
                else:
                    grant = None
                    if phase in ('authorized','cancel_requested'):
                        grant=dict(domain='njfu-dual-node-production-v1',payload=dict(authority_id='b'*32,
                            grant_id='c'*32,task_id=self.task_id,binding_sha256=self.binding,node_id='ubuntu',
                            release_at=self.due,credential_revision=1))
                    self.bridge.apply(self.task,dict(mode='production',authority_id='b'*32,
                        task_id=self.task_id,binding_sha256=self.binding,phase=phase,
                        owner=None if phase in ('collecting','cancelled','expired_unissued') else 'ubuntu',
                        grant=grant,result=None))
                entry, = self.entries()
                self.assertEqual(entry['state'],expected)
                self.assertNotIn('证据不足',entry['message'])

    def test_old_credential_progress_does_not_hide_new_plan(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        self.bridge.apply(self.task,dict(mode='production',authority_id='b'*32,task_id=self.task_id,
            binding_sha256=self.binding,phase='cancelled',owner=None,grant=None,result=None))
        self.store.accounts.update_one({'_id':self.account['_id']},{'$inc':{'credential_revision':1,'revision':1}})
        entries=self.entries()
        self.assertEqual(sorted(e['state'] for e in entries),['planned','skipped'])
        self.assertTrue(next(e for e in entries if e['state']=='skipped')['stale_plan'])

    def test_progress_conflict_never_suppresses_plan(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        self.bridge.apply(self.task,dict(mode='production',authority_id='b'*32,task_id=self.task_id,
            binding_sha256=self.binding,phase='collecting',owner=None,grant=None,result=None))
        self.store.runs.update_one({'_id':run_spec(self.task,self.account['_id']).key},
            {'$set':{'dual_binding.binding_sha256':'f'*64}})
        self.assertEqual(sorted(e['state'] for e in self.entries()),['planned','uncertain'])

    def test_unchanged_business_after_unrelated_revision_deduplicates(self):
        self.store.accounts.update_one({'_id':self.account['_id']},{'$inc':{'revision':1}})
        entry, = self.entries()
        self.assertEqual(entry['state'],'confirmed')
        self.assertFalse(entry.get('stale_plan',False))
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':{'plan.begin':'10:00'}})
        self.assertEqual(len(self.entries()),2)


class ExecutionHealthApiTests(CalendarMailApiTests):
    def test_student_sees_own_execution_failure_without_registry_details(self):
        self.runtime.accounts.managed_gate = Mock()
        self.runtime.accounts.managed_gate.is_managed.return_value=True
        self.runtime.dual_publisher=Mock()
        self.runtime.dual_publisher.status.return_value={'status':'blocked','code':'publisher_sync_failed',
            'registrations':{'private':'never expose'}}
        self.student()
        with patch.object(self.runtime.scheduler,'status',return_value=dict(allow_live=True,
                scheduler_running=True,clock_ok=True,ready_to_submit=True)):
            response=self.client.get('/api/local/status',base_url=self.origin)
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json['ready_to_submit'])
        self.assertEqual(response.json['dual_node_status']['state'],'blocked')
        self.assertNotIn('registrations',str(response.json))
        accounts=self.client.get('/api/local/accounts',base_url=self.origin).json
        self.assertFalse(accounts['accounts'][0]['capabilities']['date_override_segments'])

    def test_health_is_fresh_and_local_accounts_are_independent(self):
        base=dict(allow_live=True,scheduler_running=True,clock_ok=True,ready_to_submit=True,
            dual_node_publisher=dict(status='synchronized',last_synced_at=100,pending_changes=0))
        self.assertEqual(execution_health(base,managed=True,now=101)['dual_node_status']['state'],'unavailable')
        self.assertFalse(execution_health(base,managed=True,now=101)['ready_to_submit'])
        self.assertFalse(execution_health(base,managed=True,now=200)['ready_to_submit'])
        self.assertTrue(execution_health(base,managed=False,now=200)['ready_to_submit'])
