"""Parse the official password form; never log form values or authentication pages."""
import base64
import ast
import hashlib
import re
import secrets
import string
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup, Comment
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from .models import AppError

NEW_LOGIN_SCRIPT = 'https://uia.njfu.edu.cn/authserver/themes/sudy_njfu/js/login.js'
NEW_LOGIN_CSS = 'https://uia.njfu.edu.cn/authserver/themes/sudy_njfu/css/pc/index.css'
NEW_LOGIN_SCRIPT_SHA256 = 'fbba59aab5c4c185b86b9f2fb728b4b64007bceee27497b5076877102180c389'
NEW_LOGIN_CSS_SHA256 = '9728c2e3ae428b607e3880ec97cfd91acb716eda138305c1c0527c4aaa055a08'
NEW_LOGIN_KEY_SHA256 = 'ae5324626b47e5cd740c958f17fea62e445faaf0fe027343f8d148177c7afb46'


def is_new_password_page(response):
    return BeautifulSoup(response.text, 'html.parser').find('form', id='fm1') is not None


def new_password_form(response):
    """Accept the observed form only; never treat unknown challenges as optional."""
    protocol = AppError('school_protocol', '学校统一认证页面结构变化，未发送密码', 502)
    required = AppError('verification_required', '学校统一认证需要人工验证，未发送密码', 401)
    page = urlsplit(response.url)
    if (page.scheme != 'https' or page.hostname != 'uia.njfu.edu.cn' or
            page.port not in (None, 443) or page.username or page.fragment or
            page.path.split(';', 1)[0] != '/authserver/login'):
        raise protocol
    document = BeautifulSoup(response.text, 'html.parser')
    forms = document.find_all('form', id='fm1')
    if len(forms) != 1 or document.find('form', id='casLoginForm'):
        raise protocol
    form = forms[0]
    challenge_words = r'(?:验证码|二次验证|人脸识别|短信验证|手机验证|扫码验证|滑块验证|captcha|verification\s+code)'
    explicit_challenge = re.compile(
        r'(?:请先|请完成|请进行|需要|需先|必须|first|must|complete|required).{0,24}' + challenge_words
        + r'|' + challenge_words + r'.{0,24}(?:必填|需要|必须|required)', re.I)
    visible_parts = [str(text).strip() for text in document.find_all(string=True)
                     if not isinstance(text, Comment) and text.find_parent(['script', 'style']) is None]
    # Schools may split an instruction across inline elements; inspect the
    # joined text so an explicit challenge is still noticed in that case.
    if explicit_challenge.search(' '.join(visible_parts)):
        raise required
    for alert in document.find_all(attrs={'role': re.compile(r'^alert$', re.I)}):
        if re.search(challenge_words, alert.get_text(' ', strip=True), re.I):
            raise required
    # Do not infer that an inline challenge is inactive without executing the
    # page. The currently observed school page has no such inline script.
    if any(re.search(r'captcha|otp|(?:two|multi)[_-]?factor|(?:need|require)[_-]?verif',
                     tag.get_text(), re.I) for tag in document.find_all('script', src=False)):
        raise required
    if document.find(id='otpauthcode'):
        raise required
    if any(re.search(r'captcha|verify|otp', str(item.get(key, '')), re.I)
           for item in form.find_all('input') for key in ('id', 'name')):
        raise required
    action = urljoin(response.url, form.get('action', ''))
    target = urlsplit(action)
    if (form.get('method', '').lower() != 'post' or target.scheme != 'https' or
            target.hostname != 'uia.njfu.edu.cn' or target.port not in (None, 443) or
            target.username or target.fragment or target.path.split(';', 1)[0] != '/authserver/login' or
            target.query != page.query):
        raise protocol
    username = form.find_all('input', id='username', attrs={'name': 'username'})
    visible_password = form.find_all('input', id='passwordShow', attrs={'type': 'password'})
    if (len(username) != 1 or username[0].get('type', '').lower() != 'text' or
            username[0].has_attr('disabled') or len(visible_password) != 1 or
            visible_password[0].has_attr('disabled')):
        raise protocol
    inputs = form.find_all('input', attrs={'type': 'hidden'})
    names = [item.get('name') for item in inputs if item.get('name')]
    expected = {'password', 'execution', 'encrypted', '_eventId', 'loginType'}
    if len(names) != len(expected) or set(names) != expected:
        raise protocol
    fields = {item['name']: item.get('value', '') for item in inputs if item.get('name')}
    if not all(fields.get(name) for name in ('execution', '_eventId', 'loginType')):
        raise protocol
    fields.pop('password')
    scripts = [urlsplit(urljoin(response.url, tag['src'])) for tag in document.find_all('script', src=True)]
    if sum(p.scheme == 'https' and p.hostname == 'uia.njfu.edu.cn' and
           p.port in (None, 443) and p.path == urlsplit(NEW_LOGIN_SCRIPT).path and
           not p.username and not p.fragment and not p.query for p in scripts) != 1:
        raise protocol
    widgets = document.find_all(id='captcha2')
    if len(widgets) > 1:
        raise required
    if widgets:
        overlay = widgets[0].find_parent(id='modalOverlay')
        if (not overlay or set(overlay.get('class') or []) != {'modal-overlay'} or
                overlay.get('aria-hidden') == 'false'):
            raise required
        style = overlay.get('style', '').strip()
        if style and not re.fullmatch(r'(?i)display\s*:\s*none\s*;?', style):
            raise required
        if any(re.search(r'(?i)modalOverlay|captcha2|openModal', tag.get_text())
               for tag in document.find_all('script', src=False)):
            raise required
        if any(re.search(r'(?i)modal-overlay|captcha2', tag.get_text()) for tag in document.find_all('style')):
            raise required
        links = [urlsplit(urljoin(response.url, tag['href'])) for tag in document.find_all('link', href=True)
                 if 'stylesheet' in (tag.get('rel') or [])]
        if sum(p.scheme == 'https' and p.hostname == 'uia.njfu.edu.cn' and
               p.port in (None, 443) and p.path == urlsplit(NEW_LOGIN_CSS).path and
               not p.username and not p.fragment and not p.query for p in links) != 1:
            raise protocol
    return action, fields, bool(widgets)


def new_password_encrypt(password, script):
    error = AppError('school_protocol', '学校认证加密协议未通过校验，未发送密码', 502)
    if hashlib.sha256(script.content).hexdigest() != NEW_LOGIN_SCRIPT_SHA256:
        raise error
    pattern = r'''RSAUtils\.getKeyPair\(\s*["']([0-9a-fA-F]+)["']\s*,\s*["']["']\s*,\s*["']([0-9a-fA-F]+)["']\s*\)'''
    keys = re.findall(pattern, script.text)
    if len(keys) != 2 or len(set(keys)) != 1:
        raise error
    exponent, modulus = keys[0]
    if (int(exponent, 16) != 65537 or
            hashlib.sha256(modulus.lower().encode('ascii')).hexdigest() != NEW_LOGIN_KEY_SHA256):
        raise error
    modulus = int(modulus, 16)
    if modulus.bit_length() != 1024 or not modulus % 2:
        raise error
    chunk_size = 2 * ((modulus.bit_length()-1)//16)
    if not password.isascii() or not all(33 <= ord(char) <= 126 for char in password) or len(password) > chunk_size:
        raise error
    plain = password.encode('ascii').ljust(chunk_size, b'\0')
    cipher = pow(int.from_bytes(plain, 'little'), int(exponent, 16), modulus)
    result = format(cipher, 'x')
    return result.zfill((len(result)+3)//4*4)


def require_trusted_new_css(response):
    if hashlib.sha256(response.content).hexdigest() != NEW_LOGIN_CSS_SHA256:
        raise AppError('school_protocol', '学校验证页面样式已变化，未发送密码', 502)


def password_form(response, password):
    document = BeautifulSoup(response.text, 'html.parser')
    form = document.find('form', id='casLoginForm')
    if not form:
        challenge = any(re.search(r'captcha|verify|otp', str(item.get(key, '')), re.I)
                        for item in document.find_all('input') for key in ('id', 'name'))
        text = document.get_text(' ', strip=True)
        if challenge or any(word in text for word in ('验证码', '二次验证', '人脸识别', '短信验证', '手机验证', '扫码验证')):
            raise AppError('verification_required', '学校统一认证需要人工验证，请检查官方登录页面', 401)
        raise AppError('school_protocol', '学校统一认证页面格式不符合预期，未发送密码', 502)
    action = urljoin(response.url, form.get('action', ''))
    parsed = urlsplit(action)
    if (parsed.scheme != 'https' or parsed.hostname != 'uia.njfu.edu.cn'
            or parsed.port not in (None, 443) or parsed.username or parsed.fragment
            or parsed.path.split(';', 1)[0] != '/authserver/login'
            or form.get('method', '').lower() != 'post'):
        raise AppError('school_protocol', '学校统一认证表单地址异常，未发送密码', 502)
    fields = {field['name']: field.get('value', '') for field in form.find_all('input', type='hidden')
              if field.get('name')}
    salt_field = form.find('input', id='pwdDefaultEncryptSalt')
    salt = salt_field.get('value', '').strip().encode('utf-8') if salt_field else b''
    if len(salt) not in (16, 24, 32) or any(not fields.get(k) for k in ('lt', 'execution', '_eventId')):
        raise AppError('school_protocol', '学校统一认证参数变化，未发送密码', 502)
    # Match the official AES-CBC/PKCS7 protocol. The random 64-character prefix
    # absorbs the random first block; only the encrypted password is submitted.
    alphabet = string.ascii_letters+string.digits
    prefix = ''.join(secrets.choice(alphabet) for _ in range(64))
    iv = ''.join(secrets.choice(alphabet) for _ in range(16)).encode()
    fields['password'] = base64.b64encode(AES.new(salt, AES.MODE_CBC, iv).encrypt(
        pad((prefix+password).encode('utf-8'), AES.block_size))).decode('ascii')
    return action, fields


def require_no_captcha(response):
    flag = response.text.strip().split('::::', 1)[0].lower()
    if flag == 'true':
        raise AppError('verification_required', '学校要求验证码，请先在官方登录页面完成人工验证', 401)
    if flag != 'false':
        raise AppError('school_protocol', '无法确认学校验证码要求，未提交登录', 502)


def check_login_response(response):
    if urlsplit(response.url).hostname == 'uia.njfu.edu.cn':
        document = BeautifulSoup(response.text, 'html.parser')
        if document.find(id='otpauthcode'):
            raise AppError('verification_required', '学校统一认证需要人工验证，请检查官方页面', 401)
        if document.find('form', id='casLoginForm') or document.find('form', id='fm1'):
            raise AppError('login_failed', '学校统一认证未通过，请检查统一认证密码、验证码或二次验证要求', 401)
        raise AppError('verification_required', '学校统一认证尚未完成，请检查官方页面的二次验证要求', 401)


def callback_target(response, entry):
    """Read the observed literal navigation; never execute school JavaScript."""
    current=urlsplit(response.url)
    if current.hostname!='libseat.njfu.edu.cn' or not current.path.startswith('/authcenter/doAuth/'):
        return None
    error=AppError('school_protocol','学校认证回调格式变化，未继续登录',502)
    redirect_urls=parse_qs(urlsplit(entry).query).get('redirectUrl',[])
    if len(redirect_urls)!=1:raise error
    expected=urlsplit(redirect_urls[0])
    expected_state=parse_qs(expected.query).get('uuid',[])
    if len(expected_state)!=1 or not expected_state[0]:raise error
    document=BeautifulSoup(response.text,'html.parser')
    literal=r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''
    targets=[]
    for script in document.find_all('script'):
        source=script.string or script.get_text()
        match=re.fullmatch(r'\s*window\.location\.href\s*=\s*('+literal+r')\s*;?\s*',source)
        if match:
            try:targets.append(ast.literal_eval(match[1]))
            except (SyntaxError,ValueError):raise error from None
    if len(targets)!=1 or not isinstance(targets[0],str):raise error
    target=targets[0];parsed=urlsplit(target);query=parse_qs(parsed.query)
    try:
        valid=(parsed.scheme=='https' and parsed.hostname=='libseat.njfu.edu.cn'
            and parsed.port in (None,443) and not parsed.username and not parsed.fragment
            and parsed.path in ('/ic-web/auth/token','/ic-web//auth/token')
            and parsed.path==expected.path and query.get('uuid')==expected_state
            and len(query.get('uniToken',[]))==1 and bool(query['uniToken'][0])
            and set(query)<={'uuid','uniToken','extInfo'})
    except ValueError:raise error from None
    if not valid:raise error
    return target
