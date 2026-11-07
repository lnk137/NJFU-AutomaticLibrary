"""Read-only school-query doubles; private Mongo, no campus or SMTP calls."""
import copy
from dataclasses import asdict
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'AL-BackEnd'), str(ROOT/'tests')]
from support.mongo_fixture import MongoFixture
from local_app.models import Identity, SchoolRecord
from local_app.reservations import ReservationService
from local_app.store import LocalStore
from local_app.dual_node.result_bridge import ResultBridge, run_spec
from local_app.dual_node.receipt_view import receipt_view
from test_result_bridge import status
from test_coordinator_contracts import task


class DualReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database(); self.addCleanup(self.mongo.client.drop_database, self.db.name)
        self.store = LocalStore(self.db)
        self.value = task() | {'account_ref': 'acct-fixture'}
        self.account = {'_id': 'local-one', 'student_id': 'fixture-student', 'revision': 1, 'credential_revision': 1}
        self.store.accounts.insert_one(copy.deepcopy(self.account))
        self.now = self.value['release_at'] + 60
        self.client = Mock(); self.client.identity = Identity('fixture-student', 'Fixture', '1')
        self.client.active_statuses = frozenset({1027})
        self.accounts = SimpleNamespace(account_lock=lambda _: threading.RLock(), client=lambda _: self.client)
        self.service = ReservationService(self.accounts, self.store, False, wall_clock=lambda: self.now)
        gate = Mock(); gate.is_managed.return_value = True
        self.bridge = ResultBridge(self.store, {'local-one': {'account_ref': 'acct-fixture', 'credential_revision': 1}}, gate, clock=lambda: self.now)
        self.response = status(self.value, 'uncertain'); self.response['mode'] = 'production'
        self.response['grant']['domain'] = 'njfu-dual-node-production-v1'
        self.bridge.apply(self.value, self.response)
        self.run = run_spec(self.value, 'local-one')
        self.record = SchoolRecord('real-fixture-record', 'fixture-student', self.run.seat_ids[1],
            f'{self.run.target_date} {self.run.begin}:00', f'{self.run.target_date} {self.run.end}:00', 1027)
        self.client.query.return_value = [self.record]

    def test_query_confirms_exact_receipt_without_rewriting_grant_or_mirror(self):
        before = self.store.get_run(self.run.key)
        result = self.service.reconcile(self.run.key)
        self.assertEqual(result.status, 'success')
        after = self.store.get_run(self.run.key)
        for field in ('dual_binding', 'dual_task', 'dual_progress', 'dual_result', 'dual_mirror', 'result'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['dual_reconciliation']['record'], asdict(self.record))
        view = receipt_view(self.account, after)
        self.assertIsNotNone(view)
        self.assertEqual(view.verification_source, 'local_query')
        self.assertTrue(view.matches_record(self.account, asdict(self.record)))
        self.assertEqual(view.seat_id, self.record.seat_id)
        self.client.submit.assert_not_called()
        # A later immutable server mirror cannot erase independent query evidence.
        self.bridge.apply(self.value, self.response)
        self.assertEqual(self.store.get_run(self.run.key), after)

    def test_empty_ambiguous_or_wrong_receipts_never_overwrite_the_mirror(self):
        for records in ([], [self.record, self.record],
                [SchoolRecord(**(asdict(self.record)|{'student_id':'different'}))],
                [SchoolRecord(**(asdict(self.record)|{'seat_id':'999'}))],
                [SchoolRecord(**(asdict(self.record)|{'begin':f'{self.run.target_date} 01:00:00'}))],
                [SchoolRecord(**(asdict(self.record)|{'status':1025}))]):
            with self.subTest(records=records):
                self.client.query.return_value = records
                before = self.store.get_run(self.run.key)
                self.assertEqual(self.service.reconcile(self.run.key).status, 'uncertain')
                after = self.store.get_run(self.run.key)
                self.assertEqual(after, before)

    def test_query_failure_and_wrong_authenticated_identity_preserve_original_evidence(self):
        before = self.store.get_run(self.run.key)
        self.client.query.side_effect = OSError('fixture network unavailable')
        self.assertEqual(self.service.reconcile(self.run.key).status, 'uncertain')
        self.assertEqual(self.store.get_run(self.run.key), before)
        self.client.query.side_effect = None
        self.client.identity = Identity('wrong', 'Fixture', '1')
        self.assertEqual(self.service.reconcile(self.run.key).status, 'uncertain')
        self.assertEqual(self.store.get_run(self.run.key), before)

    def test_supplemental_tampering_cannot_project_success(self):
        self.assertEqual(self.service.reconcile(self.run.key).status, 'success')
        original = self.store.get_run(self.run.key)
        mutations = [lambda r:r['dual_reconciliation'].update(grant_id='d'*32),
            lambda r:r['dual_reconciliation'].update(binding_sha256='d'*64),
            lambda r:r['dual_reconciliation'].update(receipt_ref='d'*64),
            lambda r:r['dual_reconciliation']['record'].update(student_id='wrong'),
            lambda r:r['dual_reconciliation']['record'].update(id='wrong'),
            lambda r:r['dual_reconciliation']['record'].update(seat_id='999'),
            lambda r:r['dual_reconciliation']['record'].update(status=1025),
            lambda r:r['dual_binding'].update(authority_id='invalid'),
            lambda r:r['dual_binding'].update(authority_id='d'*32),
            lambda r:r['dual_result'].update(attempt_count=5),
            lambda r:r['result'].update(records=[asdict(self.record)])]
        for mutate in mutations:
            altered = copy.deepcopy(original); mutate(altered)
            self.assertIsNone(receipt_view(self.account, altered))

    def test_normal_school_query_callback_uses_the_same_safe_confirmation(self):
        before = self.store.get_run(self.run.key)
        self.service.reconcile_account('local-one', [self.record], self.client.identity, frozenset({1027}))
        after = self.store.get_run(self.run.key)
        self.assertEqual(after['status'], 'success')
        self.assertEqual(after['result'], before['result'])
        self.assertEqual(after['dual_result'], before['dual_result'])
        self.client.query.assert_not_called()

    def test_concurrent_mirror_change_prevents_stale_confirmation(self):
        original = self.store.runs.update_one
        def racing(query, update, **options):
            if 'dual_reconciliation' in update.get('$set', {}):
                original({'_id': self.run.key}, {'$set': {'dual_progress.grant_id': 'd'*32}})
            return original(query, update, **options)
        from unittest.mock import patch
        with patch.object(self.store.runs, 'update_one', side_effect=racing):
            self.assertEqual(self.service.reconcile(self.run.key).status, 'uncertain')
        self.assertNotIn('dual_reconciliation', self.store.get_run(self.run.key))

    def test_late_node_result_still_mirrors_and_acknowledges_after_local_confirmation(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        authorized = dict(self.response, phase='authorized', result=None)
        self.bridge.apply(self.value, authorized)
        self.assertEqual(self.service.reconcile(self.run.key).status, 'success')
        evidence = self.store.get_run(self.run.key)['dual_reconciliation']
        self.response['result']['result_at'] = self.now + 10
        ack = self.bridge.apply(self.value, self.response)
        self.assertIsNotNone(ack)
        row = self.store.get_run(self.run.key)
        self.assertEqual(row['dual_reconciliation'], evidence)
        self.assertEqual(row['dual_result'], self.response['result'])
        self.assertEqual(row['status'], 'success')
        self.assertEqual(receipt_view(self.account, row).verification_source, 'local_query')
        client = Mock(); client.call.return_value = dict(self.response, mirror_ack=True)
        report = self.bridge.sync(client, [])
        self.assertEqual(report['acknowledged'], 1)

    def test_late_completed_or_unsent_results_preserve_independent_receipt_and_ack(self):
        for phase in ('completed', 'not_sent', 'failed'):
            with self.subTest(phase=phase):
                self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
                self.bridge.apply(self.value, dict(self.response, phase='authorized', result=None))
                self.assertEqual(self.service.reconcile(self.run.key).status, 'success')
                evidence = self.store.get_run(self.run.key)['dual_reconciliation']
                terminal = status(self.value, phase); terminal['mode']='production'
                terminal['grant']['domain']='njfu-dual-node-production-v1'
                if phase == 'completed': terminal['result']['receipt_ref']=evidence['receipt_ref']
                self.assertIsNotNone(self.bridge.apply(self.value, terminal))
                row=self.store.get_run(self.run.key)
                self.assertEqual(row['dual_reconciliation'], evidence)
                self.assertEqual(receipt_view(self.account,row).status,'success')

    def test_conflicting_late_node_receipt_does_not_overwrite_local_query_evidence(self):
        self.assertEqual(self.service.reconcile(self.run.key).status, 'success')
        before=self.store.get_run(self.run.key)
        terminal=status(self.value);terminal['mode']='production'
        terminal['grant']['domain']='njfu-dual-node-production-v1'
        with self.assertRaises(ValueError):self.bridge.apply(self.value,terminal)
        self.assertEqual(self.store.get_run(self.run.key),before)

    def test_power_requires_bound_account_for_local_receipt_success(self):
        from local_app.dual_node.result_bridge import dual_power_row
        self.assertEqual(self.service.reconcile(self.run.key).status, 'success')
        row = self.store.get_run(self.run.key)
        self.assertIsNone(dual_power_row([row], self.run, credential_revision=1))
        self.assertIsNone(dual_power_row([row], self.run, credential_revision=1,
            account=self.account|{'student_id':'wrong'}))
        self.assertEqual(dual_power_row([row], self.run, credential_revision=1, account=self.account), row)


if __name__ == '__main__': unittest.main()
