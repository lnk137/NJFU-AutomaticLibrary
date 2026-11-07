"""Audited terminal outcomes notify safely without school or SMTP I/O."""
from dataclasses import asdict
import test_notifications as helper
from local_app.models import RunSpec
import unittest


class UnsentNotificationTests(unittest.TestCase):
    setUpClass = classmethod(helper.NotificationTests.setUpClass.__func__)
    tearDownClass = classmethod(helper.NotificationTests.tearDownClass.__func__)
    setUp = helper.NotificationTests.setUp
    tearDown = helper.NotificationTests.tearDown
    make_service = helper.NotificationTests.make_service
    bind = helper.NotificationTests.bind
    enable = helper.NotificationTests.enable

    def saved_terminal(self, state, *, sent=False):
        run = RunSpec(self.account.id, self.account.revision, '2026-11-07', '09:00', '12:00', ('1',), self.now)
        row = {'_id': run.key, 'account_id': self.account.id, 'target_date': run.target_date,
               'spec': asdict(run), 'created': self.now, 'status': state,
               'attempted_seat_id': '1' if sent else None,
               'message': 'secret backend trace', 'attempts': [],
               'result': {'status': state, 'message': 'secret backend trace', 'records': []}}
        if sent: row['submitted_at'] = self.now
        self.store.runs.insert_one(row)
        return self.store.get_run(row['_id'])

    def test_skipped_terminal_sends_one_redacted_non_submission_notice(self):
        self.bind(); self.enable(reminders=False); self.now += 300
        row = self.saved_terminal('skipped'); self.now += 121
        self.service.run_once(); self.service.run_once()
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[-1]['subject'], '本轮预约未执行')
        self.assertIn('未自动补交', self.messages[-1]['body'])
        self.assertNotIn('secret', str(self.messages[-1]))
        self.assertEqual(self.store.get_run(row['_id']), row)

    def test_failed_before_submission_notifies_without_claiming_school_rejected(self):
        self.bind(); self.enable(reminders=False); self.now += 300
        self.saved_terminal('failed'); self.now += 121
        self.service.run_once()
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[-1]['subject'], '预约执行异常')
        self.assertNotIn('学校明确拒绝', self.messages[-1]['body'])
        self.assertNotIn('secret', str(self.messages[-1]))

    def test_inconsistent_skipped_with_send_evidence_never_claims_not_sent(self):
        self.bind(); self.enable(reminders=False); self.now += 300
        self.saved_terminal('skipped', sent=True); self.now += 121
        self.service.run_once()
        self.assertEqual(len(self.messages), 1)

import test_dual_receipt_view as dual_helper


class DualUnsentNotificationTests(unittest.TestCase):
    setUpClass = classmethod(dual_helper.DualReceiptViewTests.setUpClass.__func__)
    tearDownClass = classmethod(dual_helper.DualReceiptViewTests.tearDownClass.__func__)
    setUp = dual_helper.DualReceiptViewTests.setUp
    tearDown = dual_helper.DualReceiptViewTests.tearDown
    mail = dual_helper.DualReceiptViewTests.mail
    save_result = dual_helper.DualReceiptViewTests.save_result

    def test_expired_without_grant_notifies_once_and_keeps_evidence(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        self.bridge.apply(self.task, dict(mode='production', authority_id='b'*32,
            task_id=self.task_id, binding_sha256=self.binding, phase='expired_unissued',
            owner=None, grant=None, result=None))
        before = list(self.store.runs.find())
        self.assertEqual(self.service.run_once(), 1)
        self.assertEqual(self.messages[-1]['subject'], '本轮预约未执行')
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(list(self.store.runs.find()), before)

    def test_tampered_progress_does_not_send_a_false_non_submission_notice(self):
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        self.bridge.apply(self.task, dict(mode='production', authority_id='b'*32,
            task_id=self.task_id, binding_sha256=self.binding, phase='cancelled',
            owner=None, grant=None, result=None))
        self.store.runs.update_one({}, {'$set': {'dual_binding.binding_sha256': 'f'*64}})
        self.assertEqual(self.service.run_once(), 0)
