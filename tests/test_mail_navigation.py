"""Navigation grants and gateway routing, using in-memory clients only."""
import importlib
import hashlib
import hmac
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'AL-BackEnd')]
from deploy.inbox_gateway import create_gateway, Reply
from local_app.models import AppError

HUMAN = {'Sec-Fetch-User': '?1', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
WEBKIT = {key: value for key, value in HUMAN.items() if key != 'Sec-Fetch-User'}
COOKIE = 'njfu_mail_cancel_navigation'


def navigation(test):
    test.assertTrue((ROOT / 'AL-BackEnd/local_app/mail_navigation.py').is_file(), 'Navigation grant helper is missing')
    return importlib.import_module('local_app.mail_navigation')


class NavigationGrantTests(unittest.TestCase):
    def test_signed_random_grants_expire_after_sixty_seconds_and_reject_other_keys(self):
        nav = navigation(self); value = nav.issue_navigation_grant('fixture-key', now=1000)
        self.assertIsInstance(value, str); self.assertLessEqual(len(value), 256)
        self.assertNotEqual(value, nav.issue_navigation_grant('fixture-key', now=1000))
        self.assertTrue(nav.valid_navigation_grant(b'fixture-key', value, now=1000))
        self.assertTrue(nav.valid_navigation_grant('fixture-key', value, now=1059.999))
        for key, at in (('fixture-key', 1060), ('fixture-key', 999), ('another-key', 1000)):
            with self.subTest(key=key, at=at): self.assertFalse(nav.valid_navigation_grant(key, value, now=at))

    def test_tampering_invalid_types_and_bounds_fail_closed(self):
        nav = navigation(self); value = nav.issue_navigation_grant(b'fixture-key', now=1000)
        for malformed in (None, True, 1, b'bytes', {}, '', 'x' * 4097, value + '.extra', value[:-1], value[:-1] + ('0' if value[-1] != '0' else '1')):
            with self.subTest(value_type=type(malformed).__name__): self.assertFalse(nav.valid_navigation_grant(b'fixture-key', malformed, now=1000))
        for key in (None, True, 1, {}, b'', '', b'x' * 4097, 'x' * 4097):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaises(ValueError): nav.issue_navigation_grant(key, now=1000)
                self.assertFalse(nav.valid_navigation_grant(key, value, now=1000))
        for at in (True, '1000', float('nan'), float('inf'), -1, 10**20):
            with self.subTest(clock_type=type(at).__name__):
                with self.assertRaises(ValueError): nav.issue_navigation_grant('fixture-key', now=at)
                self.assertFalse(nav.valid_navigation_grant('fixture-key', value, now=at))

    def test_another_hmac_purpose_cannot_sign_a_navigation_grant(self):
        nav = navigation(self); payload = nav.issue_navigation_grant(b'fixture-key', now=1000).rsplit('.', 1)[0]
        for purpose in (b'', b'njfu:mail-cancel:ticket:v1\0'):
            signature = hmac.new(b'fixture-key', purpose + payload.encode('ascii'), hashlib.sha256).hexdigest()
            self.assertFalse(nav.valid_navigation_grant(b'fixture-key', payload + '.' + signature, now=1000))

    def test_top_level_navigation_required_but_webkit_user_header_is_optional(self):
        nav = navigation(self)
        self.assertTrue(nav.is_user_navigation(HUMAN)); self.assertTrue(nav.is_user_navigation({k.lower(): v for k, v in HUMAN.items()}))
        self.assertTrue(nav.is_user_navigation(WEBKIT))
        for name in ('Sec-Fetch-Mode', 'Sec-Fetch-Dest'):
            self.assertFalse(nav.is_user_navigation({k: v for k, v in HUMAN.items() if k != name}))
        for headers in ({}, None, {'Cookie': 'logged-in'}, HUMAN | {'Sec-Fetch-User': '?0'}, HUMAN | {'Sec-Fetch-Mode': 'cors'},
                HUMAN | {'Sec-Fetch-Dest': 'iframe'}, HUMAN | {'Sec-Fetch-User': True}, HUMAN | {'sec-fetch-user': '?0'},
                HUMAN | {'Sec-Fetch-User': ''}, WEBKIT | {'Sec-Fetch-Mode': 'no-cors'},
                WEBKIT | {'Sec-Fetch-Dest': 'image'}, WEBKIT | {'Sec-Fetch-Mode': True}):
            with self.subTest(headers=headers): self.assertFalse(nav.is_user_navigation(headers))

    def test_explicit_preview_purpose_never_grants_even_with_navigation_metadata(self):
        nav = navigation(self)
        for name in ('Purpose', 'Sec-Purpose', 'X-Purpose', 'X-Moz'):
            for value in ('prefetch', 'preview', 'prerender', 'prefetch;prerender', 'unknown', ' ', True, None):
                for metadata in (HUMAN, WEBKIT):
                    with self.subTest(header=name, value=value, metadata=metadata):
                        self.assertFalse(nav.is_user_navigation(metadata | {name: value}))
            self.assertTrue(nav.is_user_navigation(WEBKIT | {name: ''}))

    def test_duplicate_or_malformed_navigation_headers_fail_closed(self):
        nav = navigation(self)
        class Headers:
            def __init__(self, entries): self.entries = entries
            def items(self): return self.entries
        for extra in (('sec-fetch-mode', 'navigate'), ('sec-fetch-dest', 'document'),
                      ('sec-fetch-user', '?1'), ('Purpose', ''), (None, '?1')):
            initial = list(HUMAN.items()) + ([('purpose', '')] if extra[0] == 'Purpose' else [])
            with self.subTest(extra=extra):
                self.assertFalse(nav.is_user_navigation(Headers(initial + [extra])))


class FakeUpstream:
    def __init__(self): self.calls, self.probes = [], []
    def probe(self, source): self.probes.append(source); return True
    def forward(self, method, path, source, cookie='', data=None):
        self.calls.append((method, path, source, cookie, data))
        return Reply(200, b'{"user":null}' if path.endswith('/bootstrap') else b'{"state":"success"}')


class MailNavigationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-mail-navigation-test-'); self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name); (folder / 'static').mkdir()
        (folder / 'static/mail-cancel.html').write_text('<!doctype html><p>Fixture landing</p>')
        (folder / 'mail-cancel.html').write_text('<!doctype html><p>Different resource</p>')
        self.config = {'origin': 'https://mail-fixture.example:45581', 'relay_token': 'r' * 64, 'worker_token': 'w' * 64, 'static_root': self.temp.name}
        self.store = Mock(); self.store.current.return_value = None; self.store.summary.return_value = {'executor_online': True}
        self.upstream = FakeUpstream(); self.app = create_gateway(self.config, self.store, self.upstream); self.client = self.app.test_client()
        self.origin = self.config['origin']; self.headers = {'X-NJFU-Relay-HTTPS': '1', 'X-NJFU-Relay-Token': 'r' * 64, 'X-NJFU-Client-IP': '198.51.100.2'}
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin, headers=self.headers).json['csrf']
        self.upstream.calls.clear(); self.upstream.probes.clear(); self.store.reset_mock()

    def get(self, path, **headers): return self.client.get(path, base_url=self.origin, headers=self.headers | headers)
    def post(self, suffix, **headers):
        return self.client.post('/api/local/' + suffix, base_url=self.origin, json={'ticket': 'fixture-capability'},
            headers=self.headers | {'Origin': self.origin, 'X-CSRF-Token': self.csrf} | headers)

    def test_scanner_get_cannot_grant_and_clears_any_previous_cookie(self):
        for headers in ({}, HUMAN | {'Purpose': 'prefetch'}, WEBKIT | {'Sec-Purpose': 'prefetch;prerender'},
                        HUMAN | {'Sec-Fetch-User': '?0'}):
            self.client.set_cookie(COOKIE, 'old-grant', domain='mail-fixture.example')
            response = self.get('/static/mail-cancel.html', **headers)
            self.assertEqual(response.status_code, 200); self.assertIsNone(self.client.get_cookie(COOKIE, domain='mail-fixture.example'))
            self.assertIn('Max-Age=0', response.headers.get('Set-Cookie', ''))
            response.close()
        self.assertEqual(self.upstream.calls, [])

    def test_human_navigation_grants_only_exact_landing_and_never_calls_cancellation(self):
        nav = navigation(self); response = self.get('/static/mail-cancel.html', **HUMAN)
        cookie = self.client.get_cookie(COOKIE, domain='mail-fixture.example')
        self.assertIsNotNone(cookie); self.assertTrue(nav.valid_navigation_grant('r' * 64, cookie.value))
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=60'): self.assertIn(flag, response.headers['Set-Cookie'])
        other = self.get('/mail-cancel.html', **HUMAN); self.assertNotIn(COOKIE, other.headers.get('Set-Cookie', ''))
        response.close(); other.close()
        self.assertEqual(self.upstream.calls, []); self.assertEqual(self.upstream.probes, [])

    def test_webkit_navigation_grants_without_user_header_and_capability_routes_stay_scoped(self):
        response = self.get('/static/mail-cancel.html', **WEBKIT)
        cookie = self.client.get_cookie(COOKIE, domain='mail-fixture.example')
        self.assertIsNotNone(cookie)
        self.assertTrue(navigation(self).valid_navigation_grant('r' * 64, cookie.value))
        self.assertEqual(self.upstream.calls, [])
        response.close()
        self.client.set_cookie('njfu_inbox_session', 'stale-offline', domain='mail-fixture.example')
        response = self.post('mail-cancellation/submit')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.upstream.calls), 1)
        self.assertIn(COOKIE + '=', self.upstream.calls[0][3])
        self.assertEqual(self.post('accounts/a/query').status_code, 401)

    def test_head_request_cannot_grant_navigation_cookie(self):
        response = self.client.head('/static/mail-cancel.html', base_url=self.origin,
                                    headers=self.headers | WEBKIT)
        self.assertNotIn(COOKIE + '=', response.headers.get('Set-Cookie', ''))
        self.assertEqual(self.upstream.calls, [])
        response.close()

    def test_stale_offline_cookie_forwards_only_two_exact_post_capability_routes(self):
        self.client.set_cookie('njfu_inbox_session', 'stale-offline', domain='mail-fixture.example')
        for suffix in ('mail-cancellation/submit', 'mail-cancellation/status'):
            self.client.set_cookie(COOKIE, 'navigation-grant', domain='mail-fixture.example')
            response = self.post(suffix); self.assertEqual(response.status_code, 200)
            self.assertEqual(self.upstream.calls[-1][:3], ('POST', '/api/local/' + suffix, '198.51.100.2'))
            self.assertIn(COOKIE + '=navigation-grant', self.upstream.calls[-1][3])
            if suffix.endswith('submit'): self.assertIn(COOKIE + '=;', response.headers.get('Set-Cookie', ''))
        self.store.current.assert_not_called(); self.assertEqual(self.upstream.probes, [])
        before = len(self.upstream.calls)
        for suffix in ('accounts/a/query', 'mail-cancellation/submit/extra', 'mail-cancellation/status/extra', 'mail-cancellation/unknown'):
            self.assertEqual(self.post(suffix).status_code, 401)
        self.assertEqual(self.get('/api/local/mail-cancellation/submit').status_code, 401)
        self.assertEqual(self.get('/api/local/mail-cancellation/status').status_code, 401)
        self.assertEqual(len(self.upstream.calls), before)

    def test_capability_routes_keep_origin_csrf_relay_checks_and_clear_submit_cookie(self):
        for headers in ({'Origin': 'https://evil.example'}, {'X-CSRF-Token': 'wrong'}, {'X-NJFU-Relay-Token': 'wrong'}):
            self.client.set_cookie(COOKIE, 'navigation-grant', domain='mail-fixture.example')
            response = self.post('mail-cancellation/submit', **headers)
            self.assertEqual(response.status_code, 403); self.assertIn('Max-Age=0', response.headers.get('Set-Cookie', ''))
        self.assertEqual(self.upstream.calls, [])

    def test_upstream_failure_clears_cookie_without_retry(self):
        def failed(*args): self.upstream.calls.append(args); raise AppError('executor_unavailable', 'Fixture unavailable', 503)
        self.upstream.forward = failed
        self.client.set_cookie('njfu_inbox_session', 'stale-offline', domain='mail-fixture.example')
        self.client.set_cookie(COOKIE, 'navigation-grant', domain='mail-fixture.example')
        response = self.post('mail-cancellation/submit')
        self.assertEqual(response.status_code, 503); self.assertEqual(len(self.upstream.calls), 1)
        self.assertIn('Max-Age=0', response.headers.get('Set-Cookie', ''))


if __name__ == '__main__': unittest.main()
