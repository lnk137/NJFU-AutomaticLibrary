"""HTTPS gateway with school-first authentication and restricted offline sessions."""
from dataclasses import asdict, dataclass
import hmac
import ipaddress
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import requests
from flask import Flask, jsonify, request, send_file, make_response
from werkzeug.exceptions import HTTPException
from local_app.models import AppError, Seat, validate_plan
from local_app.mail_navigation import NAV_COOKIE, issue_navigation_grant, is_user_navigation
from deploy.inbox_store import COOKIE, SESSION, InboxStore
from urllib3.exceptions import NewConnectionError


def connection_not_established(exc):
    pending, seen = [exc], set()
    while pending and len(seen) < 30:
        value = pending.pop()
        if id(value) in seen: continue
        seen.add(id(value))
        if isinstance(value, (requests.ConnectTimeout, NewConnectionError, ConnectionRefusedError)): return True
        if isinstance(value, BaseException): pending.extend(a for a in value.args if isinstance(a,BaseException))
        pending.extend(v for v in (getattr(value,'reason',None),getattr(value,'__cause__',None),getattr(value,'__context__',None)) if isinstance(v,BaseException))
    return False


@dataclass
class Reply:
    status: int
    body: bytes
    cookies: tuple = ()
    saved_credential_rejected: bool = False


class Upstream:
    """Only connection unavailability is an offline fallback signal."""
    def __init__(self, origin, relay_token):
        self.origin, self.token = origin, relay_token
        self.csrf, self.lock = None, threading.Lock()

    def _send(self, method, path, source, cookie='', data=None, csrf=None, timeout=40):
        if not path.startswith('/api/local/') and path != '/health': raise ValueError('Invalid upstream path')
        session = requests.Session(); session.trust_env = False
        headers = {'Host': urlsplit(self.origin).netloc, 'X-NJFU-Relay-HTTPS': '1',
                   'X-NJFU-Relay-Token': self.token, 'X-NJFU-Client-IP': source, 'Cookie': cookie}
        if method != 'GET': headers.update({'Origin': self.origin, 'X-CSRF-Token': csrf or ''})
        try:
            return session.request(method, 'http://127.0.0.1:15003'+path, headers=headers,
                                   json=data, timeout=(1.5, timeout), allow_redirects=False)
        finally: session.close()

    def probe(self, source):
        try: response = self._send('GET', '/health', source, timeout=2)
        except requests.ConnectionError as exc:
            if connection_not_established(exc): return False
            raise AppError('executor_unavailable', '执行端响应中断或超时，未降级登录，请稍后重试', 503) from None
        except requests.Timeout: raise AppError('executor_unavailable', '执行端响应超时，未降级登录，请稍后重试', 503) from None
        except requests.RequestException: raise AppError('executor_unavailable', '执行端协议响应异常，未降级登录', 503) from None
        try: body = response.json()
        except ValueError: body = {}
        if response.status_code != 200 or body.get('application') != 'NJFU-AutomaticLibrary-local-multi':
            raise AppError('executor_unavailable', '执行端状态异常，未降级登录', 503)
        return True

    def forward(self, method, path, source, cookie='', data=None):
        try:
            for attempt in range(2):
                csrf = None
                if method != 'GET':
                    with self.lock:
                        if not self.csrf:
                            boot = self._send('GET', '/api/local/bootstrap', source, timeout=3)
                            if boot.status_code != 200: raise AppError('executor_unavailable', '执行端页面校验暂不可用', 503)
                            self.csrf = boot.json().get('csrf')
                        csrf = self.csrf
                response = self._send(method, path, source, cookie, data, csrf)
                if response.status_code == 403:
                    try: stale = response.json().get('code') == 'invalid_csrf'
                    except ValueError: stale = False
                    if stale and attempt == 0:
                        with self.lock: self.csrf = None
                        continue
                return Reply(response.status_code, response.content, tuple(response.raw.headers.getlist('Set-Cookie')),
                             response.headers.get('X-NJFU-Saved-Credential-Rejected') == '1')
        except (requests.RequestException, ValueError):
            raise AppError('executor_unavailable', '执行端连接中断，未降级登录或重复提交，请重新检查状态', 503) from None


def create_gateway(config, store, upstream=None):
    origin = config['origin']; host = urlsplit(origin).netloc
    upstream = upstream or Upstream(origin, config['relay_token'])
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 2*1024*1024
    csrf = secrets.token_urlsafe(32)

    def user():
        value = store.current(request.cookies.get(COOKIE))
        if not value: raise AppError('authentication_required', '请重新登录', 401)
        return value

    def relay(reply):
        response = make_response(reply.body, reply.status)
        response.headers['Content-Type'] = 'application/json'
        for cookie in reply.cookies:
            if cookie.startswith('njfu_autolib_session='): response.headers.add('Set-Cookie', cookie)
        return response

    def forwarded(path, data=None):
        return relay(upstream.forward(request.method, path, request.headers['X-NJFU-Client-IP'],
                                      request.headers.get('Cookie', ''), data))

    @app.before_request
    def boundary():
        if request.host != host or request.remote_addr not in ('127.0.0.1', '::1'):
            raise AppError('invalid_relay', '访问入口无效', 403)
        if request.path.startswith('/_inbox/'):
            supplied = request.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer '+config['worker_token']):
                raise AppError('invalid_worker', '执行端校验失败', 403)
            return
        if (request.headers.get('X-NJFU-Relay-HTTPS') != '1'
                or not hmac.compare_digest(request.headers.get('X-NJFU-Relay-Token', ''), config['relay_token'])):
            raise AppError('invalid_relay', '公网转发校验失败', 403)
        try: ipaddress.ip_address(request.headers.get('X-NJFU-Client-IP', ''))
        except ValueError: raise AppError('invalid_relay', '公网来源无效', 403) from None
        if request.method not in ('GET', 'POST', 'PUT'): raise AppError('method_not_allowed', '不支持此操作', 405)
        if request.method != 'GET':
            if request.headers.get('Origin') != origin: raise AppError('invalid_origin', '请从本站页面操作', 403)
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf):
                raise AppError('invalid_csrf', '页面校验失效，请刷新后重试', 403)
            if request.content_length is not None and request.content_length > 65536:
                raise AppError('invalid_request', '请求过大', 413)
            if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                raise AppError('invalid_json', '请提交 JSON 对象')
        if request.path.startswith('/api/local/admin/'):
            raise AppError('local_admin_only', '管理员请使用本机入口', 403)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Strict-Transport-Security': 'max-age=86400',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if request.path == '/api/local/mail-cancellation/submit':
            response.delete_cookie(NAV_COOKIE, secure=True, httponly=True, samesite='Strict', path='/')
        return response

    @app.errorhandler(AppError)
    def known(exc):
        response = jsonify(error=str(exc), code=exc.code); response.status_code = exc.http_status
        if exc.http_status == 429: response.headers['Retry-After'] = '300'
        return response

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException): return jsonify(error='入口不存在或请求无效', code='http_error'), exc.code
        return jsonify(error='离线收件服务暂不可用，请稍后重试', code='inbox_error'), 503

    @app.post('/_inbox/snapshot')
    def publish():
        data = request.get_json(silent=True)
        if not isinstance(data, dict): raise AppError('invalid_snapshot', '快照格式无效')
        store.publish(data); return jsonify(ok=True)

    @app.post('/_inbox/poll')
    def poll(): return jsonify(jobs=store.poll((request.get_json(silent=True) or {}).get('quiet_until')), summary=store.summary())

    @app.post('/_inbox/ack')
    def ack():
        data = request.get_json(); store.ack(data['request_id'], data['lease'], data['result'])
        return jsonify(ok=True)

    @app.get('/_inbox/status')
    def worker_status(): return jsonify(store.summary())

    @app.get('/health')
    def health():
        return jsonify(application='NJFU-AutomaticLibrary-inbox-gateway',status='ok',
                       executor_online=store.summary()['executor_online'])

    @app.get('/api/local/bootstrap')
    def bootstrap():
        offline = store.current(request.cookies.get(COOKIE))
        online = upstream.probe(request.headers['X-NJFU-Client-IP'])
        current = offline
        if online and not offline and COOKIE not in request.cookies:
            reply = upstream.forward('GET', '/api/local/bootstrap', request.headers['X-NJFU-Client-IP'], request.headers.get('Cookie', ''))
            if reply.status != 200: return relay(reply)
            current = json.loads(reply.body).get('user')
            if current and current.get('role') == 'root': current = None
        return jsonify(csrf=csrf, user=current, public_access=True, admin_login=False,
                       executor_online=online, offline_available=True)

    @app.post('/api/local/auth/login')
    def login():
        data = request.get_json()
        if data.get('kind') != 'student': raise AppError('local_admin_only', '管理员请使用本机入口', 403)
        if upstream.probe(request.headers['X-NJFU-Client-IP']):
            reply = upstream.forward('POST', '/api/local/auth/login', request.headers['X-NJFU-Client-IP'], request.headers.get('Cookie', ''), data)
            try: code = json.loads(reply.body).get('code')
            except (ValueError, AttributeError): code = None
            if (reply.saved_credential_rejected is True
                    and code in ('login_failed', 'verification_required', 'login_incomplete', 'identity_mismatch')):
                store.block(data.get('username'), code)
            response = relay(reply)
            if 200 <= reply.status < 300:
                store.logout(request.cookies.get(COOKIE)); response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Strict')
            return response
        offline = store.authenticate(data.get('username'), data.get('password'), request.headers['X-NJFU-Client-IP'])
        token = store.issue(offline); offline.pop('generation', None)
        response = jsonify(user=offline)
        response.set_cookie(COOKIE, token, max_age=SESSION, secure=True, httponly=True, samesite='Strict', path='/')
        response.delete_cookie('njfu_autolib_session',secure=True,httponly=True,samesite='Strict',path='/')
        return response

    @app.post('/api/local/auth/logout')
    def logout():
        store.logout(request.cookies.get(COOKIE))
        revoked = False
        try:
            if upstream.probe(request.headers['X-NJFU-Client-IP']):
                reply = upstream.forward('POST', '/api/local/auth/logout', request.headers['X-NJFU-Client-IP'],
                                         request.headers.get('Cookie', ''), {})
                revoked = 200 <= reply.status < 300 and json.loads(reply.body).get('ok') is True
        except (AppError, ValueError, AttributeError):
            pass
        response = jsonify(ok=True, upstream_revoked=revoked)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Strict', path='/')
        response.delete_cookie('njfu_autolib_session', secure=True, httponly=True, samesite='Strict', path='/')
        return response

    @app.route('/api/local/<path:path>', methods=['GET', 'POST', 'PUT'])
    def api(path):
        if request.method == 'POST' and path in ('mail-cancellation/submit', 'mail-cancellation/status'):
            return forwarded('/api/local/' + path, request.get_json())
        offline = store.current(request.cookies.get(COOKIE))
        if COOKIE in request.cookies and not offline:
            raise AppError('authentication_required','离线认证已失效，请重新通过学校认证登录',401)
        if not offline:
            if not upstream.probe(request.headers['X-NJFU-Client-IP']):
                raise AppError('offline_login_required', '执行电脑离线，请重新登录以暂存配置', 401)
            return forwarded('/api/local/'+path+('?' + request.query_string.decode() if request.query_string else ''), request.get_json(silent=True))
        student = offline['student_id']; account = store.public_account(student); account_id = account['id']
        if request.method == 'GET':
            if path == 'auth/me': return jsonify(user=offline)
            if path == 'accounts': return jsonify(accounts=[account])
            if path == 'policy': return jsonify(store.shared('policy'))
            if path == 'seats': return jsonify(seats=store.shared('seats'))
            if path == 'inbox': return jsonify(store.summary(student))
            if path == 'status':
                value = store.shared('runtime')
                value.update(scheduler_running=False, ready_to_submit=False, execution_message='离线登录：配置等待执行端确认', inbox=store.summary(student), offline_mode=True)
                return jsonify(value)
            if path == 'runs': return jsonify(store.cached_runs(student, request.args))
            if path == f'accounts/{account_id}/records': return jsonify(store.public_data(student, 'records'))
            if path.startswith('inbox/'): return jsonify(store.receipt(student, path.split('/', 1)[1]))
        if path == f'accounts/{account_id}/plan' and request.method == 'PUT':
            data = request.get_json()
            seats = {s['id']: Seat(s['id'], s['name'], s['location']) for s in store.shared('seats')}
            data['plan'] = asdict(validate_plan(data.get('plan'), seats))
            receipt = store.enqueue(student, data, request.cookies.get(COOKIE))
            return jsonify(account=account, pending=receipt['state'] != 'applied', receipt=receipt,
                           message='已收件，尚未在执行电脑生效；以同步结果为准'), 202 if receipt['state'] != 'applied' else 200
        raise AppError('offline_scope', '离线登录仅能查看缓存和暂存自己的预约配置；学校操作请重新通过实时认证登录', 403)

    @app.get('/')
    @app.get('/<path:asset>')
    def static(asset='index.html'):
        root = Path(config['static_root']).resolve(); path = (root/asset).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix not in {'.html', '.js', '.css', '.png', '.jpg', '.svg', '.ico', '.woff', '.woff2', '.ttf'}:
            raise AppError('not_found', '资源不存在', 404)
        response = send_file(path)
        if asset == 'static/mail-cancel.html' and request.path == '/static/mail-cancel.html':
            if is_user_navigation(request.headers):
                response.set_cookie(NAV_COOKIE, issue_navigation_grant(config['relay_token']), max_age=60,
                                    secure=True, httponly=True, samesite='Strict', path='/')
            else:
                response.delete_cookie(NAV_COOKIE, secure=True, httponly=True, samesite='Strict', path='/')
        return response

    return app


def main():
    import argparse
    from waitress import serve, create_server
    parser = argparse.ArgumentParser(); parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if args.config.is_symlink() or args.config.stat().st_mode & 0o077: raise ValueError('Private config required')
    config = json.loads(args.config.read_text())
    for key in ('relay_token', 'worker_token'):
        path = Path(config.pop(key+'_file'))
        if path.is_symlink() or path.stat().st_mode & 0o077: raise ValueError('Private token required')
        config[key] = path.read_text().strip()
    store = InboxStore(config['database'])
    app = create_gateway(config, store)
    control = create_server(app, host='127.0.0.1', port=15006, threads=4,
                            connection_limit=30, max_request_body_size=2*1024*1024, channel_timeout=30)
    threading.Thread(target=control.run, name='njfu-inbox-control', daemon=True).start()
    serve(app, host='127.0.0.1', port=15005, threads=12,
          connection_limit=100, max_request_body_size=2*1024*1024, channel_timeout=60)


if __name__ == '__main__': main()
