"""Reminder capabilities exercise the existing cancellation core on fake school."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.config import load_settings
from local_app.models import AppError, ZONE
from local_app.notifications import NotificationService
import test_cancellations as cancellation_fixture


class MailCancellationTests(unittest.TestCase):
    setUpClass = classmethod(cancellation_fixture.CancellationTests.setUpClass.__func__)
    tearDownClass = classmethod(cancellation_fixture.CancellationTests.tearDownClass.__func__)
    tearDown = cancellation_fixture.CancellationTests.tearDown

    def setUp(self):
        cancellation_fixture.CancellationTests.setUp(self)
        settings = load_settings(Path(__file__).resolve().parents[1], {'data_dir': Path(self.temp.name)})
        self.messages = []
        self.notifications = NotificationService(settings, self.accounts, self.store, self.accounts.vault,
            sender=self.messages.append, clock=lambda: self.now)
        self.addCleanup(self.notifications.close)
        self.binding = 'b' * 32
        self.notifications.preferences.insert_one({'_id': self.account.id, 'revision': 1,
            'verified': True, 'binding_id': self.binding, 'results_enabled': False,
            'reminders_enabled': True, 'reminder_minutes': 50, 'reminders_since': self.now - 1,
            'address': self.accounts.vault.seal('notification-address:' + self.account.id, 'fixture@example.com')})
        self.store.save_snapshot(self.account.id, self.account.revision, [self.record, self.sibling],
            datetime.fromtimestamp(self.now, ZONE).isoformat())

    def capability(self, status='sent'):
        self.notifications._discover()
        row = self.notifications.outbox.find_one({'kind': 'reminder'})
        payload = self.notifications._payload(row)
        self.assertEqual(payload.get('action', {}).get('label'), '取消预约')
        url = urlsplit(payload['action']['url'])
        self.assertEqual(url.path, '/static/mail-cancel.html')
        self.assertEqual(url.query, '')
        ticket = parse_qs(url.fragment)['ticket'][0]
        self.notifications.outbox.update_one({'_id': row['_id']}, {'$set': {'status': status}})
        from local_app.mail_cancellation import MailCancellationService
        return MailCancellationService(self.notifications, self.service, clock=lambda: self.now), ticket, row['_id']

    def test_exact_one_click_cancels_only_ticket_receipt_and_repeat_never_previews_again(self):
        service, ticket, key = self.capability()
        before = deepcopy(self.store.get_account(self.account.id))
        self.assertEqual(service.status(ticket)['state'], 'not_started')
        self.assertEqual(service.submit(ticket)['state'], 'success')
        with patch.object(self.service, 'preview', side_effect=AssertionError('Replay preview forbidden')):
            self.assertEqual(service.submit(ticket)['state'], 'success')
        self.assertEqual(self.school.cancels, [self.record.id])
        self.assertEqual(self.school.records, [self.sibling])
        after = self.store.get_account(self.account.id)
        for field in ('revision', 'credential_revision', 'plan', 'date_exceptions', 'secret'):
            self.assertEqual(before.get(field), after.get(field))
        claim = self.notifications.outbox.find_one({'_id': key})['mail_cancellation']
        self.assertNotIn(ticket, str(claim))

    def test_durable_claim_precedes_school_lookup_and_prevents_replay_after_early_timeout(self):
        service, ticket, key = self.capability('unknown')
        def timeout(*args, **kwargs):
            self.assertIn('mail_cancellation', self.notifications.outbox.find_one({'_id': key}))
            raise TimeoutError('synthetic lookup failure')
        with patch.object(self.service, 'preview', side_effect=timeout):
            self.assertEqual(service.submit(ticket)['state'], 'uncertain')
        with patch.object(self.service, 'preview', side_effect=AssertionError('Replay forbidden')):
            self.assertEqual(service.submit(ticket)['state'], 'uncertain')
            self.assertEqual(service.status(ticket)['state'], 'uncertain')
        self.assertEqual(self.school.cancels, [])

    def test_interrupted_claim_becomes_uncertain_and_never_restarts_lookup(self):
        service, ticket, _ = self.capability()
        with patch.object(self.service, 'preview', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): service.submit(ticket)
        self.assertEqual(service.status(ticket)['state'], 'pending')
        self.now += 181
        with patch.object(self.service, 'preview', side_effect=AssertionError('Interrupted operation must not replay')):
            self.assertEqual(service.status(ticket)['state'], 'uncertain')
            self.assertEqual(service.submit(ticket)['state'], 'uncertain')
        self.assertEqual(self.school.cancels, [])

    def test_two_hour_capability_expiry_is_enforced_inside_final_school_guard(self):
        self.record = replace(self.record, begin='2026-11-06 12:00:00', end='2026-11-06 14:00:00')
        self.school.records = [self.record, self.sibling]
        self.store.save_snapshot(self.account.id, self.account.revision, self.school.records,
            datetime.fromtimestamp(self.now, ZONE).isoformat())
        self.notifications.preferences.update_one({'_id': self.account.id}, {'$set': {'reminder_minutes': 230}})
        service, ticket, _ = self.capability()
        self.now += 7198
        original, calls = self.service._query, []
        def query(row):
            result = original(row); calls.append(1)
            if len(calls) == 2: self.now += 3
            return result
        with patch.object(self.service, '_query', side_effect=query):
            self.assertEqual(service.submit(ticket)['state'], 'failed')
        self.assertEqual(self.school.cancels, [])

    def test_unknown_cancel_never_replays_and_status_only_reconciles_acknowledged_rows(self):
        service, ticket, _ = self.capability('dispatching')
        self.school.cancel_mode = 'timeout'
        self.assertEqual(service.submit(ticket)['state'], 'uncertain')
        self.assertEqual(service.status(ticket)['state'], 'uncertain')
        self.assertEqual(service.submit(ticket)['state'], 'uncertain')
        self.assertEqual(self.school.cancels, [self.record.id])

    def test_temporary_pre_submit_guards_allow_an_explicit_retry_without_automatic_replay(self):
        service, ticket, key = self.capability()
        before = deepcopy(self.store.get_account(self.account.id))
        for code in ('release_window', 'execution_paused', 'account_busy', 'service_closed'):
            with self.subTest(code=code):
                with patch.object(self.service, 'preview', side_effect=AppError(code, 'Synthetic guard', 409)):
                    self.assertEqual(service.submit(ticket)['state'], 'not_started')
                self.assertEqual(self.service.rows.count_documents({}), 0)
                self.assertEqual(self.school.cancels, [])
                with patch.object(self.service, 'preview', side_effect=AssertionError('Status cannot retry')):
                    self.assertEqual(service.status(ticket)['state'], 'not_started')
                self.assertEqual(self.notifications.outbox.find_one({'_id': key})['mail_cancellation']['state'],
                                 'not_started')
        self.assertEqual(self.store.get_account(self.account.id), before)
        self.assertEqual(service.submit(ticket)['state'], 'success')
        self.assertEqual(service.submit(ticket)['state'], 'success')
        self.assertEqual(self.school.cancels, [self.record.id])

    def test_temporary_final_guard_before_durable_cancel_intent_can_retry(self):
        service, ticket, _ = self.capability()
        guard, calls = self.service._guard, []
        def transient(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise AppError('release_window', 'Synthetic final guard', 409)
            return guard(*args, **kwargs)
        with patch.object(self.service, '_guard', side_effect=transient):
            self.assertEqual(service.submit(ticket)['state'], 'not_started')
        self.assertEqual(self.service.rows.count_documents({}), 0)
        self.assertEqual(self.school.cancels, [])
        self.assertEqual(service.submit(ticket)['state'], 'success')
        self.assertEqual(self.school.cancels, [self.record.id])

    def test_guard_error_with_durable_intent_never_reopens_capability(self):
        service, ticket, _ = self.capability()
        def intent_then_guard(*args, **kwargs):
            from local_app.mail_cancellation import _cancel_id, read_ticket
            claims = read_ticket(self.accounts.vault.key, ticket, self.now)
            self.service.rows.insert_one({'_id': _cancel_id(claims), 'account_id': self.account.id,
                'record': claims['record'], 'state': 'submitting', 'requested_at': self.now, 'ack': False})
            raise AppError('execution_paused', 'Synthetic guard after intent', 503)
        with patch.object(self.service, 'preview', side_effect=intent_then_guard):
            self.assertEqual(service.submit(ticket)['state'], 'pending')
        with patch.object(self.service, 'preview', side_effect=AssertionError('Intent cannot replay')):
            self.assertEqual(service.submit(ticket)['state'], 'pending')
        self.assertEqual(self.school.cancels, [])

    def test_nontransient_failure_remains_nonreplayable(self):
        service, ticket, _ = self.capability()
        with patch.object(self.service, 'preview', side_effect=AppError('record_changed', 'Synthetic change', 409)):
            self.assertEqual(service.submit(ticket)['state'], 'failed')
        with patch.object(self.service, 'preview', side_effect=AssertionError('Failure cannot replay')):
            self.assertEqual(service.submit(ticket)['state'], 'failed')
        self.assertEqual(self.school.cancels, [])

    def test_ack_status_can_query_readback_but_never_cancel_again(self):
        service, ticket, _ = self.capability()
        self.school.cancel_mode = 'unverified'
        self.assertEqual(service.submit(ticket)['state'], 'uncertain')
        self.school.records = [self.sibling]
        self.assertEqual(service.status(ticket)['state'], 'success')
        self.assertEqual(self.school.cancels, [self.record.id])

    def test_ticket_tampering_size_types_and_expiry_reject_before_school(self):
        service, ticket, _ = self.capability()
        for invalid in (None, 12, {}, '', 'x' * 8193, ticket[:-1] + ('0' if ticket[-1] != '0' else '1')):
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(AppError):
                service.submit(invalid)
        self.now = datetime(2026, 11, 6, 9, tzinfo=ZONE).timestamp()
        with self.assertRaises(AppError): service.submit(ticket)
        self.assertEqual(self.school.cancels, [])

    def test_current_binding_credentials_access_outbox_status_and_exact_source_are_required(self):
        for change in ('binding', 'unverified', 'credentials', 'access', 'pending', 'cancelled', 'source'):
            with self.subTest(change=change):
                service, ticket, key = self.capability()
                pref = self.notifications.preferences.find_one({'_id': self.account.id})
                account = self.store.get_account(self.account.id)
                outbox = self.notifications.outbox.find_one({'_id': key})
                if change == 'binding': self.notifications.preferences.update_one({'_id': self.account.id}, {'$set': {'binding_id': 'c' * 32}})
                if change == 'unverified': self.notifications.preferences.update_one({'_id': self.account.id}, {'$set': {'verified': False}})
                if change == 'credentials': self.store.accounts.update_one({'_id': self.account.id}, {'$inc': {'credential_revision': 1}})
                if change == 'access': self.accounts.is_allowed = lambda _: False
                if change in ('pending', 'cancelled'): self.notifications.outbox.update_one({'_id': key}, {'$set': {'status': change}})
                if change == 'source': self.notifications.outbox.update_one({'_id': key}, {'$set': {'source.end': '2026-11-06 13:00:00'}})
                with self.assertRaises(AppError): service.submit(ticket)
                self.notifications.preferences.replace_one({'_id': self.account.id}, pref)
                self.store.accounts.replace_one({'_id': self.account.id}, account)
                self.notifications.outbox.replace_one({'_id': key}, outbox)
                self.accounts.is_allowed = lambda _: True
        self.assertEqual(self.school.cancels, [])

    def test_changed_live_record_never_cancels_sibling_or_modified_interval(self):
        service, ticket, _ = self.capability()
        self.school.records = [replace(self.record, end='2026-11-06 13:00:00'), self.sibling]
        self.assertEqual(service.submit(ticket)['state'], 'failed')
        self.assertEqual(self.school.cancels, [])

    def web_client(self):
        from local_app.web import create_app
        root = Path(self.temp.name)
        static = root / 'AL-FrontEnd/dist/build/h5/static'
        static.mkdir(parents=True)
        (static / 'mail-cancel.html').write_text('<!doctype html><title>取消预约</title>')
        app = create_app(replace(self.notifications.settings, root=root), self.accounts, self.store,
            self.accounts.catalog, lambda: {}, notifications=self.notifications, cancellations=self.service)
        client = app.test_client()
        origin = 'http://127.0.0.1:5003'
        csrf = client.get('/api/local/bootstrap', base_url=origin).json['csrf']
        return client, origin, {'Origin': origin, 'X-CSRF-Token': csrf}

    def test_anonymous_submit_requires_human_navigation_cookie_and_keeps_other_auth_guards(self):
        _, ticket, _ = self.capability()
        client, origin, headers = self.web_client()
        path = '/api/local/mail-cancellation/submit'
        denied = client.post(path, json={'ticket': ticket}, headers=headers, base_url=origin)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json['code'], 'mail_navigation_required')
        self.assertEqual(self.school.cancels, [])
        scanner = client.get('/static/mail-cancel.html', base_url=origin)
        self.assertEqual(scanner.status_code, 200)
        scanner.close()
        self.assertEqual(client.post(path, json={'ticket': ticket}, headers=headers, base_url=origin).status_code, 403)
        for protected_path in ('/api/local/accounts/' + self.account.id + '/cancellations/submit',
                               '/api/local/mail-cancellation/submit/extra'):
            self.assertEqual(client.post(protected_path, json={'ticket': ticket}, headers=headers, base_url=origin).status_code, 401)
        self.assertEqual(client.get(path, base_url=origin).status_code, 401)
        human = client.get('/static/mail-cancel.html', base_url=origin,
            headers={'Sec-Fetch-User': '?1', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        self.assertIn('HttpOnly', human.headers.get('Set-Cookie', ''))
        self.assertIn('SameSite=Strict', human.headers.get('Set-Cookie', ''))
        human.close()
        result = client.post(path, json={'ticket': ticket}, headers=headers, base_url=origin)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['state'], 'success')
        self.assertEqual(self.school.cancels, [self.record.id])
        status = client.post('/api/local/mail-cancellation/status', json={'ticket': ticket}, headers=headers, base_url=origin)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json['state'], 'success')
        self.assertEqual(self.db.campus_sessions.count_documents({}), 0)

    def test_anonymous_capability_routes_retain_origin_csrf_and_exact_body(self):
        _, ticket, _ = self.capability()
        client, origin, headers = self.web_client()
        path = '/api/local/mail-cancellation/status'
        for bad in ({'Origin': 'https://other.invalid', 'X-CSRF-Token': headers['X-CSRF-Token']}, {'Origin': origin}):
            self.assertEqual(client.post(path, json={'ticket': ticket}, headers=bad, base_url=origin).status_code, 403)
        self.assertEqual(client.post(path, json={'ticket': ticket, 'account_id': self.account.id}, headers=headers, base_url=origin).status_code, 400)
        self.assertEqual(client.post(path, json={'ticket': ticket}, headers=headers, base_url=origin).json['state'], 'not_started')
        self.assertEqual(self.school.cancels, [])


if __name__ == '__main__':
    unittest.main()
