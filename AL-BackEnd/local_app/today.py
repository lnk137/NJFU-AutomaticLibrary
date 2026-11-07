"""Read-only preview and explicit, idempotent same-day booking confirmation."""
import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import secrets
import uuid
from .models import AppError, RunSpec, ZONE, stamp, validate_plan
from .policy import validate_window, closing_end


class TodayBookingService:
    def __init__(self, accounts, reservations, store):
        self.accounts, self.reservations, self.store = accounts, reservations, store
        self._signing_key = secrets.token_bytes(32)
        from .today_batches import TodayBatchService
        self.batches = TodayBatchService(self)

    def _now(self):
        return datetime.fromtimestamp(self.reservations.wall_clock(), ZONE)

    def _validate(self, account_id, data):
        row = self.store.get_account(account_id)
        if type(data.get('expected_revision')) is not int or data['expected_revision'] != row['revision']:
            raise AppError('conflict', '账号配置已变化，请重新载入后预检', 409)
        today = self._now().date().isoformat()
        if data.get('date') != today:
            raise AppError('invalid_date', '当天预约只能选择北京时间今天，请刷新后重试')
        plan = validate_plan({'seat_ids': data.get('seat_ids'), 'day_offset': 1, 'mode': 'day',
                              'begin': data.get('begin'), 'end': data.get('end'),
                              'trigger': '07:00:00', 'enabled': True}, self.accounts.catalog,
                             single_date=self._now().date())
        try:
            plan=replace(plan,end=closing_end(plan.end,self._now().isoweekday()))
            validate_window(plan.begin, plan.end, self._now().isoweekday())
            if any(int(value[-2:]) % 30 for value in (plan.begin, plan.end)):
                raise ValueError('当天预约请选择整点或半点时段')
            start = datetime.fromisoformat(f'{today}T{plan.begin}').replace(tzinfo=ZONE).timestamp()
            if start <= self.reservations.wall_clock():
                raise ValueError('开始时间已过，请选择今天尚未开始的时段')
        except ValueError as exc:
            raise AppError('invalid_time', str(exc)) from None
        return row, today, plan, start

    def _check_school(self, row, day, begin, end):
        account_id = row['_id']
        if self.batches.active(account_id):
            raise AppError('account_busy', '此账号有正在执行的当天批次，请先查看结果', 409)
        if account_id in self.accounts.busy:
            raise AppError('account_busy', '此账号正在执行预约，请稍后再试', 409)
        pending = self.store.runs.find_one({'account_id': account_id, 'target_date': day,
                                            'status': {'$in': ['preparing', 'submitting', 'uncertain']}})
        if pending:
            raise AppError('booking_uncertain', '该账号当天有待确认任务，请先核对学校记录', 409)
        client = self.accounts.client(account_id)
        prepare = getattr(client, 'prepare_booking', None)
        if prepare:
            prepare()
        query_day = datetime.fromisoformat(day).date()
        # Keep the shared snapshot's ordinary today-through-three-days range.
        records = client.query(query_day, query_day + timedelta(days=3))
        self.store.save_snapshot(account_id, row['revision'], records, stamp())
        start, finish = f'{day} {begin}:00', f'{day} {end}:00'
        for record in records:
            if record.student_id != row['student_id']:
                raise AppError('identity_mismatch', '学校查询身份异常，未继续预约', 502)
            if record.begin < finish and record.end > start:
                raise AppError('booking_overlap', '学校记录中已有重叠时段预约，请先在官方页面处理', 409)

    def _encode(self, claims):
        encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')
        signature = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + '.' + signature

    def _decode(self, token):
        try:
            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError()
            encoded, signature = token.split('.')
            expected = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
            if not isinstance(claims, dict) or set(claims) != {'account_id', 'revision', 'target_date',
                    'begin', 'end', 'seat_ids', 'operation_id', 'expires_at'}:
                raise ValueError()
            return claims
        except (ValueError, TypeError, UnicodeError):
            raise AppError('invalid_preview', '预约预检已失效，请重新预检', 400) from None

    def preview(self, account_id, data):
        with self.accounts.account_lock(account_id):
            row, day, plan, start = self._validate(account_id, data)
            self._check_school(row, day, plan.begin, plan.end)
            expiry = min(self.reservations.wall_clock() + 120, start - 1)
            if expiry <= self.reservations.wall_clock():
                raise AppError('invalid_time', '开始时间过近，请选择下一时段')
            claims = {'account_id': account_id, 'revision': row['revision'], 'target_date': day,
                      'begin': plan.begin, 'end': plan.end, 'seat_ids': list(plan.seat_ids),
                      'operation_id': uuid.uuid4().hex, 'expires_at': expiry}
            return {'date': day, 'begin': plan.begin, 'end': plan.end,
                    'seats': [asdict(self.accounts.catalog[key]) for key in plan.seat_ids],
                    'expires_at': datetime.fromtimestamp(expiry, ZONE).isoformat(),
                    'preview_token': self._encode(claims)}

    @staticmethod
    def _existing(row):
        return {'run_id': row['_id'], 'result': row.get('result') or {
            'status': row['status'], 'message': row.get('message', '任务已处理'), 'records': []}}

    def submit(self, account_id, token):
        claims = self._decode(token)
        if claims['account_id'] != account_id:
            raise AppError('invalid_preview', '预检账号不匹配，请重新预检', 403)
        run = RunSpec(**claims, due_at=self.reservations.wall_clock(), kind='manual')
        with self.accounts.account_lock(account_id):
            existing = self.store.get_run(run.key)
            if existing and existing['status'] not in ('queued', 'preparing'):
                return self._existing(existing)
            if not self.reservations.allow_live:
                raise AppError('live_disabled', '真实预约执行未开启，请使用正式启动入口', 403)
            if not self.reservations.guard():
                raise AppError('execution_paused', '后台时间校验或执行状态异常，请检查后台', 503)
            if claims['expires_at'] <= self.reservations.wall_clock():
                raise AppError('preview_expired', '预检已过期，请重新检查当天预约', 409)
            row, day, plan, _ = self._validate(account_id, {
                'expected_revision': claims['revision'], 'date': claims['target_date'],
                'seat_ids': claims['seat_ids'], 'begin': claims['begin'], 'end': claims['end']})
            self._check_school(row, day, plan.begin, plan.end)
            self.accounts.busy.add(account_id)
            try:
                result = self.reservations.execute(run)
                return {'run_id': run.key, 'result': asdict(result)}
            finally:
                self.accounts.busy.discard(account_id)

    def close(self):
        self.batches.close()
