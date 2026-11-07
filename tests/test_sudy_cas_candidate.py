"""New CAS structure, with generated keys and no school credentials or network."""
import hashlib
import unittest
from unittest.mock import patch

import requests
from Crypto.PublicKey import RSA
from local_app.models import AppError
from local_app.school import CampusClient
from support.cas_fixture import LOGIN, cas_steps
from support.school_fixture import Transport

SCRIPT_PATH = '/authserver/themes/sudy_njfu/js/login.js'
CSS_PATH = '/authserver/themes/sudy_njfu/css/pc/index.css'


def form(extra='', action='/authserver/login?service=fixture', widget=''):
    return f'''<html><form id="fm1" method="post" action="{action}">
    <input id="username" name="username" type="text">
    <input id="passwordShow" type="password">
    <input id="password" name="password" type="hidden" value="">
    <input name="execution" type="hidden" value="fixture-execution">
    <input name="encrypted" type="hidden" value="true">
    <input name="_eventId" type="hidden" value="submit">
    <input name="loginType" type="hidden" value="fixture-type">{extra}
    </form><script src="{SCRIPT_PATH}"></script>{widget}</html>'''


class SudyCasCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSA.generate(1024)
        cls.modulus = '00' + format(cls.key.n, 'x')
        cls.script = ('RSAUtils.getKeyPair("010001", "", "' + cls.modulus + '");\n') * 2
        cls.css = '.modal-overlay { display: none; }'
        cls.digest = hashlib.sha256(cls.script.encode()).hexdigest()
        cls.key_digest = hashlib.sha256(cls.modulus.lower().encode()).hexdigest()
        cls.css_digest = hashlib.sha256(cls.css.encode()).hexdigest()

    def steps(self, page=None, script=None, css=None):
        old = cas_steps()
        old[2] = ('GET', LOGIN, page if page is not None else form(), 200, {})
        old[3] = ('GET', SCRIPT_PATH, script if script is not None else self.script, 200, {})
        old[4] = ('POST', '/authserver/login?service=fixture', '', 302,
                  {'Location': 'http://libseat.njfu.edu.cn/authcenter/doAuth/fixture?ticket=fixture-ticket'})
        if css is not None:
            old.insert(4, ('GET', CSS_PATH, css, 200, {}))
        return old

    def pinned(self):
        return patch.multiple('local_app.cas', NEW_LOGIN_SCRIPT_SHA256=self.digest,
                              NEW_LOGIN_KEY_SHA256=self.key_digest,
                              NEW_LOGIN_CSS_SHA256=self.css_digest, create=True)

    def test_new_form_uses_verified_rsa_and_one_fake_login(self):
        transport = Transport(self.steps())
        with self.pinned():
            identity = CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(identity.student_id, 'fixture01')
        posts = [x for x in transport.calls if x[0] == 'POST']
        self.assertEqual(len(posts), 1)
        fields = posts[0][2]['data']
        self.assertEqual(fields['username'], 'fixture01')
        self.assertEqual(fields['execution'], 'fixture-execution')
        self.assertNotIn('fixture-password', str(fields))
        decrypted = pow(int(fields['password'], 16), self.key.d, self.key.n)
        chunk_size = 2 * ((self.key.n.bit_length() - 1) // 16)
        self.assertEqual(decrypted.to_bytes(chunk_size, 'little').rstrip(b'\x00'), b'fixture-password')

    def test_changed_script_is_not_trusted_and_never_posts_password(self):
        transport = Transport(self.steps(script=self.script + '// changed'))
        with self.pinned(), self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'school_protocol')
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_auth_form_with_explicit_challenge_never_posts_password(self):
        transport = Transport(self.steps(page=form('<input id="otpauthcode" name="otpauthcode">')))
        with self.pinned(), self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'verification_required')
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_unknown_page_is_not_reported_as_proven_captcha(self):
        steps = cas_steps()[:3]
        steps[2] = ('GET', LOGIN, '<html><title>Unavailable</title></html>', 200, {})
        transport = Transport(steps)
        with self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'school_protocol')
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_explicit_unknown_page_challenge_still_requires_verification(self):
        steps = cas_steps()[:3]
        steps[2] = ('GET', LOGIN, '<html><input id="captchaAnswer" name="captcha"></html>', 200, {})
        transport = Transport(steps)
        with self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'verification_required')
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_default_hidden_slider_requires_trusted_css(self):
        widget = f'<link rel="stylesheet" href="{CSS_PATH}"><div id="modalOverlay" class="modal-overlay"><div id="captcha2"></div></div>'
        transport = Transport(self.steps(page=form(widget=widget), css=self.css))
        with self.pinned():
            self.assertEqual(CampusClient('fixture01', 'fixture-password', transport=transport).login().student_id, 'fixture01')
        self.assertEqual(sum(x[0] == 'POST' for x in transport.calls), 1)

    def test_active_slider_and_changed_css_never_post_password(self):
        for style, css in [('display: block', self.css), ('', self.css + '/* changed */')]:
            with self.subTest(style=style):
                widget = f'<link rel="stylesheet" href="{CSS_PATH}"><div id="modalOverlay" class="modal-overlay" style="{style}"><div id="captcha2"></div></div>'
                transport = Transport(self.steps(page=form(widget=widget), css=css))
                with self.pinned(), self.assertRaises(AppError):
                    CampusClient('fixture01', 'fixture-password', transport=transport).login()
                self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_active_overlay_class_never_posts_password(self):
        widget = f'<link rel="stylesheet" href="{CSS_PATH}"><div id="modalOverlay" class="modal-overlay active"><div id="captcha2"></div></div>'
        transport = Transport(self.steps(page=form(widget=widget), css=self.css))
        with self.pinned(), self.assertRaises(AppError):
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_disabled_password_control_and_new_challenge_id_never_post(self):
        pages = [form().replace('id="passwordShow" type="password"', 'id="passwordShow" type="password" disabled'),
                 form('<input id="captchaAnswer" type="text">')]
        for page in pages:
            with self.subTest(page=page):
                transport = Transport(self.steps(page=page))
                with self.pinned(), self.assertRaises(AppError):
                    CampusClient('fixture01', 'fixture-password', transport=transport).login()
                self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_changed_service_query_never_posts_password(self):
        steps = self.steps(page=form(action='/authserver/login?service=other'))
        steps[4] = ('POST', '/authserver/login?service=other', '', 302,
                    {'Location': 'http://libseat.njfu.edu.cn/authcenter/doAuth/fixture?ticket=fixture-ticket'})
        transport = Transport(steps)
        with self.pinned(), self.assertRaises(AppError):
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_untrusted_new_form_action_never_posts_password(self):
        transport = Transport(self.steps(page=form(action='https://example.com/authserver/login')))
        with self.pinned(), self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'school_protocol')
        self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_unrepresentable_password_never_posts(self):
        for password in ('fixture-密码', 'a' * 256):
            with self.subTest(length=len(password)):
                transport = Transport(self.steps())
                with self.pinned(), self.assertRaises(AppError) as caught:
                    CampusClient('fixture01', password, transport=transport).login()
                self.assertEqual(caught.exception.code, 'school_protocol')
                self.assertFalse(any(x[0] == 'POST' for x in transport.calls))

    def test_lost_login_response_is_not_a_retryable_network_error(self):
        steps = self.steps()
        steps[4] = ('POST', '/authserver/login?service=fixture', requests.ConnectionError('fixture drop'), 200, {})
        transport = Transport(steps)
        with self.pinned(), self.assertRaises(AppError) as caught:
            CampusClient('fixture01', 'fixture-password', transport=transport).login()
        self.assertEqual(caught.exception.code, 'login_uncertain')
        self.assertEqual(sum(x[0] == 'POST' for x in transport.calls), 1)

    def test_explicit_sms_requirements_outside_form_never_post_password(self):
        for message in ('<div role="alert">请先完成手机短信验证</div>',
                        '<div>请先完成手机短信验证</div>',
                        '<div><span>请先完成</span><span>手机短信验证</span></div>',
                        '<script>window.needCaptcha = true;</script>'):
            with self.subTest(message=message):
                transport = Transport(self.steps(page=form() + message))
                with self.pinned(), self.assertRaises(AppError) as caught:
                    CampusClient('fixture01', 'fixture-password', transport=transport).login()
                self.assertEqual(caught.exception.code, 'verification_required')
                self.assertFalse(any(item[0] == 'POST' for item in transport.calls))
