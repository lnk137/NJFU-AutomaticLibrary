"""Explicit receipt-bound cancellation, with durable protection against replay."""
import base64
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from pymongo.errors import DuplicateKeyError
from .models import AppError, ZONE


class CancellationService:
    def __init__(self, accounts, reservations, store, *, quiet_window=None):
        self.accounts, self.reservations, self.store = accounts, reservations, store
        self.quiet_window, self.closed = quiet_window, False
        self.rows = store.db.campus_cancellations
        self.rows.create_index([('account_id', 1), ('requested_at', -1)])
        self.key = hmac.new(accounts.vault.key, b'cancellation-preview-v1', hashlib.sha256).digest()
        self.rows.update_many({'state': 'submitting'}, {'$set': {
            'state': 'uncertain', 'message': '取消过程曾中断，请刷新学校记录核对，未重复取消'}})

    def _now(self): return self.reservations.wall_clock()

    def _guard(self, account_id, revision, *, live=False):
        if self.closed: raise AppError('service_closed', '取消服务已关闭', 503)
        if self.quiet_window: self.quiet_window()
        row = self.store.get_account(account_id)
        if type(revision) is not int or row['revision'] != revision:
            raise AppError('conflict', '账号配置已变化，请重新核对预约', 409)
        if not self.accounts.is_allowed(row['student_id']):
            raise AppError('access_denied', '此账号的操作权限已停用', 403)
        if account_id in self.accounts.busy: raise AppError('account_busy', '此账号正在预约，请稍后核对', 409)
        if live and not self.reservations.allow_live:
            raise AppError('live_disabled', '真实操作未开启，未取消学校预约', 403)
        if not self.reservations.guard(): raise AppError('execution_paused', '后台时间或执行状态需检查', 503)
        return row

    def _query(self, row):
        client = self.accounts.client(row['_id'])
        day = datetime.fromtimestamp(self._now(), ZONE).date()
        records = client.query(day, day + timedelta(days=3))
        if not isinstance(records, list) or any(r.student_id != row['student_id'] for r in records):
            raise AppError('identity_mismatch', '学校记录身份不一致，未继续操作', 502)
        self.store.save_snapshot(row['_id'], row['revision'], records, datetime.fromtimestamp(self._now(), ZONE).isoformat())
        return client, records

    def _selected(self, records, record_id):
        matches = [r for r in records if r.id == record_id]
        if len(matches) != 1: raise AppError('record_changed', '预约记录已变化，请重新查询', 409)
        record = matches[0]
        try:
            begin, end = (datetime.strptime(v, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE)
                          for v in (record.begin, record.end))
            if type(record.status) is not int or record.status != 1027 or begin >= end or begin.timestamp() <= self._now(): raise ValueError()
            if not record.id or not record.seat_id: raise ValueError()
        except (TypeError, ValueError):
            raise AppError('not_cancellable', '当前记录不适合取消，请到学校页面核对', 409) from None
        return asdict(record), begin.timestamp()

    def _encode(self, claims):
        raw = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
        return raw + '.' + hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()

    def _decode(self, token):
        try:
            if not isinstance(token, str) or len(token) > 8192: raise ValueError()
            raw, signature = token.split('.')
            if not hmac.compare_digest(signature, hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()): raise ValueError()
            claims = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True))
            if set(claims) != {'account_id', 'revision', 'credential_revision', 'record', 'cancel_id', 'expires_at'}: raise ValueError()
            return claims
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise AppError('invalid_preview', '取消确认已失效，请重新核对预约') from None

    def preview(self, account_id, data):
        if not isinstance(data, dict) or set(data) != {'record_id', 'expected_revision'} or not isinstance(data['record_id'], str):
            raise AppError('invalid_cancellation', '请选择一条具体预约')
        lock = self.accounts.account_lock(account_id)
        if not lock.acquire(blocking=False): raise AppError('account_busy', '此账号正忙，请稍后核对', 409)
        try:
            row = self._guard(account_id, data['expected_revision'])
            _, records = self._query(row)
            record, start = self._selected(records, data['record_id'])
            identity = json.dumps({'account_id': account_id, 'record': record}, sort_keys=True).encode()
            claims = {'account_id': account_id, 'revision': row['revision'], 'credential_revision': row['credential_revision'],
                      'record': record, 'cancel_id': hashlib.sha256(identity).hexdigest(), 'expires_at': min(self._now() + 120, start)}
            return {'cancel_id': claims['cancel_id'], 'record': record, 'expires_at': claims['expires_at'], 'preview_token': self._encode(claims)}
        finally: lock.release()

    def _view(self, row):
        return {key: row.get(key) for key in ('account_id', 'state', 'record', 'message', 'requested_at', 'completed_at')} | {'cancel_id': row['_id']}

    def _confirm_readback(self, doc, records):
        record = doc['record']
        exact = [asdict(r) for r in records if r.id == record['id']]
        inactive = not exact
        if doc.get('ack') and inactive:
            self.rows.update_one({'_id': doc['_id'], 'state': 'uncertain', 'ack': True}, {'$set': {
                'state': 'success', 'message': '学校已确认取消本段预约', 'completed_at': self._now()}})

    def reconcile(self, account_id, cancel_id):
        doc = self.rows.find_one({'_id': cancel_id, 'account_id': account_id})
        if not doc: raise AppError('not_found', '取消记录不存在', 404)
        if doc['state'] != 'uncertain' or not doc.get('ack'): return self._view(doc)
        begin = datetime.strptime(doc['record']['begin'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE)
        now = datetime.fromtimestamp(self._now(), ZONE)
        if not now.date() <= begin.date() <= now.date() + timedelta(days=3) or begin.timestamp() <= self._now():
            return self._view(doc)
        with self.accounts.account_lock(account_id):
            row = self.store.get_account(account_id)
            self._guard(account_id, row['revision'])
            _, records = self._query(row)
            self._confirm_readback(doc, records)
        return self.get(account_id, cancel_id)

    def get(self, account_id, cancel_id):
        row = self.rows.find_one({'_id': cancel_id, 'account_id': account_id})
        if not row: raise AppError('not_found', '取消记录不存在', 404)
        return self._view(row)

    def submit(self, account_id, preview_token):
        claims = self._decode(preview_token)
        if claims['account_id'] != account_id: raise AppError('invalid_preview', '取消确认账号不匹配', 403)
        existing = self.rows.find_one({'_id': claims['cancel_id'], 'account_id': account_id})
        if existing: return self._view(existing)
        lock = self.accounts.account_lock(account_id)
        if not lock.acquire(blocking=False): raise AppError('account_busy', '此账号正忙，请稍后核对', 409)
        try:
            row = self._guard(account_id, claims['revision'], live=True)
            if row['credential_revision'] != claims['credential_revision'] or self._now() >= claims['expires_at']:
                raise AppError('preview_expired', '取消确认已过期，请重新核对', 409)
            client, records = self._query(row)
            record, start = self._selected(records, claims['record']['id'])
            if record != claims['record']: raise AppError('record_changed', '预约内容已变化，未取消', 409)
            def final_guard():
                latest = self._guard(account_id, claims['revision'], live=True)
                return latest['credential_revision'] == claims['credential_revision'] and self._now() < min(start, claims['expires_at'])
            if not final_guard(): raise AppError('preview_expired', '取消确认已过期，请重新核对', 409)
            doc = {'_id': claims['cancel_id'], 'account_id': account_id, 'record': record, 'state': 'submitting',
                   'requested_at': self._now(), 'ack': False, 'message': '正在取消本段预约'}
            try: self.rows.insert_one(doc)
            except DuplicateKeyError: return self.get(account_id, claims['cancel_id'])
            old_live, old_guard = client.allow_live, getattr(client, 'cancellation_guard', None)
            try:
                client.allow_live, client.cancellation_guard = True, final_guard
                response = client.cancel(record['id'])
                if not isinstance(response, dict) or type(response.get('code')) is not int: raise ValueError()
                if response['code'] != 0:
                    self.rows.update_one({'_id': doc['_id']}, {'$set': {'state': 'failed', 'message': '学校未接受取消，请到学校页面核对'}})
                else:
                    self.rows.update_one({'_id': doc['_id']}, {'$set': {'ack': True, 'state': 'uncertain', 'message': '学校已接收取消，正在核对结果'}})
                    _, readback = self._query(self.store.get_account(account_id))
                    self._confirm_readback(self.rows.find_one({'_id': doc['_id']}), readback)
            except Exception:
                self.rows.update_one({'_id': doc['_id']}, {'$set': {'state': 'uncertain', 'message': '取消结果待核实，请刷新学校记录；未重复取消'}})
            finally:
                client.allow_live = old_live
                if old_guard is None: delattr(client, 'cancellation_guard')
                else: client.cancellation_guard = old_guard
            return self.get(account_id, doc['_id'])
        finally: lock.release()

    def close(self): self.closed = True
