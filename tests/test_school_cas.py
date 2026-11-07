import base64
import sys
import unittest
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from support.school_fixture import Transport, query_step
from support.cas_fixture import cas_steps, form, SALT, CALLBACK, TOKEN
from local_app.models import AppError
from local_app.school import CampusClient


class CasTests(unittest.TestCase):
    def test_official_flow_encrypts_password_and_reads_verified_identity(self):
        transport = Transport(cas_steps()+[query_step()])
        client = CampusClient('fixture01', 'fixture-password', transport=transport)
        self.assertEqual(client.login().student_id, 'fixture01')
        posts = [call for call in transport.calls if call[0] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], 'https://uia.njfu.edu.cn/authserver/login')
        payload = posts[0][2]['data']
        self.assertEqual(payload['execution'], 'fixture-dynamic-execution')
        self.assertEqual(payload['lt'], 'fixture-lt')
        decoded = unpad(AES.new(SALT.encode(), AES.MODE_CBC, bytes(16)).decrypt(
            base64.b64decode(payload['password'])), AES.block_size)
        self.assertEqual(decoded[64:], b'fixture-password')
        self.assertNotIn('fixture-password', str(payload))
        self.assertTrue(all(call[1].startswith('https://') for call in transport.calls))
        from datetime import date
        client.query(date.today(), date.today())
        self.assertEqual(transport.calls[-1][2]['headers']['token'], 'fixture-token-fixture01')

    def test_required_captcha_stops_before_password_post(self):
        steps = cas_steps()[:4]
        steps[3] = ('GET', '/authserver/needCaptcha.html', 'true::::fixture-public-salt', 200, {})
        transport = Transport(steps)
        with self.assertRaises(AppError) as result:
            CampusClient('fixture01', 'pass', transport=transport).login()
        self.assertEqual(result.exception.code, 'verification_required')
        self.assertFalse(any(call[0] == 'POST' for call in transport.calls))

    def test_invalid_form_and_untrusted_action_never_send_password(self):
        for html in (form(salt='bad'), form(action='https://example.com/authserver/login')):
            steps = cas_steps()[:3];steps[2] = ('GET', steps[2][1], html, 200, {})
            transport = Transport(steps)
            with self.subTest(html=html), self.assertRaises(AppError):
                CampusClient('fixture01', 'pass', transport=transport).login()
            self.assertFalse(any(call[0] == 'POST' for call in transport.calls))

    def test_post_is_not_repeated_or_forwarded_to_redirect_target(self):
        steps = cas_steps()[:5]
        steps[4] = ('POST', '/authserver/login', '', 307, {'Location': 'https://example.com/'})
        transport = Transport(steps)
        with self.assertRaises(AppError):CampusClient('fixture01', 'pass', transport=transport).login()
        self.assertEqual(len([call for call in transport.calls if call[0]=='POST']), 1)

    def test_password_form_or_mfa_response_is_not_reported_as_success(self):
        for html in (form(), '<html>二次验证</html>'):
            steps = cas_steps()[:5]
            steps[4] = ('POST', '/authserver/login', html, 200, {})
            transport = Transport(steps)
            with self.subTest(html=html):
                with self.assertRaises(AppError) as result:
                    CampusClient('fixture01', 'pass', transport=transport).login()
                self.assertIn(result.exception.code, ('login_failed', 'verification_required'))
                self.assertEqual(len([call for call in transport.calls if call[0]=='POST']), 1)

    def test_cross_host_redirect_does_not_receive_school_token(self):
        client = CampusClient('fixture01', 'pass', transport=Transport(cas_steps()))
        client.login()
        client.http.steps = [('GET', '/ic-web/auth/userInfo', '', 302,
                             {'Location': 'https://uia.njfu.edu.cn/authserver/login'}),
                             ('GET', '/authserver/login', form(), 200, {})]
        client._request('GET', '/ic-web/auth/userInfo')
        self.assertNotIn('token', client.http.calls[-1][2].get('headers', {}))

    def test_callback_literal_is_followed_without_executing_javascript(self):
        transport=Transport(cas_steps())
        self.assertEqual(CampusClient('fixture01','pass',transport=transport).login().student_id,'fixture01')
        token_calls=[call for call in transport.calls if '/auth/token?' in call[1]]
        self.assertEqual(len(token_calls),1)
        self.assertEqual(token_calls[0][0],'GET')

    def test_official_session_cookie_is_preserved_for_record_queries(self):
        transport=Transport(cas_steps()+[query_step()])
        client=CampusClient('fixture01','pass',transport=transport);client.login()
        self.assertEqual(transport.cookies.get('ic-cookie'),'school-session-fixture01')
        from datetime import date
        self.assertEqual(client.query(date.today(),date.today()),[])

    def test_callback_rejects_other_host_state_path_and_executable_expression(self):
        targets=[TOKEN.replace('libseat.njfu.edu.cn','example.com'),
            TOKEN.replace('uuid=fixture-state','uuid=other-state'),
            TOKEN.replace('/ic-web//auth/token','/ic-web/reserve')]
        scripts=['window.location.href = "'+url+'";' for url in targets]
        scripts.append('window.location.href = obtainToken();')
        for script in scripts:
            steps=cas_steps()[:6]
            steps[5]=('GET',CALLBACK.replace('http:','https:'),'<html><script>'+script+'</script></html>',200,{})
            transport=Transport(steps)
            with self.subTest(script=script):
                with self.assertRaises(AppError):CampusClient('fixture01','pass',transport=transport).login()
                self.assertEqual(len(transport.calls),6)


if __name__ == '__main__':unittest.main()
