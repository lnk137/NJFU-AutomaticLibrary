"""Exact run reads preserve ownership, redaction and offline cache boundaries."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import unittest
from unittest.mock import patch

import test_local_api as local_fixture
import test_inbox_gateway as gateway_fixture
from local_app.config import load_settings
from local_app.web import create_app
from support.auth_fixture import grant

PUBLIC_FIELDS = {'id', 'account_id', 'target_date', 'status', 'spec', 'message',
                 'updated_at', 'result', 'attempts', 'scheduler_timings'}


class LocalRunDetailTests(unittest.TestCase):
    setUpClass = classmethod(local_fixture.ApiTests.setUpClass.__func__)
    tearDownClass = classmethod(local_fixture.ApiTests.tearDownClass.__func__)
    tearDown = local_fixture.ApiTests.tearDown

    def setUp(self):
        local_fixture.ApiTests.setUp(self)
        grant(self.auth, ['fixture01', 'fixture02'])
        self.accounts = [self.service.verify_and_save(student, 'pass') for student in ('fixture01', 'fixture02')]
        self.rows = []
        for account in self.accounts:
            row = {'_id': account.id + ':2026-11-08/segment-1', 'account_id': account.id,
                   'target_date': '2026-11-08', 'status': 'uncertain', 'created': 1,
                   'spec': {'account_id': account.id, 'begin': '09:00', 'end': '12:00'},
                   'result': {'status': 'uncertain', 'records': []}, 'attempts': [],
                   'native_binding': {'grant': 'must-not-leak'}, 'secret': 'must-not-leak',
                   'private_unknown': 'must-not-leak'}
            self.store.runs.insert_one(deepcopy(row)); self.rows.append(row)

    def student(self, account, client=None, domain='127.0.0.1'):
        (client or self.client).set_cookie('njfu_autolib_session',
            self.auth._issue('student', account_id=account.id, student_id=account.student_id), domain=domain)

    def detail(self, key, client=None):
        return (client or self.client).get('/api/local/runs/' + key, base_url=self.origin)

    def test_local_root_gets_the_same_whitelisted_projection_as_history(self):
        before = list(self.store.runs.find())
        with patch.object(self.service, 'client', side_effect=AssertionError('No school access')):
            response = self.detail(self.rows[0]['_id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json), {'run'})
        self.assertEqual(set(response.json['run']), PUBLIC_FIELDS)
        listed = asdict(self.store.list_runs(self.accounts[0].id, None))['items'][0]
        self.assertEqual(response.json['run'], listed)
        self.assertNotIn('must-not-leak', response.get_data(as_text=True))
        self.assertEqual(list(self.store.runs.find()), before)

    def test_student_reads_own_exact_run_but_other_and_missing_are_404(self):
        self.student(self.accounts[0])
        self.assertEqual(self.detail(self.rows[0]['_id']).status_code, 200)
        for key in (self.rows[1]['_id'], 'missing/run'):
            response = self.detail(key)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('run', response.json)

    def test_anonymous_read_requires_authentication(self):
        client = self.app.test_client()
        self.assertEqual(self.detail(self.rows[0]['_id'], client).status_code, 401)

    def test_public_root_stays_denied_while_public_student_can_read_own_run(self):
        origin = 'https://detail-fixture.example:45581'
        settings = load_settings(Path(__file__).resolve().parents[1],
                                 {'public_origin': origin, 'relay_token': 'f' * 64})
        app = create_app(settings, self.service, self.store, self.catalog, lambda: {}, auth=self.auth,
                         reconcile=lambda _: self.fail('GET must never reconcile'))
        client = app.test_client()
        headers = {'X-NJFU-Relay-HTTPS': '1', 'X-NJFU-Relay-Token': 'f' * 64,
                   'X-NJFU-Client-IP': '198.51.100.7'}
        path = '/api/local/runs/' + self.rows[0]['_id']
        client.set_cookie('njfu_autolib_session', self.auth._issue('root'), domain='detail-fixture.example')
        self.assertEqual(client.get(path, base_url=origin, headers=headers).status_code, 403)
        self.student(self.accounts[0], client, 'detail-fixture.example')
        self.assertEqual(client.get(path, base_url=origin, headers=headers).status_code, 200)


class OfflineRunDetailTests(unittest.TestCase):
    tearDown = gateway_fixture.GatewayTests.tearDown
    get = gateway_fixture.GatewayTests.get
    post = gateway_fixture.GatewayTests.post
    login = gateway_fixture.GatewayTests.login

    def setUp(self):
        gateway_fixture.GatewayTests.setUp(self)
        packet = gateway_fixture.snapshot(2)
        self.run = {'id': 'account-a:2026-11-08/segment-1', 'account_id': 'account-a',
                    'target_date': '2026-11-08', 'status': 'submitting', 'spec': {},
                    'native_binding': {'grant': 'must-not-leak'}, 'secret': 'must-not-leak',
                    'private_unknown': 'must-not-leak'}
        packet['accounts'][0]['runs'] = {'items': [self.run], 'counts': {'total': 101}, 'next_cursor': 'local-cursor'}
        other = deepcopy(packet['accounts'][0]); other['student_id'] = '2310000002'
        other['account'] = other['account'] | {'id': 'account-b', 'student_id': '2310000002'}
        other['runs'] = {'items': [self.run | {'id': 'other/run', 'account_id': 'account-b'}],
                         'counts': {'total': 1}, 'next_cursor': None}
        packet['accounts'].append(other)
        self.store.publish(packet)
        self.assertEqual(self.login().status_code, 200)

    def test_offline_own_run_uses_only_cached_whitelisted_data(self):
        with (patch.object(self.upstream, 'probe', side_effect=AssertionError('No upstream probe')),
              patch.object(self.upstream, 'forward', side_effect=AssertionError('No upstream forwarding'))):
            response = self.get('/runs/' + self.run['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json), {'run'})
        self.assertEqual(set(response.json['run']), PUBLIC_FIELDS)
        self.assertEqual(response.json['run']['status'], 'submitting')
        self.assertNotIn('must-not-leak', response.get_data(as_text=True))

    def test_cache_miss_or_other_account_is_404_without_forwarding(self):
        with patch.object(self.upstream, 'forward', side_effect=AssertionError('No upstream forwarding')):
            for key in ('not-in-export', 'other/run'):
                response = self.get('/runs/' + key)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('run', response.json)

    def test_expired_offline_session_does_not_fall_back_to_online_detail(self):
        packet = gateway_fixture.snapshot(3)
        packet['accounts'][0]['auth']['generation'] = '2:1:1'
        self.store.publish(packet)
        with patch.object(self.upstream, 'forward', side_effect=AssertionError('No fallback after revocation')):
            self.assertEqual(self.get('/runs/' + self.run['id']).status_code, 401)

    def test_invalid_cross_account_cache_stays_unavailable(self):
        packet = gateway_fixture.snapshot(3)
        packet['accounts'][0]['runs']['items'] = [self.run | {'account_id': 'account-b'}]
        self.store.publish(packet)
        self.assertEqual(self.get('/runs/' + self.run['id']).status_code, 503)
