"""Isolated dual-node health and persistent alert contracts; no school traffic."""
from pathlib import Path
import json
import tempfile
import unittest
import uuid
from unittest.mock import patch
import test_coordinator_ledger as fixtures
from test_coordinator_ledger import NOW, task, report, capability, prepared
from local_app.dual_node.rpc import dispatch
from local_app.execution_health import execution_health


class MonitoringTests(unittest.TestCase):
    def test_sync_alone_never_reports_ready(self):
        value=execution_health({'ready_to_submit':True,'dual_node_publisher':{
            'status':'synchronized','last_synced_at':100,'pending_changes':0}},managed=True,now=101)
        self.assertFalse(value['ready_to_submit'])
        self.assertEqual(value['dual_node_status']['state'],'unavailable')

    def test_dual_evidence_cannot_override_local_safety_gate_or_warning(self):
        for local in ({'ready_to_submit':False,'scheduler_warning':'local not ready'},
                      {'ready_to_submit':True,'clock_ok':False,'scheduler_warning':'clock paused'},
                      {'ready_to_submit':True,'allow_live':False},
                      {'ready_to_submit':True,'operator_paused':True}):
            with self.subTest(local=local):
                base=dict(local,dual_node_publisher={'status':'synchronized','last_synced_at':100,'pending_changes':0,
                    'execution_health':{'checked_at':100,'summary':{'state':'ready','ready':True,'degraded':False}}})
                value=execution_health(base,managed=True,now=101)
                self.assertFalse(value['ready_to_submit'])
                if local.get('scheduler_warning'):
                    self.assertEqual(value['scheduler_warning'],local['scheduler_warning'])

    def test_health_is_publisher_only_and_never_selects_or_expires(self):
        helper=fixtures.LedgerTests();clock=[NOW]
        with tempfile.TemporaryDirectory() as directory, helper.initial(Path(directory).resolve()/'authority',clock) as authority:
            request=lambda role,node:dispatch(authority,{'schema':1,'op':'health','request_id':uuid.uuid4().hex,'body':{}},role=role,node_id=node)
            self.assertFalse(request('node','mac')['ok'])
            initial=authority.db.execute('SELECT value FROM tasks').fetchall()
            observed=request('publisher',None)
            self.assertTrue(observed['ok'],observed)
            self.assertEqual(observed['result']['tasks'][0]['state'],'waiting')
            self.assertEqual(initial,authority.db.execute('SELECT value FROM tasks').fetchall())
            clock[0]=task()['release_at']+1
            observed=request('publisher',None)
            self.assertTrue(observed['ok'],observed)
            self.assertEqual(observed['result']['tasks'][0]['code'],'release_without_authorization')
            self.assertEqual(initial,authority.db.execute('SELECT value FROM tasks').fetchall())

    def test_valid_report_is_eligible_but_prepared_owner_is_ready(self):
        helper=fixtures.LedgerTests();clock=[NOW]
        with tempfile.TemporaryDirectory() as directory, helper.initial(Path(directory).resolve()/'authority',clock) as authority:
            clock[0]=task()['release_at']-300
            helper.reports(authority,clock[0],nodes=('mac',))
            health=authority.health()
            self.assertEqual(health['tasks'][0]['state'],'eligible')
            self.assertFalse(health['tasks'][0]['ready'])
            authority.select(task());helper.ready(authority,'mac',clock)
            health=authority.health()
            row=health['tasks'][0]
            self.assertEqual(row['state'],'ready');self.assertTrue(row['ready']);self.assertTrue(row['degraded'])
            authority.control(action='pause')
            self.assertEqual(authority.health()['tasks'][0]['state'],'paused')

    def test_alerts_persist_deduplicate_and_never_keep_payloads(self):
        from local_app.dual_node.monitor import AlertJournal
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'alerts.json';journal=AlertJournal(path)
            snapshot={'schema':1,'checked_at':100,'operator_paused':False,'nodes':[], 'tasks':[
                {'task_id':'a'*64,'state':'missed','code':'release_without_authorization','release_at':90,'account_ref':'fixture-one','credential':'private','message':'never retain'}]}
            journal.observe(snapshot);journal.observe(snapshot)
            journal=AlertJournal(path);journal.observe(snapshot)
            records=journal.status()['alerts']
            self.assertEqual(len(records),1);self.assertEqual(records[0]['occurrences'],3)
            self.assertNotIn('private',path.read_text());self.assertNotIn('fixture-one',path.read_text());self.assertNotIn('never retain',path.read_text())
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_alert_path_symlink_cannot_write_outside(self):
        from local_app.dual_node.monitor import AlertJournal
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);outside=root/'outside';outside.write_text('user-data')
            path=root/'alerts.json';path.symlink_to(outside)
            journal=AlertJournal(path)
            journal.observe({'schema':1,'checked_at':100,'operator_paused':False,'nodes':[], 'tasks':[]})
            self.assertEqual(outside.read_text(),'user-data');self.assertEqual(journal.status()['persistence'],'unavailable')

    def test_student_projection_uses_only_own_health(self):
        base={'ready_to_submit':True,'dual_node_publisher':{'status':'synchronized','last_synced_at':100,'pending_changes':0,
              'execution_health':{'checked_at':100,'accounts':{'one':{'state':'ready','ready':True,'degraded':True,'code':'owner_prepared'},
                  'two':{'state':'missed','ready':False,'code':'release_without_authorization'}}},'alerts':[{'private':'other account'}]}}
        mine=execution_health(base,managed=True,account_id='one',now=101)
        self.assertTrue(mine['ready_to_submit']);self.assertEqual(mine['dual_node_status']['state'],'ready')
        self.assertNotIn('other account',str(mine.get('dual_node_status')))
        self.assertNotIn('two',str(mine.get('dual_node_status')))
        other=execution_health(base,managed=True,account_id='two',now=101)
        self.assertFalse(other['ready_to_submit'])
        self.assertTrue(execution_health(base,managed=False,account_id='two',now=101)['ready_to_submit'])

class MonitoringBoundsTests(unittest.TestCase):
    def test_alert_lock_contention_returns_without_blocking(self):
        from local_app.dual_node.monitor import AlertJournal
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'alerts.json';fd=os.open(path.with_suffix('.lock'),os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                before=time.monotonic();journal=AlertJournal(path)
                journal.observe({'checked_at':100,'tasks':[]})
                self.assertLess(time.monotonic()-before,.5)
                self.assertEqual(journal.status()['persistence'],'unavailable')
            finally:os.close(fd)

    def test_ready_health_rejects_bad_clock_and_restarted_owner(self):
        from local_app.dual_node.contracts import DualNodeError
        helper=fixtures.LedgerTests();clock=[NOW]
        with tempfile.TemporaryDirectory() as directory, helper.initial(Path(directory).resolve()/'authority',clock) as authority:
            helper.reports(authority);authority.select(task());helper.ready(authority,'mac',clock)
            with patch.object(authority,'_clock_health',side_effect=DualNodeError('clock_untrusted')):
                row=authority.health()['tasks'][0]
                self.assertFalse(row['ready']);self.assertEqual(row['code'],'clock_untrusted')
            authority.heartbeat('mac',boot_id='new-mac-boot',seq=1)
            row=authority.health()['tasks'][0]
            self.assertFalse(row['ready']);self.assertEqual(row['code'],'owner_unavailable')

    def test_alerts_remain_bounded_and_mark_recovery(self):
        from local_app.dual_node.monitor import AlertJournal,MAX_BYTES,LIMIT
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'alerts.json';journal=AlertJournal(path)
            for i in range(140):
                journal.observe({'checked_at':100+i,'tasks':[{'task_id':format(i,'064x'),'code':'probe_report_missing'}]})
            self.assertEqual(len(journal.status()['alerts']),LIMIT)
            self.assertLess(path.stat().st_size,MAX_BYTES)
            journal.observe({'checked_at':300,'tasks':[]})
            self.assertFalse(any(x['active'] for x in journal.status()['alerts']))

    def test_health_response_fits_transport_and_keeps_no_replay_cache(self):
        from local_app.dual_node.contracts import canonical_bytes
        helper=fixtures.LedgerTests();clock=[NOW]
        with tempfile.TemporaryDirectory() as directory, helper.initial(Path(directory).resolve()/'authority',clock) as authority:
            for i in range(1,91):
                ref='fixture-extra-'+str(i);authority.enroll(ref,active=True)
                authority.publish(dict(task(),account_ref=ref))
            response=dispatch(authority,{'schema':1,'op':'health','request_id':uuid.uuid4().hex,'body':{}},role='publisher')
            self.assertTrue(response['ok'],response)
            self.assertEqual(len(response['result']['tasks']),91)
            self.assertLess(len(canonical_bytes(response)),65536)
            self.assertEqual(authority.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)

from test_calendar_mail_api import CalendarMailApiTests
from unittest.mock import Mock
import time


class PublisherMonitorTests(unittest.TestCase):
    def test_publisher_sync_collects_health_and_persists_deduplicated_alert(self):
        from test_dual_control import PublisherControlTests
        helper=PublisherControlTests('test_revoked_access_cancels_old_task_and_never_publishes')
        helper.setUp();self.addCleanup(helper.doCleanups)
        original=helper.call
        snapshot={'schema':1,'checked_at':helper.now,'operator_paused':False,'nodes':[{'node':'mac','online':True}],
            'tasks':[{'task_id':'a'*64,'account_ref':'fixture-one','release_at':helper.now+60,'phase':'collecting',
                      'state':'blocked','code':'probe_report_missing','ready':False,'degraded':True}]}
        def call(op,**body):
            if op=='health':return snapshot
            return original(op,**body)
        helper.client.call.side_effect=call
        with patch('time.time',return_value=helper.now):
            helper.loop.sync_once();helper.loop.sync_once()
        status=helper.loop.status()
        self.assertEqual(status['status'],'synchronized')
        self.assertEqual(status['execution_health']['accounts']['local-one']['code'],'probe_report_missing')
        alerts=status['monitor_alerts']['alerts'];self.assertEqual(len(alerts),1)
        self.assertEqual(alerts[0]['occurrences'],2)
        self.assertNotIn('fixture-one',json.dumps(alerts))
        self.assertTrue((helper.path.parent/'dual-monitor-alerts.json').exists())

    def test_downlevel_health_does_not_claim_execution_ready(self):
        from test_dual_control import PublisherControlTests
        helper=PublisherControlTests('test_revoked_access_cancels_old_task_and_never_publishes')
        helper.setUp();self.addCleanup(helper.doCleanups)
        with patch('time.time',return_value=helper.now):helper.loop.sync_once()
        status=helper.loop.status()
        self.assertEqual(status['monitor_code'],'monitor_unavailable')
        value=execution_health({'ready_to_submit':True,'dual_node_publisher':status},managed=True,now=helper.now)
        self.assertFalse(value['ready_to_submit']);self.assertEqual(value['dual_node_status']['state'],'unavailable')


class MonitoringApiTests(CalendarMailApiTests):
    def fixture_status(self):
        self.runtime.accounts.managed_gate=Mock()
        self.runtime.accounts.managed_gate.is_managed.return_value=True
        self.runtime.accounts.managed_gate.has_managed_scope.return_value=True
        now=time.time()
        publisher=Mock();self.runtime.dual_publisher=publisher
        publisher.status.return_value={'status':'synchronized','last_synced_at':now,'pending_changes':0,
            'execution_health':{'checked_at':now,'summary':{'state':'blocked','ready':False,'degraded':False},
                'accounts':{self.account.id:{'state':'ready','ready':True,'degraded':True},
                            'other-private-account':{'state':'missed','ready':False}}},
            'monitor_alerts':{'persistence':'available','alerts':[{'task_ref':'a'*16,'code':'probe_report_missing',
                'active':True,'first_seen':now,'message':'never echo private text'}]}}

    def test_student_sees_only_own_prepared_state(self):
        self.fixture_status();self.student()
        with patch.object(self.runtime.scheduler,'status',return_value={'ready_to_submit':True,'allow_live':True}):
            response=self.client.get('/api/local/status',base_url=self.origin)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['ready_to_submit']);self.assertEqual(response.json['dual_node_status']['state'],'ready')
        self.assertNotIn('other-private-account',str(response.json));self.assertNotIn('alerts',response.json)

    def test_root_dashboard_exposes_fixed_anonymous_alert(self):
        self.fixture_status()
        response=self.client.get('/api/local/admin/dashboard',base_url=self.origin)
        self.assertEqual(response.status_code,200,response.json)
        alerts=[x for x in response.json['alerts'] if x['type']=='dual_node']
        self.assertEqual(len(alerts),1);self.assertEqual(alerts[0]['task_ref'],'a'*16)
        self.assertNotIn('never echo private text',str(alerts))

if __name__=='__main__':unittest.main()
