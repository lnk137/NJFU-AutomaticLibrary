"""Synthetic CAS exchange based on the school's public pages; no real credentials."""
from urllib.parse import urlencode
TOKEN = 'https://libseat.njfu.edu.cn/ic-web//auth/token?uuid=fixture-state&uniToken=fixture-token&extInfo=fixture-state'
ENTRY = 'https://libseat.njfu.edu.cn/authcenter/toLoginPage?'+urlencode({
    'redirectUrl':'https://libseat.njfu.edu.cn/ic-web//auth/token?uuid=fixture-state','extInfo':'fixture-state'})
LOGIN = 'https://uia.njfu.edu.cn/authserver/login?service=fixture'
CALLBACK = 'http://libseat.njfu.edu.cn/authcenter/doAuth/fixture?ticket=fixture-ticket'
SALT = 'fixture-salt-key'


def form(action='/authserver/login', salt=SALT):
    return f'''<form id="casLoginForm" action="{action}" method="post">
      <input name="username" id="username" type="text">
      <input id="password" type="password">
      <input name="password" id="passwordEncrypt" type="text">
      <input name="lt" type="hidden" value="fixture-lt">
      <input name="execution" type="hidden" value="fixture-dynamic-execution">
      <input name="dllt" type="hidden" value="userNamePasswordLogin">
      <input name="_eventId" type="hidden" value="submit">
      <input name="rmShown" type="hidden" value="1">
      <input id="pwdDefaultEncryptSalt" type="hidden" value="{salt}">
    </form>'''


def cas_steps(student='fixture01'):
    return [
        ('GET', '/ic-web/auth/address', {'code': 0, 'data': ENTRY}, 200, {}),
        ('GET', ENTRY, '', 302, {'Location': LOGIN}),
        ('GET', LOGIN, form(), 200, {}),
        ('GET', '/authserver/needCaptcha.html', 'false', 200, {}),
        ('POST', '/authserver/login', '', 302, {'Location': CALLBACK}),
        ('GET', CALLBACK.replace('http:', 'https:'), '<html><script>window.location.href = "'+TOKEN+'";</script></html>', 200, {}),
        ('GET', TOKEN, '', 302, {'Location': 'https://libseat.njfu.edu.cn/',
            'Set-Cookie':'ic-cookie=school-session-'+student+'; Path=/; Secure'}),
        ('GET', 'https://libseat.njfu.edu.cn/', '<html>Information Commons</html>', 200, {}),
        ('GET', '/ic-web/auth/userInfo', {'code': 0, 'data': {
            'logonName': student, 'trueName': 'Fixture', 'accNo': 123,
            'cardNo': 'fixture-card', 'token': 'fixture-token-'+student}}, 200, {})]
