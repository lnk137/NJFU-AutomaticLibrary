"""Loopback-only same-origin API; legacy mutation routes are never registered."""
from dataclasses import asdict
import base64
import binascii
import hmac
import ipaddress
from pathlib import Path
import secrets
from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from .models import AppError
from .policy import school_policy
from .execution_health import execution_health
from .auth import COOKIE_NAME, SESSION_SECONDS
from .dashboard import build_dashboard
from .mail_cancellation import MailCancellationService
from .mail_navigation import NAV_COOKIE, issue_navigation_grant, valid_navigation_grant, is_user_navigation


def create_app(settings, service, store, catalog, runtime_status, *, reconcile=None, today=None, auth=None, inbox=None, form_imports=None, power=None, executor=None, calendar=None, notifications=None, cancellations=None, credential_sync=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 65536
    token = secrets.token_urlsafe(32)
    hosts = {f'127.0.0.1:{settings.port}', f'localhost:{settings.port}'}
    frontend = (settings.root/'AL-FrontEnd/dist/build/h5').resolve()
    mail_cancellation = MailCancellationService(notifications, cancellations) if notifications is not None and cancellations is not None else None
    mail_paths = {'/api/local/mail-cancellation/submit', '/api/local/mail-cancellation/status'}
    navigation_key = settings.relay_token or service.vault.key

    @app.before_request
    def boundary():
        g.public=bool(settings.public_origin and 'https://'+request.host==settings.public_origin)
        g.source=request.remote_addr
        if g.public:
            if (request.remote_addr not in {'127.0.0.1','::1'} or request.headers.get('X-NJFU-Relay-HTTPS')!='1'
                    or not hmac.compare_digest(request.headers.get('X-NJFU-Relay-Token',''),settings.relay_token)):
                raise AppError('invalid_relay','公网转发校验失败',403)
            try:g.source=str(ipaddress.ip_address(request.headers.get('X-NJFU-Client-IP','')))
            except ValueError:raise AppError('invalid_relay','公网来源无效',403) from None
        elif request.host not in hosts: raise AppError('invalid_host', '访问地址无效', 403)
        if request.method not in ('GET','POST','PUT'): raise AppError('method_not_allowed', '不支持此操作', 405)
        if request.method != 'GET':
            if request.headers.get('Origin') != (settings.public_origin if g.public else 'http://'+request.host):
                raise AppError('invalid_origin', '请从本站页面操作', 403)
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),token):
                raise AppError('invalid_csrf', '页面校验失效，请刷新后重试', 403)
            if not request.is_json: raise AppError('invalid_json','请提交 JSON 对象',415)
            data = request.get_json(silent=True)
            if not isinstance(data,dict): raise AppError('invalid_json','请求须为 JSON 对象')
        if request.path.startswith('/api/local/') and request.path not in {
                '/api/local/bootstrap', '/api/local/auth/login', '/api/local/auth/logout'} and not (
                    request.method == 'POST' and request.path in mail_paths):
            if auth is None:raise AppError('authentication_required', '登录服务尚未连接', 401)
            g.user=auth.require(request.cookies.get(COOKIE_NAME))
            if g.public and g.user['role']=='root':raise AppError('local_admin_only','管理员请使用本机入口',403)

    def managed(account_id=None):
        gate = getattr(service, 'managed_gate', None)
        if gate is None: return False
        try:
            return (gate.has_managed_scope() if account_id is None else gate.is_managed(account_id)) is True
        except Exception:
            return True

    def public_account(account):
        value = asdict(account)
        is_managed = managed(account.id)
        sync_state = 'not_required'
        if is_managed:
            sync_state = 'unknown'
            if credential_sync is not None:
                try:
                    observed = credential_sync(account.id, account.credential_revision)
                    if observed in ('matched', 'maintenance_required', 'unknown'):
                        sync_state = observed
                except Exception:
                    pass
        value['capabilities'] = {'date_override_segments': not is_managed,
                                 'execution_mode': 'dual_node' if is_managed else 'local',
                                 'credential_sync_state': sync_state}
        return value

    def own(account_id):
        if g.user['role'] != 'root' and g.user['account_id'] != account_id:
            raise AppError('not_found', '账号不存在', 404)
        store.get_account(account_id)

    def admin():
        if g.user['role'] != 'root':raise AppError('forbidden', '仅管理员可执行此操作', 403)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if getattr(g,'public',False):response.headers['Strict-Transport-Security']='max-age=86400'
        if request.path == '/api/local/mail-cancellation/submit':
            response.delete_cookie(NAV_COOKIE, path='/', secure=getattr(g, 'public', False), httponly=True, samesite='Strict')
        return response

    @app.errorhandler(AppError)
    def known_error(exc):
        response=jsonify(error=str(exc),code=exc.code)
        response.status_code=exc.http_status
        if getattr(g, 'public', False) and getattr(exc, 'saved_credential_rejected', False) is True:
            response.headers['X-NJFU-Saved-Credential-Rejected'] = '1'
        # An old read can finish after a new login. Only explicit logout clears
        # the browser cookie; invalid sessions remain denied server-side.
        if exc.http_status==429:response.headers['Retry-After']='300'
        return response

    @app.errorhandler(Exception)
    def unknown_error(exc):
        if isinstance(exc,HTTPException): return jsonify(error='请求无效或入口不存在',code='http_error'),exc.code
        return jsonify(error='本机服务暂不可用，请检查后台状态',code='service_error'),503

    @app.get('/health')
    def health():return jsonify(application='NJFU-AutomaticLibrary-local-multi',status='ok',allow_live=settings.allow_live,public_enabled=bool(settings.public_origin))
    @app.get('/api/local/bootstrap')
    def bootstrap():
        user=auth.current(request.cookies.get(COOKIE_NAME)) if auth else None
        if g.public and user and user['role']=='root':user=None
        return jsonify(csrf=token,user=user,public_access=g.public,admin_login=not g.public)
    @app.post('/api/local/auth/login')
    def login():
        if auth is None:raise AppError('unavailable','登录服务尚未连接',503)
        data=request.get_json()
        if g.public and data.get('kind')=='root':raise AppError('local_admin_only','管理员请使用本机入口',403)
        session,user=auth.login(data,g.source)
        auth.logout(request.cookies.get(COOKIE_NAME))
        response=jsonify(user=user)
        response.set_cookie(COOKIE_NAME,session,max_age=SESSION_SECONDS,httponly=True,samesite='Strict',secure=g.public,path='/')
        return response
    @app.post('/api/local/auth/logout')
    def logout():
        if auth:auth.logout(request.cookies.get(COOKIE_NAME))
        response=jsonify(ok=True);response.delete_cookie(COOKIE_NAME,path='/');return response
    def mail_ticket():
        if mail_cancellation is None:raise AppError('unavailable', '邮件取消服务尚未加载', 503)
        data = request.get_json()
        if set(data) != {'ticket'}:raise AppError('mail_cancellation_invalid', '取消链接内容无效')
        return data['ticket']
    @app.post('/api/local/mail-cancellation/submit')
    def mail_cancel_submit():
        if not valid_navigation_grant(navigation_key, request.cookies.get(NAV_COOKIE)):
            raise AppError('mail_navigation_required', '请直接点击邮件中的取消预约按钮。预览不会取消预约。', 403)
        ticket = mail_ticket()
        return jsonify(mail_cancellation.submit(ticket))
    @app.post('/api/local/mail-cancellation/status')
    def mail_cancel_status():
        ticket = mail_ticket()
        return jsonify(mail_cancellation.status(ticket))
    @app.get('/api/local/auth/me')
    def me():return jsonify(user=g.user)
    @app.get('/api/local/admin/users')
    def users():
        admin();return jsonify(users=auth.list_users())
    @app.get('/api/local/admin/dashboard')
    def dashboard():
        admin()
        return jsonify(build_dashboard(store,catalog,request.args,runtime=execution_health(runtime_status(),managed=managed())))
    @app.post('/api/local/admin/executor')
    def executor_control():
        admin()
        if executor is None:raise AppError('executor_unavailable','当前执行器无需此恢复操作',409)
        data=request.get_json()
        if set(data)!={'action'} or not isinstance(data['action'],str) or data['action'] not in {'recover','pause','resume'}:
            raise AppError('invalid_executor_action','请选择恢复连接、暂停或继续')
        if data['action']=='pause':executor.pause()
        else:executor.recover_once(resume=data['action']=='resume')
        dual_control=getattr(executor,'dual_control',None)
        if dual_control and data['action'] in {'pause','resume'}:
            dual_control(data['action'])
        auth._audit('executor_'+data['action'],actor='root')
        return jsonify(runtime=runtime_status())
    def forms():
        admin()
        if form_imports is None:raise AppError('unavailable','表单导入尚未连接',503)
        return form_imports
    @app.get('/api/local/admin/form-template')
    def form_template():
        admin()
        return send_file(settings.root/'AL-BackEnd/resources/booking-form.xlsx',as_attachment=True,download_name='NJFU-personal-booking-form.xlsx')
    @app.get('/api/local/admin/form-imports')
    def form_list():return jsonify(items=forms().recent())
    @app.post('/api/local/admin/form-imports')
    def form_create():
        importer=forms();data=request.get_json()
        try:content=base64.b64decode(data.get('content',''),validate=True)
        except (ValueError,TypeError,binascii.Error):raise AppError('invalid_form','文件内容无法读取') from None
        return jsonify(importer.create(data.get('filename'),content,data.get('request_id')))
    @app.get('/api/local/admin/form-imports/<key>')
    def form_get(key):return jsonify(forms().get(key))
    @app.post('/api/local/admin/form-imports/<key>/confirm')
    def form_confirm(key):return jsonify(forms().confirm(key))
    @app.put('/api/local/admin/users/<student>')
    def access(student):
        admin();return jsonify(user=auth.set_access(student,request.get_json()))
    @app.get('/api/local/admin/audit')
    def audit():
        admin();return jsonify(items=auth.list_audit())
    @app.get('/api/local/admin/inbox')
    def admin_inbox():
        admin();return jsonify(inbox.status() if inbox else {'configured':False})
    def local_power():
        admin()
        if g.public:raise AppError('local_admin_only', '电源设置仅本机管理员可用', 403)
        if power is None:raise AppError('power_unavailable', '电源服务尚未加载', 503)
        return power
    @app.get('/api/local/admin/power')
    def power_status():return jsonify(local_power().status())
    @app.put('/api/local/admin/power')
    def power_settings():return jsonify(local_power().update(request.get_json()))
    @app.post('/api/local/admin/power/install')
    def power_install():
        component = local_power()
        if request.get_json() != {}:raise AppError('invalid_power', '安装不接受自定义参数')
        return jsonify(component.install())
    @app.get('/api/local/inbox')
    def inbox_status():
        return jsonify(inbox.status(None if g.user['role']=='root' else g.user['student_id']) if inbox else {'configured':False})
    @app.get('/api/local/policy')
    def policy():return jsonify(school_policy())
    @app.get('/api/local/accounts')
    def accounts():return jsonify(accounts=[public_account(a) for a in store.list_accounts()
        if g.user['role']=='root' or a.id==g.user['account_id']])
    @app.post('/api/local/accounts/verify')
    def verify():
        d=request.get_json()
        if g.user['role']!='root' and d.get('student_id')!=g.user['student_id']:
            raise AppError('forbidden','只能验证自己的学校账号',403)
        return jsonify(account=public_account(service.verify_and_save(d.get('student_id'),d.get('password',''),d.get('alias',''),d.get('expected_revision'))))
    @app.put('/api/local/accounts/<account_id>/plan')
    def plan(account_id):
        own(account_id)
        d=request.get_json()
        return jsonify(account=public_account(service.save_plan(account_id,d.get('plan'),d.get('expected_revision'))))
    @app.post('/api/local/accounts/<account_id>/check')
    def check(account_id):
        own(account_id);return jsonify(account=public_account(service.recheck(account_id)))
    @app.post('/api/local/accounts/<account_id>/date-exceptions')
    def date_exceptions(account_id):
        own(account_id)
        data = request.get_json()
        return jsonify(account=public_account(service.update_date_exceptions(account_id, data, data.get('expected_revision'))))
    @app.get('/api/local/accounts/<account_id>/seat-preferences')
    def seat_preferences(account_id):
        own(account_id)
        return jsonify(service.seat_preferences(account_id))
    @app.put('/api/local/accounts/<account_id>/seat-preferences')
    def save_seat_preferences(account_id):
        own(account_id)
        data = request.get_json()
        return jsonify(service.save_seat_preferences(account_id, data.get('preferences'), data.get('expected_revision')))
    @app.post('/api/local/accounts/<account_id>/query')
    def query(account_id):
        own(account_id);return jsonify(service.query(account_id))
    @app.post('/api/local/accounts/<account_id>/today/preview')
    def preview_today(account_id):
        own(account_id)
        if today is None: raise AppError('unavailable', '当天预约尚未连接', 503)
        return jsonify(today.preview(account_id, request.get_json()))
    @app.post('/api/local/accounts/<account_id>/today/submit')
    def submit_today(account_id):
        own(account_id)
        if today is None: raise AppError('unavailable', '当天预约尚未连接', 503)
        return jsonify(today.submit(account_id, request.get_json().get('preview_token')))
    def today_batches(account_id):
        own(account_id)
        if today is None:raise AppError('unavailable', '当天预约尚未连接', 503)
        return today.batches
    @app.post('/api/local/accounts/<account_id>/today/batches/preview')
    def preview_today_batch(account_id):
        return jsonify(today_batches(account_id).preview(account_id, request.get_json()))
    @app.post('/api/local/accounts/<account_id>/today/batches/submit')
    def submit_today_batch(account_id):
        batch = today_batches(account_id).submit(account_id, request.get_json().get('preview_token'))
        return jsonify(batch), 202 if batch['state'] == 'running' else 200
    @app.get('/api/local/accounts/<account_id>/today/batches')
    def latest_today_batch(account_id):
        return jsonify(today_batches(account_id).latest(account_id))
    @app.get('/api/local/accounts/<account_id>/today/batches/<batch_id>')
    def get_today_batch(account_id, batch_id):
        return jsonify(today_batches(account_id).get(account_id, batch_id))
    @app.get('/api/local/accounts/<account_id>/records')
    def snapshot(account_id):
        own(account_id);return jsonify(service.snapshot(account_id))
    @app.get('/api/local/accounts/<account_id>/calendar')
    def booking_calendar(account_id):
        own(account_id)
        if calendar is None:raise AppError('unavailable', '预约日历尚未加载', 503)
        return jsonify(calendar.get(account_id,request.args.get('start'),request.args.get('end')))
    def notification_service(account_id):
        own(account_id)
        if notifications is None:raise AppError('unavailable', '邮件通知尚未加载', 503)
        return notifications
    @app.get('/api/local/accounts/<account_id>/notifications')
    def notification_settings(account_id):
        return jsonify(notification_service(account_id).get_settings(account_id))
    @app.put('/api/local/accounts/<account_id>/notifications')
    def save_notification_settings(account_id):
        return jsonify(notification_service(account_id).update_preferences(account_id,request.get_json()))
    @app.post('/api/local/accounts/<account_id>/notifications/verification')
    def request_mail_verification(account_id):
        data=request.get_json()
        if set(data)!={'address'}:raise AppError('invalid_notification', '请填写收件邮箱')
        return jsonify(notification_service(account_id).request_verification(account_id,data))
    @app.post('/api/local/accounts/<account_id>/notifications/verification/confirm')
    def confirm_mail_verification(account_id):
        data=request.get_json()
        if set(data)!={'challenge_id','code'}:raise AppError('invalid_notification', '请填写邮箱验证码')
        return jsonify(notification_service(account_id).confirm_verification(account_id,data))
    @app.get('/api/local/admin/mail')
    def mail_status():
        admin();return jsonify(notifications.status() if notifications else {'configured':False})
    def cancellation_service(account_id):
        own(account_id)
        if cancellations is None:raise AppError('unavailable', '取消服务尚未加载', 503)
        return cancellations
    @app.post('/api/local/accounts/<account_id>/cancellations/preview')
    def cancellation_preview(account_id):
        return jsonify(cancellation_service(account_id).preview(account_id,request.get_json()))
    @app.post('/api/local/accounts/<account_id>/cancellations/submit')
    def cancellation_submit(account_id):
        data=request.get_json()
        if set(data)!={'preview_token'}:raise AppError('invalid_cancellation', '请先核对具体预约')
        return jsonify(cancellation_service(account_id).submit(account_id,data.get('preview_token')))
    @app.get('/api/local/accounts/<account_id>/cancellations/<cancel_id>')
    def cancellation_status(account_id,cancel_id):
        return jsonify(cancellation_service(account_id).get(account_id,cancel_id))
    @app.post('/api/local/accounts/<account_id>/cancellations/<cancel_id>/reconcile')
    def cancellation_reconcile(account_id,cancel_id):
        if request.get_json()!={}:raise AppError('invalid_cancellation', '核对取消结果不接受其他参数')
        return jsonify(cancellation_service(account_id).reconcile(account_id,cancel_id))
    @app.get('/api/local/seats')
    def seats():return jsonify(seats=[asdict(s) for s in catalog.values()])
    @app.get('/api/local/status')
    def status():
        result=execution_health(runtime_status(), managed=managed(None if g.user['role']=='root' else g.user['account_id']),
                                account_id=None if g.user['role']=='root' else g.user['account_id'])
        if g.user['role']=='root':result=result | {'connection':service.last_connection}
        else:result={k:result[k] for k in ('allow_live','execution_message','scheduler_running',
                                         'clock_ok','ready_to_submit','last_error','scheduler_warning','execution_scope','dual_node_status') if k in result}
        if inbox:result['inbox']=inbox.status(None if g.user['role']=='root' else g.user['student_id'])
        return jsonify(result)
    @app.get('/api/local/runs')
    def runs():
        try:limit=int(request.args.get('limit','100'))
        except ValueError:raise AppError('invalid_limit','分页条数无效') from None
        account_id=request.args.get('account_id') or None
        if g.user['role']!='root':
            if account_id:own(account_id)
            account_id=g.user['account_id']
        return jsonify(asdict(store.list_runs(account_id,request.args.get('date') or None,limit,request.args.get('cursor') or None)))
    @app.get('/api/local/runs/<path:run_key>')
    def run_detail(run_key):
        row=store.get_run(run_key)
        if not row:raise AppError('not_found','任务不存在',404)
        own(row['account_id'])
        return jsonify(run=store.run_view(row))
    @app.post('/api/local/runs/<path:run_key>/reconcile')
    def reconcile_run(run_key):
        row=store.get_run(run_key)
        if not row:raise AppError('not_found','任务不存在',404)
        own(row['account_id'])
        if reconcile is None:raise AppError('unavailable','任务核对服务未连接',503)
        return jsonify(result=asdict(reconcile(run_key)))
    @app.get('/')
    @app.route('/<path:asset>', methods=['GET','POST','PUT'])
    def static_asset(asset='index.html'):
        if request.method != 'GET': raise AppError('not_found','入口不存在',404)
        path=(frontend/asset).resolve()
        if (not path.is_relative_to(frontend) or path.suffix not in {'.html','.js','.css','.png','.jpg','.svg','.ico','.woff','.woff2','.ttf'} or not path.is_file()):
            raise AppError('not_found','资源不存在',404)
        response = send_file(path)
        if asset == 'static/mail-cancel.html':
            if is_user_navigation(request.headers):
                response.set_cookie(NAV_COOKIE, issue_navigation_grant(navigation_key), max_age=60,
                    httponly=True, samesite='Strict', secure=g.public, path='/')
            else:
                response.delete_cookie(NAV_COOKIE, path='/', httponly=True, samesite='Strict', secure=g.public)
        return response
    return app
