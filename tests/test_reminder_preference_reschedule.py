"""Only never-attempted reminders may move to a newly selected due time."""
import unittest
from unittest.mock import patch

import test_notifications as snapshot_fixture
import test_dual_receipt_view as dual_fixture


class ReminderRescheduleCases:
    def account_id(self):
        return self.account['_id'] if isinstance(self.account, dict) else self.account.id

    def change_minutes(self):
        prefs = self.service.get_settings(self.account_id())
        return self.service.update_preferences(self.account_id(), dict(revision=prefs['revision'],
            results_enabled=False, reminders_enabled=True, reminder_minutes=10))

    def pending(self):
        self.service._discover()
        event = self.service.outbox.find_one({'kind': self.kind})
        self.assertEqual(event['status'], 'pending')
        self.assertNotIn('attempted_at', event)
        return event

    def assert_sent_once_at_new_due(self, original):
        self.now += 1200
        self.assertEqual(self.service.run_once(), 1)
        event = self.service.outbox.find_one({'_id': original['_id']})
        self.assertEqual(event['status'], 'sent')
        self.assertEqual(event['due_at'], original['due_at'] + 1200)
        self.assertEqual(event['attempted_at'], self.now)
        self.assertEqual(event['source'], original['source'])
        self.assertEqual(event['binding_id'], original['binding_id'])
        self.assertEqual(self.service.outbox.count_documents({'kind': self.kind}), 1)
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(len(self.messages), 1)

    def test_pending_reminder_uses_new_due_after_minutes_change(self):
        original = self.pending()
        self.change_minutes()
        self.assert_sent_once_at_new_due(original)

    def test_cancelled_unattempted_reminder_returns_at_new_due(self):
        original = self.pending()
        self.change_minutes()
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(self.service.outbox.find_one({'_id': original['_id']})['status'], 'cancelled')
        self.assert_sent_once_at_new_due(original)

    def test_sent_reminder_is_not_resent_after_minutes_change(self):
        original = self.pending()
        self.assertEqual(self.service.run_once(), 1)
        sent = self.service.outbox.find_one({'_id': original['_id']})
        self.change_minutes(); self.now += 1200
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(self.service.outbox.find_one({'_id': original['_id']}), sent)
        self.assertEqual(len(self.messages), 1)

    def test_unknown_delivery_is_not_retried_after_minutes_change(self):
        original = self.pending()
        def ambiguous_delivery(payload):
            raise TimeoutError('isolated ambiguous sender')
        self.service.sender = ambiguous_delivery
        self.assertEqual(self.service.run_once(), 0)
        unknown = self.service.outbox.find_one({'_id': original['_id']})
        self.assertEqual(unknown['status'], 'unknown')
        self.change_minutes(); self.now += 1200
        self.service.sender = self.messages.append
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(self.service.outbox.find_one({'_id': original['_id']}), unknown)
        self.assertEqual(self.messages, [])

    def test_rebound_email_cannot_adopt_an_old_reminder(self):
        original = self.pending()
        self.change_minutes()
        self.service.preferences.update_one({'_id': self.account_id()}, {'$set': {'binding_id': 'd' * 32}})
        self.now += 1200
        self.assertEqual(self.service.run_once(), 0)
        event = self.service.outbox.find_one({'_id': original['_id']})
        self.assertEqual(event['binding_id'], original['binding_id'])
        self.assertNotIn('attempted_at', event)
        self.assertEqual(self.messages, [])

    def test_reschedule_losing_to_an_attempt_cannot_reopen_delivery(self):
        original = self.pending()
        self.change_minutes(); self.service.run_once(); self.now += 1200
        update = self.service.outbox.update_one
        raced = False
        def competing_attempt(query, changes, *args, **kwargs):
            nonlocal raced
            if not raced and changes.get('$set', {}).get('status') == 'pending':
                raced = True
                update({'_id': original['_id']}, {'$set': {'status': 'unknown', 'attempted_at': self.now}})
            return update(query, changes, *args, **kwargs)
        with patch.object(self.service.outbox, 'update_one', side_effect=competing_attempt):
            self.assertEqual(self.service.run_once(), 0)
        event = self.service.outbox.find_one({'_id': original['_id']})
        self.assertEqual(event['status'], 'unknown')
        self.assertEqual(event['due_at'], original['due_at'])
        self.assertEqual(self.messages, [])


class SnapshotReminderRescheduleTests(ReminderRescheduleCases, unittest.TestCase):
    kind = 'reminder'
    setUpClass = classmethod(snapshot_fixture.NotificationTests.setUpClass.__func__)
    tearDownClass = classmethod(snapshot_fixture.NotificationTests.tearDownClass.__func__)
    tearDown = snapshot_fixture.NotificationTests.tearDown
    make_service = snapshot_fixture.NotificationTests.make_service
    bind = snapshot_fixture.NotificationTests.bind
    enable = snapshot_fixture.NotificationTests.enable
    snapshot = snapshot_fixture.NotificationTests.snapshot
    record = snapshot_fixture.NotificationTests.record

    def setUp(self):
        snapshot_fixture.NotificationTests.setUp(self)
        self.bind(); self.enable(results=False); self.snapshot(); self.now += 1800
        self.messages.clear()


class DualReminderRescheduleTests(ReminderRescheduleCases, unittest.TestCase):
    kind = 'dual_reminder'
    setUpClass = classmethod(dual_fixture.DualReceiptViewTests.setUpClass.__func__)
    tearDownClass = classmethod(dual_fixture.DualReceiptViewTests.tearDownClass.__func__)
    tearDown = dual_fixture.DualReceiptViewTests.tearDown
    mail = dual_fixture.DualReceiptViewTests.mail
    save_result = dual_fixture.DualReceiptViewTests.save_result
    reminder_time = dual_fixture.DualReceiptViewTests.reminder_time

    def setUp(self):
        dual_fixture.DualReceiptViewTests.setUp(self)
        self.reminder_time()
