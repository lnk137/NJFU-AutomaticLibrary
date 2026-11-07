import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.config import load_settings
from local_app.mail_config import load_mail_config, SMTPSender, MailConfig
from local_app.models import AppError, ZONE
from local_app.notifications import NotificationService
from support.account_fixture import services
from support.mongo_fixture import MongoFixture
ROOT = Path(__file__).resolve().parents[1]


class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()
    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, self.catalog = services(self.db)
        self.account = self.accounts.verify_and_save('fixture01', 'pass', 'A')
        self.other = self.accounts.verify_and_save('fixture02', 'pass', 'B')
        self.now = datetime(2026, 11, 6, 8, tzinfo=ZONE).timestamp()
        self.messages = []
        self.settings = load_settings(ROOT, {'data_dir': Path(self.temp.name)})
        self.service = self.make_service()
    def make_service(self, sender=None):
        return NotificationService(self.settings, self.accounts, self.store, self.accounts.vault,
            sender=sender or self.messages.append, clock=lambda: self.now)
    def tearDown(self):
        self.service.close(); self.accounts.close(); self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)
    def bind(self, account=None, address='receiver@example.com'):
        account = account or self.account
        result = self.service.request_verification(account.id, {'address': address})
        self.assertNotIn('code', result)
        self.service.run_once()
        code = re.search(r'\b[0-9]{6}\b', self.messages[-1]['body']).group()
        return self.service.confirm_verification(account.id, {'challenge_id': result['challenge_id'], 'code': code})
    def enable(self, results=True, reminders=True):
        prefs = self.service.get_settings(self.account.id)
        return self.service.update_preferences(self.account.id, {'revision': prefs['revision'],
            'results_enabled': results, 'reminders_enabled': reminders, 'reminder_minutes': 30})
    def record(self, **overrides):
        return {'id': 'receipt-1', 'student_id': 'fixture01', 'seat_id': '1',
                'begin': '2026-11-06 09:00:00', 'end': '2026-11-06 12:00:00', 'status': 1027, **overrides}
    def snapshot(self, records=None, **extra):
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {
            'snapshot': records if records is not None else [self.record()],
            'snapshot_at': datetime.fromtimestamp(self.now, ZONE).isoformat(), **extra}})
    def saved_run(self, state='success', key='run-1', **overrides):
        row = {'_id': key, 'account_id': self.account.id, 'created': self.now,
            'status': state, 'attempted_seat_id': '1', 'submitted_at': self.now,
            'spec': {'account_id': self.account.id, 'kind': 'scheduled', 'due_at': self.now,
                     'target_date': '2026-11-06', 'begin': '09:00', 'end': '12:00', 'seat_ids': ['1']},
            'result': {'status': state, 'records': [self.record()],
                       'verified_at': datetime.fromtimestamp(self.now, ZONE).isoformat()},
            'attempts': [{'outcome': 'rejected', 'code': 9}], **overrides}
        self.store.runs.replace_one({'_id': key}, row, upsert=True)
        return row
    def test_defaults_are_disabled_and_read_does_not_mutate_accounts(self):
        before = list(self.store.accounts.find())
        service = NotificationService(self.settings, self.accounts, self.store, self.accounts.vault)
        self.assertFalse(service.status()['configured'])
        self.assertFalse(service.get_settings(self.account.id)['results_enabled'])
        service.start(); self.assertFalse(service.status()['running'])
        self.assertEqual(service.run_once(), 0)
        self.assertEqual(list(self.store.accounts.find()), before)
        self.assertEqual(self.service.preferences.count_documents({}), 0)
    def test_verification_async_encrypted_single_use_independent_revision(self):
        before = self.store.get_account(self.account.id)
        result = self.service.request_verification(self.account.id, {'address': 'receiver@example.com'})
        self.assertEqual(self.messages, [])
        self.assertNotIn('receiver@example.com', str(self.service.challenges.find_one()) + str(self.service.outbox.find_one()))
        self.service.run_once()
        code = re.search(r'\b[0-9]{6}\b', self.messages[-1]['body']).group()
        self.assertNotIn(code, str(self.service.challenges.find_one()))
        data = {'challenge_id': result['challenge_id'], 'code': code}
        prefs = self.service.confirm_verification(self.account.id, data)
        self.assertTrue(prefs['verified']); self.assertFalse(prefs['results_enabled'])
        with self.assertRaises(AppError): self.service.confirm_verification(self.account.id, data)
        self.assertEqual(self.store.get_account(self.account.id), before)
    def test_unverified_optin_conflict_and_email_header_validation(self):
        with self.assertRaises(AppError): self.enable()
        for address in ['a@example.com\r\nBcc:b@example.com', 'not-an-email', 'a@@example.com', 'a@b']:
            with self.subTest(address=address), self.assertRaises(AppError):
                self.service.request_verification(self.account.id, {'address': address})
        self.bind(); prefs = self.enable()
        with self.assertRaises(AppError) as cm:
            self.service.update_preferences(self.account.id, dict(revision=prefs['revision'] - 1,
                results_enabled=False, reminders_enabled=False, reminder_minutes=30))
        self.assertEqual(cm.exception.code, 'notification_conflict')
    def test_account_destination_rate_limits_and_maximum_guesses(self):
        result = self.service.request_verification(self.account.id, {'address': 'receiver@example.com'})
        for account in (self.account, self.other):
            with self.assertRaises(AppError) as cm:
                self.service.request_verification(account.id, {'address': 'receiver@example.com'})
            self.assertEqual(cm.exception.http_status, 429)
        with patch('local_app.notifications.hmac.compare_digest', return_value=False):
            for _ in range(5):
                with self.assertRaises(AppError):
                    self.service.confirm_verification(self.account.id, {'challenge_id': result['challenge_id'], 'code': '000000'})
        self.assertEqual(self.service.challenges.find_one()['attempts'], 5)
        self.service.run_once(); self.assertEqual(self.messages, [])
    def test_address_rebinding_turns_off_preferences_and_expiry(self):
        self.bind(); self.enable(); self.now += 61
        result = self.service.request_verification(self.account.id, {'address': 'new@example.com'})
        self.service.run_once(); code = re.search(r'\b[0-9]{6}\b', self.messages[-1]['body']).group()
        prefs = self.service.confirm_verification(self.account.id, {'challenge_id': result['challenge_id'], 'code': code})
        self.assertEqual(prefs['address_masked'], 'n***@example.com')
        self.assertFalse(prefs['results_enabled']); self.assertFalse(prefs['reminders_enabled'])
        self.now += 61
        pending = self.service.request_verification(self.account.id, {'address': 'last@example.com'})
        self.now += 601
        with self.assertRaises(AppError):
            self.service.confirm_verification(self.account.id, {'challenge_id': pending['challenge_id'], 'code': '123456'})
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
    def test_result_dedup_reconcile_restart_no_history_or_booking_writes(self):
        self.bind()
        self.saved_run(key='history', created=self.now - 600,
            spec={'account_id': self.account.id, 'kind': 'scheduled', 'due_at': self.now - 600})
        self.enable(); self.now += 300; self.saved_run('uncertain'); self.now += 121
        before, runs = self.store.get_account(self.account.id), list(self.store.runs.find())
        self.service.run_once(); self.assertIn('待确认', self.messages[-1]['subject'])
        self.assertIn('不代表预约失败', self.messages[-1]['body'])
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.store.get_account(self.account.id), before)
        self.assertEqual(list(self.store.runs.find()), runs)
        self.saved_run('success'); self.service.run_once()
        self.assertEqual(self.messages[-1]['subject'], '预约成功')
        self.assertIn('record_id=receipt-1', self.messages[-1]['body'])
        self.make_service().run_once(); self.assertEqual(len(self.messages), 3)
    def test_manual_explicit_failure_and_backend_fault_exclusion(self):
        self.bind(); self.enable(); self.now += 300
        manual = self.saved_run('failed', key='manual'); manual['spec']['kind'] = 'manual'
        self.store.runs.replace_one({'_id': 'manual'}, manual)
        self.saved_run('failed', key='backend', attempts=[], result={'status': 'failed', 'message': 'secret backend trace'})
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[-1]['subject'], '预约失败')
        self.assertNotIn('secret', self.messages[-1]['body'])
    def test_success_requires_exact_identity_status_seat_and_interval(self):
        self.bind(); self.enable(); self.now += 300
        for change in ({'student_id': 'fixture02'}, {'status': '1027'}, {'seat_id': '2'}, {'begin': '2026-11-06 10:00:00'}):
            self.saved_run(result={'status': 'success', 'verified_at': datetime.fromtimestamp(self.now, ZONE).isoformat(),
                'records': [self.record(**change)]})
            self.service.run_once()
        self.assertEqual(len(self.messages), 1)
    def test_reminder_uses_receipt_default30_and_restart_dedup(self):
        self.bind(); self.enable(results=False); self.snapshot()
        before = self.store.get_account(self.account.id)
        self.now += 1800; self.service.run_once()
        self.assertEqual(len(self.messages), 2); self.assertIn('使用提醒', self.messages[-1]['subject'])
        for text in ('最近核对', '使用时段', '座位：'):
            self.assertIn(text, self.messages[-1]['body'])
        self.assertIn('action', self.messages[-1])
        self.assertEqual(self.messages[-1]['action']['label'], '取消预约')
        url = urlsplit(self.messages[-1]['action']['url'])
        self.assertEqual((url.scheme, url.netloc, url.path, url.query),
                         ('http', '127.0.0.1:5003', '/static/mail-cancel.html', ''))
        from local_app.mail_cancellation import read_ticket
        claims = read_ticket(self.accounts.vault.key, parse_qs(url.fragment)['ticket'][0], self.now)
        self.assertEqual(claims['account_id'], self.account.id)
        self.assertEqual(claims['record'], self.record())
        self.assertEqual(claims['expires_at'], datetime(2026, 11, 6, 9, tzinfo=ZONE).timestamp())
        self.assertIn(self.messages[-1]['action']['url'], self.messages[-1]['body'])
        self.assertNotIn(self.messages[-1]['action']['url'], self.messages[-1]['html_body'])
        self.assertIn('此链接仅能在运行预约后台的电脑上打开。', self.messages[-1]['body'])
        self.make_service().run_once(); self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.store.get_account(self.account.id), before)
    def test_reminder_action_uses_public_origin_and_encodes_exact_receipt(self):
        self.bind(); self.enable(results=False)
        self.service.settings = replace(self.settings, public_origin='https://library.example.com:5443')
        self.snapshot([self.record(id='receipt &other=wrong/"<tag>')])
        self.now += 1800; self.service.run_once()
        self.assertIn('action', self.messages[-1])
        self.assertEqual(self.messages[-1]['action']['label'], '取消预约')
        url = urlsplit(self.messages[-1]['action']['url'])
        self.assertEqual((url.scheme, url.netloc, url.path, url.query),
                         ('https', 'library.example.com:5443', '/static/mail-cancel.html', ''))
        from local_app.mail_cancellation import read_ticket
        claims = read_ticket(self.accounts.vault.key, parse_qs(url.fragment)['ticket'][0], self.now)
        self.assertEqual(claims['record']['id'], 'receipt &other=wrong/"<tag>')
        self.assertNotIn('此链接仅能', self.messages[-1]['body'])
    def test_verification_result_and_cancellation_have_no_action(self):
        self.bind(); self.enable(reminders=False)
        self.assertNotIn('action', self.messages[-1])
        self.now += 300; self.saved_run(); self.service.run_once()
        self.assertEqual(self.messages[-1]['subject'], '预约成功')
        self.assertNotIn('action', self.messages[-1])
        self.assertNotIn('intent=cancel', self.messages[-1]['body'])
        self.db.campus_cancellations.insert_one({'_id': 'cancel-1', 'account_id': self.account.id,
            'record': self.record(), 'state': 'success', 'ack': True, 'completed_at': self.now})
        self.service.run_once()
        self.assertEqual(self.messages[-1]['subject'], '预约已取消')
        self.assertNotIn('action', self.messages[-1])
        self.assertNotIn('intent=cancel', self.messages[-1]['body'])
    def test_reminder_rejects_unknown_wrong_identity_stale_error_and_expired(self):
        self.bind(); self.enable(results=False); self.now += 1800
        variants = [({'status': '1027'}, {}), ({'student_id': 'fixture02'}, {}),
            ({}, {'snapshot_at': datetime.fromtimestamp(self.now - 36 * 3600 - 1, ZONE).isoformat()}),
            ({}, {'query_error': 'failed'})]
        for record, extra in variants:
            self.snapshot([self.record(**record)], **extra); self.service.run_once()
        self.assertEqual(len(self.messages), 1)
        self.now += 301; self.snapshot(query_error=None)
        self.service.run_once(); self.assertEqual(len(self.messages), 1)
    def test_recheck_unsubscribe_and_access_before_send(self):
        self.bind(); self.enable(); self.snapshot(); self.now += 1800; self.service._discover()
        prefs = self.service.get_settings(self.account.id)
        self.service.update_preferences(self.account.id, dict(revision=prefs['revision'],
            results_enabled=False, reminders_enabled=False, reminder_minutes=30))
        self.service.run_once(); self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.service.outbox.find_one({'kind': 'reminder'})['status'], 'cancelled')
        self.accounts.is_allowed = lambda student: False
        with self.assertRaises(AppError): self.service.get_settings(self.account.id)
    def test_acknowledged_cancellation_suppresses_reminder_and_sends_once(self):
        self.bind(); self.enable(); self.snapshot(); self.now += 1800; self.service._discover()
        self.db.campus_cancellations.insert_one({'_id': 'cancel-1', 'account_id': self.account.id,
            'record': self.record(), 'state': 'success', 'ack': True, 'completed_at': self.now})
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[-1]['subject'], '预约已取消')
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
    def test_uncertain_cancellation_does_not_suppress_reminder(self):
        self.bind(); self.enable(results=False); self.snapshot(); self.now += 1800
        self.db.campus_cancellations.insert_one({'_id': 'cancel-1', 'account_id': self.account.id,
            'record': self.record(), 'state': 'uncertain', 'ack': False, 'completed_at': self.now})
        self.service.run_once(); self.assertIn('使用提醒', self.messages[-1]['subject'])
    def test_smtp_ambiguous_error_and_restart_never_retry(self):
        calls = []
        def failing(payload): calls.append(payload); raise TimeoutError('secret destination')
        self.service.sender = failing
        self.service.request_verification(self.account.id, {'address': 'receiver@example.com'})
        self.assertEqual(self.service.run_once(), 0)
        self.make_service(failing).run_once(); self.assertEqual(len(calls), 1)
        row = self.service.outbox.find_one(); self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('secret', str(row)); self.assertEqual(self.service.status()['error'], 'delivery_not_confirmed')
    def test_release_window_defers_work_without_account_lock_or_school_calls(self):
        self.service.request_verification(self.account.id, {'address': 'receiver@example.com'})
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'plan': {'enabled': True, 'trigger': '08:00:10'}}})
        self.accounts.account_lock = lambda *_: self.fail('notification acquired booking lock')
        self.accounts.client = lambda *_: self.fail('notification logged into school')
        before = list(self.service.outbox.find()); self.service.run_once()
        self.assertEqual(self.messages, []); self.assertEqual(list(self.service.outbox.find()), before)
        self.now += 131; self.service.run_once(); self.assertEqual(len(self.messages), 1)


    def test_raw_snapshot_conflict_discovery_and_delivery_are_rejected(self):
        self.bind(); self.enable(results=False); self.now += 1800
        invalid = [[self.record(), self.record(status=999)],
            [self.record(), self.record(id='other', student_id='fixture02')],
            [self.record(), self.record(id='overlap', begin='2026-11-06 11:00:00', end='2026-11-06 14:00:00')],
            [self.record(), self.record()]]
        for records in invalid:
            self.snapshot(records); self.service.run_once()
        self.assertEqual(self.service.outbox.count_documents({'kind': 'reminder'}), 0)
        self.snapshot(); self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'kind': 'reminder'}), 1)
        self.snapshot(invalid[0]); self.service.run_once()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.service.outbox.find_one({'kind': 'reminder'})['status'], 'cancelled')

    def test_early_opening_reminder_defers_past_release_then_sends_once(self):
        self.now = datetime(2026, 11, 6, 6, 50, tzinfo=ZONE).timestamp()
        self.bind(); self.enable(results=False)
        self.store.accounts.update_one({'_id': self.account.id}, {'$set': {'plan': {'enabled': True, 'trigger': '07:00:00'}}})
        self.snapshot([self.record(begin='2026-11-06 07:30:00', end='2026-11-06 10:00:00')])
        self.now = datetime(2026, 11, 6, 7, tzinfo=ZONE).timestamp()
        self.service.run_once(); self.assertEqual(len(self.messages), 1)
        self.now += 135
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
        self.assertIn('座位：A001', self.messages[-1]['body'])
        self.service.run_once(); self.assertEqual(len(self.messages), 2)
        self.service.outbox.delete_many({'kind': 'reminder'})
        self.now += 166
        self.service.run_once(); self.assertEqual(len(self.messages), 2)

    def test_preference_round_robin_reaches_account_201_and_wraps(self):
        account = self.store.get_account(self.account.id)
        for index in range(201):
            key, student = f'pagination-{index:03d}', f'page{index:03d}'
            row = {**account, '_id': key, 'student_id': student, 'identity': {**account['identity'], 'student_id': student},
                'snapshot': [self.record(student_id=student)], 'snapshot_at': datetime.fromtimestamp(self.now, ZONE).isoformat()}
            self.store.accounts.insert_one(row)
            self.service.preferences.insert_one({'_id': key, 'revision': 1, 'verified': True,
                'binding_id': 'bound', 'results_enabled': False, 'reminders_enabled': True,
                'reminder_minutes': 30, 'reminders_since': self.now - 1,
                'address': self.accounts.vault.seal('notification-address:' + key, 'page@example.com')})
        self.now += 1800
        self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'kind': 'reminder'}), 200)
        self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'kind': 'reminder'}), 201)
        self.assertIsNotNone(self.service.outbox.find_one({'account_id': 'pagination-200'}))
        self.service._discover()
        self.assertEqual(self.service._preference_cursor, 'pagination-199')

class MailConfigTests(unittest.TestCase):
    def test_private_config_permissions_schema_symlinks_and_redaction(self):
        with tempfile.TemporaryDirectory(prefix='njfu-mail-config-test-') as temp:
            path = Path(temp) / 'mail.json'; self.assertEqual(load_mail_config(temp), (None, None))
            data = dict(enabled=True, host='smtp.example.com', port=465, security='tls',
                        username='sender', password='secret-password', from_address='sender@example.com')
            path.write_text(json.dumps(data)); path.chmod(0o644)
            self.assertEqual(load_mail_config(temp)[1], 'unsafe_mail_config')
            path.chmod(0o600); config, error = load_mail_config(temp)
            self.assertIsNone(error); self.assertNotIn('secret-password', repr(config))
            self.assertNotIn("username='sender'", repr(config))
            data['security'] = 'plaintext'; path.write_text(json.dumps(data))
            self.assertEqual(load_mail_config(temp)[1], 'invalid_mail_config')
            path.unlink(); path.symlink_to(Path(temp) / 'absent')
            self.assertEqual(load_mail_config(temp)[1], 'unsafe_mail_config')
    def test_starttls_certificate_validation_precedes_login(self):
        import ssl
        config = MailConfig('smtp.example.com', 587, 'starttls', 'user', 'pass', 'from@example.com')
        calls = []
        class FakeSMTP:
            def __init__(self, *args, **kwargs): calls.append(('connect', kwargs))
            def ehlo(self): calls.append(('ehlo',))
            def starttls(self, context): calls.append(('tls', context.check_hostname, context.verify_mode))
            def login(self, *args): calls.append(('login',))
            def send_message(self, *args, **kwargs): calls.append(('send',)); return {}
            def close(self): calls.append(('close',))
        with patch('local_app.mail_config.smtplib.SMTP', FakeSMTP):
            SMTPSender(config)({'to': 'receiver@example.com', 'subject': 'test', 'body': 'body', 'message_id': '<test@local.invalid>'})
        self.assertLess(next(i for i, item in enumerate(calls) if item[0] == 'tls'),
                        next(i for i, item in enumerate(calls) if item[0] == 'login'))
        self.assertIn(('tls', True, ssl.CERT_REQUIRED), calls)


if __name__ == '__main__': unittest.main()
