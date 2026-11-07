"""Receipt-scoped reminder capabilities; existing cancellation transport owns POSTs."""
import base64
import binascii
from datetime import datetime
import hashlib
import hmac
import json
import math
import re

from .models import AppError, ZONE

RECORD_FIELDS = {'id', 'student_id', 'seat_id', 'begin', 'end', 'status'}
CLAIM_FIELDS = {'version', 'account_id', 'record', 'credential_revision', 'binding_id',
                'outbox_key', 'issued_at', 'expires_at'}
ATTEMPTED = ('dispatching', 'sent', 'unknown')
PRE_SUBMIT_RETRYABLE = {'account_busy', 'release_window', 'execution_paused', 'service_closed'}


def _invalid():
    return AppError('mail_cancellation_invalid', '取消链接已失效，请到学校页面核对预约。', 403)


def _key(value):
    return hmac.new(value, b'reminder-mail-cancellation-v1', hashlib.sha256).digest()


def _record_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def _cancel_id(claims):
    return hashlib.sha256(json.dumps({'account_id': claims['account_id'],
        'record': claims['record']}, sort_keys=True).encode()).hexdigest()


def _claims(value, now):
    try:
        if (not isinstance(value, dict) or set(value) != CLAIM_FIELDS
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['account_id'], str) or not 1 <= len(value['account_id']) <= 128
                or type(value['credential_revision']) is not int or value['credential_revision'] < 0
                or not isinstance(value['binding_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['binding_id'])
                or not isinstance(value['outbox_key'], str) or len(value['outbox_key']) > 512):
            raise ValueError()
        record = value['record']
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise ValueError()
        if (any(not isinstance(record[field], str) or not 1 <= len(record[field]) <= 200
                for field in ('id', 'student_id', 'seat_id'))
                or type(record['status']) is not int or record['status'] != 1027):
            raise ValueError()
        begin, end = (datetime.strptime(record[field], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE)
                      for field in ('begin', 'end'))
        if begin >= end or any(type(value[field]) not in (int, float) or not math.isfinite(value[field])
                               for field in ('issued_at', 'expires_at')):
            raise ValueError()
        if not value['issued_at'] <= now + 5 or not now < value['expires_at'] <= min(begin.timestamp(), value['issued_at'] + 7200):
            raise ValueError()
        expected = 'reminder:' + value['account_id'] + ':' + _record_digest(record)
        if not hmac.compare_digest(value['outbox_key'], expected):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _invalid() from None


def issue_ticket(key, account, preferences, outbox, now):
    """Only reminder payload construction calls this; no ticket is persisted."""
    claims = {'version': 1, 'account_id': account['_id'], 'record': outbox['source'],
        'credential_revision': account['credential_revision'], 'binding_id': preferences['binding_id'],
        'outbox_key': outbox['_id'], 'issued_at': now,
        'expires_at': min(datetime.strptime(outbox['source']['begin'], '%Y-%m-%d %H:%M:%S')
                          .replace(tzinfo=ZONE).timestamp(), now + 7200)}
    _claims(claims, now)
    encoded = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    return encoded + '.' + hmac.new(_key(key), encoded.encode(), hashlib.sha256).hexdigest()


def read_ticket(key, ticket, now):
    try:
        if not isinstance(ticket, str) or not 1 <= len(ticket) <= 8192:
            raise ValueError()
        encoded, signature = ticket.split('.')
        if not re.fullmatch('[A-Za-z0-9_-]+', encoded) or not re.fullmatch('[0-9a-f]{64}', signature):
            raise ValueError()
        expected = hmac.new(_key(key), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        return _claims(json.loads(data), now)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise _invalid() from None


class MailCancellationService:
    def __init__(self, notifications, cancellations, *, clock=None):
        self.notifications, self.cancellations = notifications, cancellations
        self.accounts, self.store, self.key = notifications.accounts, notifications.store, notifications.vault.key
        self.clock = clock or notifications.clock

    def _context(self, ticket):
        claims = read_ticket(self.key, ticket, self.clock())
        account = self.notifications._account(claims['account_id'])
        preferences = self.notifications._preference(claims['account_id'])
        outbox = self.notifications.outbox.find_one({'_id': claims['outbox_key'], 'account_id': claims['account_id']})
        binding = preferences.get('binding_id')
        if (account.get('credential_revision') != claims['credential_revision']
                or account['student_id'] != claims['record']['student_id']
                or preferences.get('verified') is not True or not isinstance(binding, str)
                or not hmac.compare_digest(binding, claims['binding_id']) or not outbox
                or outbox.get('kind') != 'reminder' or outbox.get('status') not in ATTEMPTED
                or outbox.get('source') != claims['record'] or outbox.get('binding_id') != binding):
            raise _invalid()
        return claims, account, outbox

    def _claim(self, claims, outbox):
        value = outbox.get('mail_cancellation')
        if value is not None and (not isinstance(value, dict)
                or value.get('cancel_id') != _cancel_id(claims)
                or value.get('binding_id') != claims['binding_id']
                or value.get('credential_revision') != claims['credential_revision']
                or value.get('target_sha256') != _record_digest(claims['record'])):
            raise _invalid()
        return value

    def _existing(self, claims):
        row = self.cancellations.rows.find_one({'_id': _cancel_id(claims), 'account_id': claims['account_id']})
        if row is not None and row.get('record') != claims['record']:
            raise _invalid()
        return row

    @staticmethod
    def _view(row):
        state = row.get('state')
        if state == 'submitting': state = 'pending'
        if state not in {'not_started', 'pending', 'uncertain', 'success', 'failed'}: state = 'uncertain'
        return {'state': state, 'message': row.get('message') or '取消结果待核实，请勿重复操作。'}

    def _result(self, claims, outbox, *, reconcile=False):
        claim, row = self._claim(claims, outbox), self._existing(claims)
        if row is not None:
            if reconcile and row.get('state') == 'uncertain' and row.get('ack') is True:
                row = self.cancellations.reconcile(claims['account_id'], _cancel_id(claims))
            return self._view(row)
        if claim is None:
            return {'state': 'not_started', 'message': '尚未收到取消请求。'}
        if claim.get('state') == 'pending' and self.clock() - claim['requested_at'] > 180:
            return {'state': 'uncertain', 'message': '取消结果待核实，请勿重复操作。'}
        return self._view(claim)

    def _finish(self, claims, result):
        self.notifications.outbox.update_one({'_id': claims['outbox_key'],
            'mail_cancellation.cancel_id': _cancel_id(claims)}, {'$set': {
                'mail_cancellation.state': result['state'], 'mail_cancellation.message': result['message'],
                'mail_cancellation.completed_at': self.clock()}})
        return result

    def submit(self, ticket):
        claims, account, outbox = self._context(ticket)
        previous = self._claim(claims, outbox)
        if previous is not None and previous.get('state') != 'not_started':
            return self._result(claims, outbox)
        lock = self.accounts.account_lock(claims['account_id'])
        if not lock.acquire(blocking=False):
            raise AppError('account_busy', '此账号正忙，请稍后核对取消状态。', 409)
        try:
            with self.notifications._delivery:
                claims, account, outbox = self._context(ticket)
                previous = self._claim(claims, outbox)
                if previous is not None and previous.get('state') != 'not_started':
                    return self._result(claims, outbox)
                claim = {'cancel_id': _cancel_id(claims), 'binding_id': claims['binding_id'],
                    'credential_revision': claims['credential_revision'], 'target_sha256': _record_digest(claims['record']),
                    'state': 'pending', 'message': '正在取消预约，请勿重复操作。', 'requested_at': self.clock()}
                changed = self.notifications.outbox.update_one({'_id': claims['outbox_key'],
                    'account_id': claims['account_id'], 'kind': 'reminder',
                    **{'source.' + key: value for key, value in claims['record'].items()},
                    'binding_id': claims['binding_id'], 'status': {'$in': list(ATTEMPTED)},
                    'mail_cancellation': previous if previous is not None else {'$exists': False}},
                    {'$set': {'mail_cancellation': claim}})
                if not changed.modified_count:
                    _, _, latest = self._context(ticket)
                    if self._claim(claims, latest) is None: raise _invalid()
                    return self._result(claims, latest)
                existing = self._existing(claims)
                if existing is not None:
                    return self._finish(claims, self._view(existing))
                try:
                    preview = self.cancellations.preview(claims['account_id'], {
                        'record_id': claims['record']['id'], 'expected_revision': account['revision']})
                    if preview.get('record') != claims['record'] or preview.get('cancel_id') != _cancel_id(claims):
                        raise _invalid()
                    self._context(ticket)
                    bounded = self.cancellations._decode(preview['preview_token'])
                    bounded['expires_at'] = min(bounded['expires_at'], claims['expires_at'])
                    result = self.cancellations.submit(claims['account_id'], self.cancellations._encode(bounded))
                    return self._finish(claims, self._view(result))
                except Exception as error:
                    existing = self._existing(claims)
                    if existing is not None: result = self._view(existing)
                    # CancellationService persists its intent before any POST.
                    # Only a definite temporary guard rejection with no intent
                    # permits a fresh user click. Unknown lookup errors stay held.
                    elif isinstance(error, AppError) and error.code in PRE_SUBMIT_RETRYABLE:
                        result = {'state': 'not_started',
                            'message': '后台暂时不能执行取消，尚未发送取消请求。请稍后重新点击原邮件中的“取消预约”。'}
                    else: result = {'state': 'failed' if isinstance(error, AppError) else 'uncertain',
                        'message': '未能取消预约，请到学校页面核对。' if isinstance(error, AppError)
                                   else '取消结果待核实，请勿重复操作。'}
                    return self._finish(claims, result)
        finally:
            lock.release()

    def status(self, ticket):
        claims, _, outbox = self._context(ticket)
        return self._result(claims, outbox, reconcile=True)
