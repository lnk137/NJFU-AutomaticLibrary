"""Local reconciliation is described as local and preserves immutable node proof."""
import unittest
import test_dual_receipt_view as helper
from local_app.dual_node.receipt_view import reconciliation_evidence


class LocalReceiptProjectionTests(unittest.TestCase):
    setUpClass = classmethod(helper.DualReceiptViewTests.setUpClass.__func__)
    tearDownClass = classmethod(helper.DualReceiptViewTests.tearDownClass.__func__)
    setUp = helper.DualReceiptViewTests.setUp
    tearDown = helper.DualReceiptViewTests.tearDown
    mail = helper.DualReceiptViewTests.mail
    save_result = helper.DualReceiptViewTests.save_result
    entries = helper.DualReceiptViewTests.entries

    def local_success(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        row = self.save_result('uncertain')
        evidence = reconciliation_evidence(self.account, row, self.record, self.now)
        self.store.runs.update_one({'_id': row['_id']}, {'$set': {'status': 'success',
            'dual_reconciliation': evidence, 'message': '本机查询已核实学校回执'}})
        return row

    def test_result_email_names_actual_verifier_and_links_real_receipt(self):
        row = self.local_success()
        self.assertEqual(self.service.run_once(), 1)
        message = self.messages[-1]
        self.assertIn('本机查询', message['body'])
        self.assertNotIn('执行节点已查询核验', message['body'])
        self.assertIn('record_id=actual-fixture-receipt-id', message['body'])
        self.assertEqual(self.store.get_run(row['_id'])['result'], row['result'])

    def test_calendar_marks_local_verifier_without_faking_node_completion(self):
        self.local_success()
        entry, = self.entries()
        self.assertEqual(entry['state'], 'confirmed')
        self.assertEqual(entry['source'], 'dual_local_verified')
        self.assertIn('本机查询', entry['message'])

    def test_power_projection_accepts_verified_local_success(self):
        from types import SimpleNamespace
        from local_app.power import PowerService
        self.local_success()
        self.store.accounts.update_one({'_id': self.account['_id']}, {'$set': {'verified_at': '2026-11-08T07:00:00+08:00'}})
        self.accounts.busy = set()
        self.store.managed_gate = SimpleNamespace(is_managed=lambda account_id: True)
        status = dict(allow_live=True, scheduler_running=True, clock_ok=True,
                      ready_to_submit=True, last_error=None, scheduler_warning=None)
        power = PowerService(self.settings, self.accounts, self.store,
                             SimpleNamespace(status=lambda: status), self.catalog,
                             status_provider=lambda: status)
        self.assertEqual(power.snapshot(wall=self.now, elapsed=100)['expected'], ['success'])
