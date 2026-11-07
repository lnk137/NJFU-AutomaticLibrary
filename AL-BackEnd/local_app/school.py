"""Direct-campus protocol. No database, VPN, implicit login or retrying POST."""
from datetime import datetime
from contextlib import contextmanager
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import requests
from urllib3.util import Timeout
from .cas import (NEW_LOGIN_SCRIPT, NEW_LOGIN_CSS, password_form, require_no_captcha,
                  check_login_response, callback_target, is_new_password_page,
                  new_password_form, new_password_encrypt, require_trusted_new_css)
from .models import AppError, Identity, SchoolRecord, Submission, ZONE, credentials
from .recovery_policy import BUSY_REJECTION, candidate_attempts, retryable_seat_rejection
from .elapsed_clock import elapsed_time

BASE = 'https://libseat.njfu.edu.cn/'


class CampusClient:
    # 1027 was read from the user's approved official record on 2026-10-20.
    active_statuses = frozenset({1027})
    # Exact rejection observed on 2026-10-22; code 1 alone is not retryable.
    busy_rejection = BUSY_REJECTION
    def __init__(self, student_id, password, *, transport=None, allow_live=False):
        self.student_id, self.password = credentials(student_id, password)
        self.http = transport if transport is not None else requests.Session()
        self.http.trust_env = False
        self.http.headers.update({'User-Agent': 'NJFU-Local/1.0', 'Accept': 'application/json, text/plain, */*'})
        self.allow_live, self.identity, self._token = allow_live, None, None
        self._booking_ready = False
        self._prepared = []
        self._read_deadline = None
        self.submission_guard = lambda: True
        self._ownership_guard = None
        self._auth_flow = False
        self.cancellation_guard = lambda: False

    def _guard_authentication(self):
        guard=getattr(self._ownership_guard,'auth',None)
        if guard is not None:guard()

    @property
    def ownership_guard(self):
        return self._ownership_guard

    @ownership_guard.setter
    def ownership_guard(self, guard):
        if not callable(guard) or (self._ownership_guard is not None and guard is not self._ownership_guard):
            raise AppError('dual_guard_bound', '发送归属保护已绑定，不能覆盖', 409)
        self._ownership_guard = guard

    @contextmanager
    def _ownership_scope(self, run):
        guard = self._ownership_guard
        if guard is None:
            yield
        elif hasattr(guard, 'post'):
            with guard.post(run):yield
        elif guard() is True:
            yield
        else:
            raise AppError('dual_ownership_denied', '双机发送资格未确认，未发送预约', 409)

    def _request(self, method, path, *, request_timings=None, ownership_run=None, **kwargs):
        url = urljoin(BASE, path)
        for _ in range(10):
            parsed = urlsplit(url)
            # The school's CAS service advertises an HTTP callback. Upgrade that
            # exact school callback to TLS before sending a ticket over the wire.
            if (parsed.scheme == 'http' and parsed.hostname == 'libseat.njfu.edu.cn'
                    and parsed.port in (None, 80) and parsed.path.startswith('/authcenter/')):
                url = urlunsplit(parsed._replace(scheme='https', netloc='libseat.njfu.edu.cn'))
                parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.hostname not in ('libseat.njfu.edu.cn', 'uia.njfu.edu.cn') or parsed.port not in (None, 443)
                    or parsed.username or parsed.fragment):
                raise AppError('redirect_blocked', '学校请求跳转到未允许的地址', 502)
            auth_post = parsed.hostname == 'uia.njfu.edu.cn' and parsed.path.split(';', 1)[0] == '/authserver/login'
            reserve_post = parsed.hostname == 'libseat.njfu.edu.cn' and parsed.path == '/ic-web/reserve/seat'
            cancel_post = parsed.hostname == 'libseat.njfu.edu.cn' and parsed.path == '/ic-web/reserve/delete'
            if method == 'POST' and not (auth_post or reserve_post or cancel_post):
                raise AppError('operation_blocked', '未允许的学校操作', 403)
            headers = dict(kwargs.get('headers', {}))
            if parsed.hostname == 'libseat.njfu.edu.cn' and self._token:
                headers['token'] = self._token
            request_options = {**kwargs, 'headers': headers}
            timeout = (5, 10)
            if method == 'GET' and self._read_deadline is not None:
                remaining = self._read_deadline - elapsed_time()
                if remaining <= 0:
                    raise AppError('network_error', '只读预热已超过准备预算', 503)
                timeout = Timeout(total=remaining, connect=min(1, remaining), read=remaining)
            if (parsed.path in ('/ic-web/reserve/seat', '/ic-web/reserve/delete')
                    and (not reserve_post or self._ownership_guard is not None)):
                checking = time.perf_counter()
                try:
                    guard = self.cancellation_guard if cancel_post else self.submission_guard
                    if method != 'POST' or not self.allow_live or not guard():
                        raise AppError('live_disabled', '真实预约未开启', 403)
                finally:
                    if request_timings is not None:
                        request_timings['guard_ms'] = round((time.perf_counter()-checking)*1000, 3)
            def send_request():
                # Start only after ownership and final permission checks pass.
                request_start = time.perf_counter()
                if request_timings is not None:
                    request_timings['request_started_at'] = time.time()
                try:
                    return self.http.request(method, url, timeout=timeout, allow_redirects=False, **request_options)
                finally:
                    if request_timings is not None:
                        request_timings['request_finished_at'] = time.time()
                        request_timings['request_ms'] = round((time.perf_counter()-request_start)*1000, 3)

            try:
                if self._auth_flow:self._guard_authentication()
                if reserve_post:
                    checking = time.perf_counter()
                    try:
                        with self._ownership_scope(ownership_run):
                            try:
                                if method != 'POST' or not self.allow_live or not self.submission_guard():
                                    raise AppError('live_disabled', '提交前执行校验已变化，未发送预约', 403)
                            finally:
                                if request_timings is not None:
                                    request_timings['guard_ms'] = round(request_timings.get('guard_ms', 0) +
                                        (time.perf_counter()-checking)*1000, 3)
                                checking = None
                            response = send_request()
                    finally:
                        # Ownership can reject before the final guard is reached.
                        if checking is not None and request_timings is not None:
                            request_timings['guard_ms'] = round(request_timings.get('guard_ms', 0) +
                                (time.perf_counter()-checking)*1000, 3)
                else:
                    response = send_request()
            except requests.RequestException:
                raise AppError('network_error', '学校连接失败，请检查校园网', 503) from None
            if method == 'GET' and self._read_deadline is not None and elapsed_time() > self._read_deadline:
                raise AppError('network_error', '只读预热已超过准备预算', 503)
            if response.status_code in (301, 302, 303, 307, 308):
                if method == 'POST':
                    if not auth_post or response.status_code not in (302, 303):
                        raise AppError('login_incomplete', '学校认证未完成，请检查登录要求', 502)
                    method = 'GET'
                location = response.headers.get('Location')
                if not location: raise AppError('school_protocol', '学校跳转地址缺失', 502)
                url = urljoin(url, location)
                kwargs = {}  # Never forward a POST body, token, or form headers.
                continue
            if response.status_code != 200: raise AppError('school_http', '学校服务暂不可用', 503)
            return response
        raise AppError('redirect_loop', '学校跳转未完成', 502)

    @staticmethod
    def _json(response):
        try: result = response.json()
        except ValueError: raise AppError('school_format', '学校响应不是有效数据，请核对登录页面或验证码要求', 502) from None
        if not isinstance(result, dict) or type(result.get('code')) is not int:
            raise AppError('school_format', '学校响应格式异常', 502)
        return result

    def login(self):
        self._guard_authentication()
        self._auth_flow=True
        try:return self._login()
        finally:self._auth_flow=False

    def _login(self):
        self.identity, self._token = None, None
        self._booking_ready = False
        self._prepared = []
        address = self._json(self._request('GET', 'ic-web/auth/address', params={
            'finalAddress': BASE.rstrip('/'), 'errPageUrl': BASE+'#/error',
            'manager': 'false', 'consoleType': '16'}))
        entry = address.get('data')
        if address['code'] != 0 or not isinstance(entry, str):
            raise AppError('school_protocol', '学校未提供统一身份认证入口', 502)
        parsed = urlsplit(entry)
        if parsed.hostname != 'libseat.njfu.edu.cn' or parsed.path != '/authcenter/toLoginPage':
            raise AppError('school_protocol', '学校统一认证入口异常', 502)
        page = self._request('GET', entry)
        if is_new_password_page(page):
            action, fields, has_widget = new_password_form(page)
            script = self._request('GET', NEW_LOGIN_SCRIPT)
            fields['password'] = new_password_encrypt(self.password, script)
            if has_widget:require_trusted_new_css(self._request('GET', NEW_LOGIN_CSS))
        else:
            action, fields = password_form(page, self.password)
            require_no_captcha(self._request('GET', urljoin(page.url, 'needCaptcha.html'),
                params={'username': self.student_id, 'pwdEncrypt2': 'pwdEncryptSalt'}))
        fields['username'] = self.student_id
        try:
            response = self._request('POST', action, data=fields,
                headers={'Origin': 'https://uia.njfu.edu.cn', 'Referer': page.url})
            check_login_response(response)
            target = callback_target(response, entry)
            if target:
                check_login_response(self._request('GET', target))
            result = self._json(self._request('GET', 'ic-web/auth/userInfo'))
        except AppError as exc:
            if exc.code in ('network_error', 'school_http'):
                raise AppError('login_uncertain', '学校登录结果未确认，已停止重复登录', 503) from None
            raise
        if result['code'] != 0:
            raise AppError('login_incomplete', '统一认证后未取得座位系统身份，请核对学校认证回调', 401)
        data = result.get('data')
        if not isinstance(data, dict) or str(data.get('logonName')) != self.student_id or not data.get('accNo') or not isinstance(data.get('token'), str) or not data['token']:
            raise AppError('identity_mismatch', '学校返回的身份未通过核对', 502)
        self.identity = Identity(self.student_id, str(data.get('trueName', '')), data['accNo'])
        self._token = data['token']
        # CAS token exchange already issued the school's authenticated cookie.
        # Replacing it with the legacy RSA cookie invalidates record queries.
        return self.identity

    @staticmethod
    def _time(value):
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(float(value)/1000, ZONE).strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo: parsed = parsed.astimezone(ZONE)
                return parsed.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError: pass
        raise AppError('school_format', '学校记录包含无效时间', 502)

    def query(self, begin_date, end_date):
        if not self.identity: raise AppError('not_logged_in', '请先验证学校登录', 401)
        records = []
        for page in range(1, 21):
            result = self._json(self._request('GET', 'ic-web/reserve/resvInfo', params={
                'beginDate': begin_date.isoformat(), 'endDate': end_date.isoformat(), 'needStatus': 6,
                'page': page, 'pageNum': 50, 'orderKey': 'gmt_create', 'orderModel': 'desc'}))
            if result['code'] != 0 or not isinstance(result.get('data'), list):
                raise AppError('query_failed', '学校记录查询失败，请重新验证登录', 502)
            try:
                for row in result['data']:
                    devs = row['resvDevInfoList']
                    if not isinstance(devs, list) or not devs: raise ValueError()
                    # This is a session-bound self-query; preserve an explicit returned identity when present.
                    student = str(row.get('logonName', self.identity.student_id))
                    records.append(SchoolRecord(str(row['uuid']), student, str(devs[0]['devId']),
                        self._time(row['resvBeginTime']), self._time(row['resvEndTime']), row['resvStatus']))
            except (KeyError, TypeError, ValueError, OverflowError):
                raise AppError('school_format', '学校记录结构变化，未作为空记录处理', 502) from None
            if len(result['data']) < 50: return records
        raise AppError('query_limit', '学校记录超过查询上限，未返回不完整结果', 502)

    def cancel(self, record_id):
        """Official 2026-11-06 frontend: POST reserve/delete, JSON {uuid}."""
        if not isinstance(record_id, str) or not 1 <= len(record_id) <= 128 or any(ord(c) < 32 for c in record_id):
            raise AppError('invalid_record', '学校预约编号无效')
        if not self.identity or not self.allow_live or not self.cancellation_guard():
            raise AppError('live_disabled', '真实取消未获确认', 403)
        return self._json(self._request('POST', 'ic-web/reserve/delete', json={'uuid': record_id}))

    def prepare_booking(self):
        """Read the school's current CAPTCHA requirement without reserving."""
        if self._booking_ready:
            return
        result = self._json(self._request('GET', 'ic-web/sysConfig/public'))
        data = result.get('data')
        if result['code'] != 0 or not isinstance(data, list):
            raise AppError('school_protocol', '无法确认学校预约验证要求，未提交预约', 502)
        flags = [row.get('sysValue') for row in data if isinstance(row, dict)
                 and row.get('sysKey') == 'resvCode']
        if len(flags) != 1:
            raise AppError('school_protocol', '学校预约验证配置变化，未提交预约', 502)
        flag = flags[0]
        if type(flag) in (str, int) and str(flag) in ('1', '2'):
            raise AppError('verification_required', '学校预约要求人工验证码，请到官方页面完成预约', 401)
        if not (type(flag) in (str, int) and str(flag) == '0'):
            raise AppError('school_protocol', '无法确认学校预约验证要求，未提交预约', 502)
        self._booking_ready = True

    def _booking_public_key(self):
        result = self._json(self._request('GET', 'ic-web/login/publicKey'))
        if result['code'] != 0 or not isinstance(result.get('data'), dict):
            raise AppError('school_protocol', '学校未提供预约公钥，未提交预约', 502)
        return result['data'].get('publicKey')

    def _seat_fields(self, seat_id):
        from .booking_protocol import seat_fields
        return seat_fields(seat_id, self._booking_public_key())

    def _submission_binding(self, run, seat_id):
        return (run.account_id, run.revision, run.target_date, run.begin, run.end,
                tuple(run.seat_ids), run.due_at, run.kind, run.operation_id, run.expires_at,
                self.identity, self._token, seat_id)

    def prepare_submission(self, run, seat_id):
        """One GET warms independent, single-use payloads for the bounded order."""
        from .booking_protocol import seat_fields
        self._prepared = []
        if not self.identity: raise AppError('not_logged_in', '准备前未完成学校登录', 401)
        self.prepare_booking()
        public_key = self._booking_public_key()
        candidates = candidate_attempts((seat_id,) + tuple(s for s in run.seat_ids if s != seat_id))
        prepared = []
        for seat in candidates:
            encrypted = seat_fields(seat, public_key)
            prepared.append((self._submission_binding(run, seat), elapsed_time(), encrypted))
        self._prepared = prepared

    def _take_prepared(self, run, seat_id):
        cached, self._prepared = self._prepared, []
        binding, now = self._submission_binding(run, seat_id), elapsed_time()
        valid = [p for p in cached if p[0][:-1] == binding[:-1] and 0 <= now-p[1] <= 2]
        for index, payload in enumerate(valid):
            if payload[0] == binding:
                self._prepared = valid[:index] + valid[index+1:]
                return payload[2]
        return None

    def prepared_until(self, run, seat_id):
        """Inspect the first matching payload without consuming or extending it."""
        binding = self._submission_binding(run, seat_id)
        return next((created+2 for key, created, _ in self._prepared if key == binding), None)

    def prepare_scheduled_submission(self, run, seat_id, deadline):
        """Bound prewarming IO without changing ordinary submit/receipt timeouts."""
        self._read_deadline = min(deadline, elapsed_time()+3.5)
        try:
            self.prepare_submission(run, seat_id)
        finally:
            self._read_deadline = None

    def submit(self, run, seat_id):
        if not self.allow_live: raise AppError('live_disabled', '真实预约未开启', 403)
        if not self.identity: raise AppError('not_logged_in', '提交前未完成学校登录', 401)
        from .booking_protocol import safe_message
        started = time.perf_counter()
        encrypted = self._take_prepared(run, seat_id)
        prepared = encrypted is not None
        try:
            if not prepared:
                self.prepare_booking()
                encrypted = self._seat_fields(seat_id)
        except AppError as exc:
            return Submission('rejected', message='请求未发送：'+safe_message(str(exc)), sent=False)
        # _request checks the current guard immediately before the HTTP call.
        timings = {'prepared': prepared, 'prepare_ms': round((time.perf_counter()-started)*1000, 3),
                   'timing_version': 2}
        try:
            result = self._json(self._request('POST', 'ic-web/reserve/seat', request_timings=timings, ownership_run=run, json={
                'testName': '', 'appAccNo': self.identity.acc_no, 'memberKind': 1,
                **encrypted, 'resvMember': [self.identity.acc_no], 'resvProperty': 0, 'sysKind': 8,
                'captcha': '', 'memo': '',
                'resvBeginTime': f'{run.target_date} {run.begin}:00', 'resvEndTime': f'{run.target_date} {run.end}:00'}))
        except AppError as exc:
            if exc.code in {'live_disabled','dual_legacy_fenced','dual_fence_invalid','dual_ownership_denied','dual_drain_pending'}:
                return Submission('rejected', message='请求未发送：已超出提交窗口或执行校验失效', sent=False, timings=timings)
            return Submission('uncertain', message='学校提交结果未确认，请查询记录', timings=timings)
        if result['code'] == 0:
            return Submission('accepted', code=0, message='学校已接收，等待记录核实', sent=True, timings=timings)
        return Submission('rejected', retryable_seat_rejection(result['code'], result.get('message')), result['code'],
                          safe_message(result.get('message')), sent=True, timings=timings)

    def close(self):
        self._prepared = []
        self.http.close()
