"""Opt-in email from persisted evidence; never logs in or submits to school."""
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlencode
import uuid
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .mail_config import SMTPSender, email_address, load_mail_config
from .mail_cancellation import issue_ticket
from .models import AppError, RunSpec, ZONE
from .dual_node.receipt_view import is_dual_row, receipt_view, progress_view

CHALLENGE_SECONDS = 600
MAX_SNAPSHOT_SECONDS = 36 * 3600
REMINDER_GRACE_SECONDS = 300
QUIET_SECONDS = 120


def _epoch(value):
    try:
        result = datetime.fromisoformat(value)
        return (result.replace(tzinfo=ZONE) if result.tzinfo is None else result).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _masked(address):
    user, domain = address.rsplit('@', 1)
    return user[0] + '***@' + domain


class NotificationService:
    def __init__(self, settings, accounts, store, vault, *, sender=None, clock=None, quiet_window=None):
        self.settings, self.accounts, self.store, self.vault = settings, accounts, store, vault
        self.clock = clock or time.time
        self.quiet_window = quiet_window
        self.preferences = store.db.campus_notification_settings
        self.challenges = store.db.campus_notification_challenges
        self.rates = store.db.campus_notification_rates
        self.outbox = store.db.campus_notification_outbox
        self.outbox.create_index([('status', 1), ('due_at', 1)])
        self.config, self.config_error = load_mail_config(settings.data_dir)
        self.sender = sender or (SMTPSender(self.config) if self.config else None)
        self._stop = threading.Event()
        self._thread = None
        self._delivery = threading.RLock()
        self._poll = threading.Lock()
        self._worker_error = None
        self._preference_cursor = None

    def status(self):
        return {'configured': self.sender is not None, 'enabled': self.sender is not None,
                'error': self.config_error or self._worker_error,
                'sender_masked': _masked(self.config.from_address) if self.config else None,
                'running': bool(self._thread and self._thread.is_alive())}

    def _account(self, account_id):
        account = self.store.get_account(account_id)
        if not self.accounts.is_allowed(account['student_id']):
            raise AppError('access_denied', '此账号的预约权限已停用', 403)
        return account

    def _preference(self, account_id):
        return self.preferences.find_one({'_id': account_id}) or {
            '_id': account_id, 'revision': 0, 'verified': False, 'address_masked': None,
            'results_enabled': False, 'reminders_enabled': False, 'reminder_minutes': 30}

    def _ensure(self, account_id):
        row = self._preference(account_id)
        try:
            self.preferences.update_one({'_id': account_id}, {'$setOnInsert': row}, upsert=True)
        except DuplicateKeyError:
            pass
        return self._preference(account_id)

    def get_settings(self, account_id):
        self._account(account_id)
        row = self._preference(account_id)
        return {'account_id': account_id, **{key: row.get(key) for key in (
            'revision', 'verified', 'address_masked', 'results_enabled', 'reminders_enabled', 'reminder_minutes')},
            'configured': self.sender is not None}

    def _require_sender(self):
        if self.sender is None:
            raise AppError('mail_unavailable', '邮件服务尚未配置，请联系管理员', 503)

    def _rate(self, key):
        now = self.clock()
        row = self.rates.find_one({'_id': key})
        recent = [item for item in (row or {}).get('times', []) if now - item < 3600]
        if recent and (now - recent[-1] < 60 or len(recent) >= 5):
            raise AppError('verification_rate_limited', '验证码发送过于频繁，请稍后再试', 429)
        query = {'_id': key, 'revision': row['revision']} if row else {'_id': key, 'revision': {'$exists': False}}
        try:
            changed = self.rates.update_one(query, {'$set': {'times': recent + [now]}, '$inc': {'revision': 1}}, upsert=not row)
        except DuplicateKeyError:
            raise AppError('verification_rate_limited', '验证码发送过于频繁，请稍后再试', 429) from None
        if not changed.matched_count and not changed.upserted_id:
            raise AppError('verification_rate_limited', '验证码发送过于频繁，请稍后再试', 429)

    def request_verification(self, account_id, data):
        self._account(account_id)
        self._require_sender()
        if not isinstance(data, dict) or set(data) != {'address'}:
            raise AppError('invalid_email', '请填写接收邮箱')
        try:
            address = email_address(data['address'])
        except ValueError:
            raise AppError('invalid_email', '请填写有效邮箱地址') from None
        now = self.clock()
        with self._delivery:
            self._rate('account:' + account_id)
            address_key = hmac.new(self.vault.key, address.lower().encode(), hashlib.sha256).hexdigest()
            self._rate('address:' + address_key)
            row = self._ensure(account_id)
            challenge_id, salt = uuid.uuid4().hex, secrets.token_hex(16)
            code = f'{secrets.randbelow(1000000):06d}'
            digest = hmac.new(self.vault.key, (challenge_id + salt + code).encode(), hashlib.sha256).hexdigest()
            address_secret = self.vault.seal('notification-address:' + account_id, address)
            self.challenges.replace_one({'_id': account_id}, {
                '_id': account_id, 'challenge_id': challenge_id, 'revision': row['revision'],
                'digest': digest, 'salt': salt, 'address': address_secret, 'address_key': address_key,
                'address_masked': _masked(address), 'attempts': 0, 'expires_at': now + CHALLENGE_SECONDS}, upsert=True)
            payload = {'to': address, 'subject': '验证图书馆邮件通知邮箱',
                       'body': f'您的邮箱验证码：{code}\n10 分钟内有效。如非本人操作，请忽略此邮件。'}
            self._queue('verification:' + challenge_id, account_id, 'verification', now,
                        now + CHALLENGE_SECONDS, {'challenge_id': challenge_id}, payload)
        return self.get_settings(account_id) | {'challenge_id': challenge_id,
                'expires_at': now + CHALLENGE_SECONDS, 'retry_after': 60}

    def confirm_verification(self, account_id, data):
        self._account(account_id)
        if (not isinstance(data, dict) or set(data) != {'challenge_id', 'code'}
                or not isinstance(data.get('challenge_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', data['challenge_id'])
                or not isinstance(data.get('code'), str) or not re.fullmatch(r'[0-9]{6}', data['code'])):
            raise AppError('invalid_verification', '请填写有效验证码')
        with self._delivery:
            challenge = self.challenges.find_one_and_update({'_id': account_id,
                'challenge_id': data['challenge_id'], 'expires_at': {'$gt': self.clock()},
                'attempts': {'$lt': 5}, 'consumed': {'$ne': True}}, {'$inc': {'attempts': 1}},
                return_document=ReturnDocument.AFTER)
            if not challenge:
                raise AppError('verification_expired', '验证码已失效，请重新获取')
            digest = hmac.new(self.vault.key, (data['challenge_id'] + challenge['salt'] + data['code']).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(digest, challenge['digest']):
                raise AppError('invalid_verification', '验证码错误')
            changed = self.preferences.update_one({'_id': account_id, 'revision': challenge['revision']},
                {'$set': {'verified': True, 'address': challenge['address'], 'address_key': challenge['address_key'],
                          'address_masked': challenge['address_masked'], 'binding_id': data['challenge_id'],
                          'verified_at': self.clock(), 'results_enabled': False, 'reminders_enabled': False},
                 '$inc': {'revision': 1}})
            self.challenges.update_one({'_id': account_id, 'challenge_id': data['challenge_id']}, {'$set': {'consumed': True}})
            if not changed.matched_count:
                raise AppError('notification_conflict', '通知设置已变化，请重新读取并验证邮箱', 409)
        return self.get_settings(account_id)

    def _queue(self, key, account_id, kind, due, expires, source, payload=None, binding_id=None, *, handoff_from=()):
        row = {'_id': key, 'account_id': account_id, 'kind': kind, 'due_at': due,
               'expires_at': expires, 'status': 'pending', 'source': source, 'binding_id': binding_id}
        if payload is not None:
            row['payload'] = self.vault.seal('notification-outbox:' + key, json.dumps(payload, ensure_ascii=False))
        try:
            self.outbox.update_one({'_id': key}, {'$setOnInsert': row}, upsert=True)
        except DuplicateKeyError:
            pass
        if handoff_from:
            current = self.outbox.find_one({'_id': key})
            if (current and current.get('status') == 'cancelled' and 'attempted_at' not in current
                    and current.get('account_id') == account_id and current.get('kind') == kind
                    and current.get('source') == source
                    and current.get('reminder_alias_released_to') in handoff_from):
                self.outbox.update_one({'_id': key, 'status': 'cancelled',
                    'account_id': account_id, 'kind': kind, 'source': source,
                    'attempted_at': {'$exists': False},
                    'reminder_alias_released_to': current['reminder_alias_released_to']},
                    {'$set': {name: value for name, value in row.items() if name != '_id'},
                     '$unset': {'reminder_alias_released_to': ''}})

    def _quiet(self):
        now = self.clock()
        if self.quiet_window and self.quiet_window():
            return True
        if self.store.runs.find_one({'spec.due_at': {'$gte': now - QUIET_SECONDS, '$lte': now + QUIET_SECONDS},
                                      'status': {'$in': ['queued', 'preparing', 'submitting', 'uncertain']}}):
            return True
        today = datetime.fromtimestamp(now, ZONE).date()
        for account in self.store.accounts.find({'plan.enabled': True}, {'plan.trigger': 1}).limit(200):
            trigger = account.get('plan', {}).get('trigger')
            if isinstance(trigger, str) and re.fullmatch(r'\d{2}:\d{2}:\d{2}', trigger):
                for delta in (-1, 0, 1):
                    due = _epoch(f'{today + timedelta(days=delta)}T{trigger}')
                    if due is not None and abs(due - now) <= QUIET_SECONDS:
                        return True
        return False

    @staticmethod
    def _record(record, account):
        if (not isinstance(record, dict) or record.get('student_id') != account['student_id']
                or type(record.get('status')) is not int or record['status'] != 1027
                or not isinstance(record.get('id'), str) or not 1 <= len(record['id']) <= 200
                or not isinstance(record.get('seat_id'), str) or not record['seat_id']):
            return False
        begin, end = _epoch(record.get('begin')), _epoch(record.get('end'))
        return begin is not None and end is not None and begin < end

    def _receipt(self, account, run):
        result, spec = run.get('result') or {}, run.get('spec') or {}
        if result.get('status') != 'success' or _epoch(result.get('verified_at')) is None:
            return None
        records = result.get('records', [])
        if not isinstance(records, list):
            return None
        matches = [record for record in records if self._record(record, account)
            and record['seat_id'] == run.get('attempted_seat_id')
            and record['begin'] == f"{spec.get('target_date')} {spec.get('begin')}:00"
            and record['end'] == f"{spec.get('target_date')} {spec.get('end')}:00"]
        return matches[0] if len(matches) == 1 else None

    def _non_submission(self, account, run):
        """Classify only bound terminal evidence; never expose exception text."""
        if is_dual_row(run):
            view = progress_view(account, run)
            return bool(view and view.status == 'skipped' and view.phase in
                        ('cancelled', 'expired_unissued', 'not_sent'))
        if (run.get('status') not in ('failed', 'skipped') or run.get('attempted_seat_id')
                or run.get('submitted_at') is not None or run.get('attempts')):
            return False
        try:
            spec = RunSpec(**run['spec'])
            return (spec.account_id == account['_id'] and spec.key == run['_id']
                    and spec.target_date == run.get('target_date')
                    and type(spec.revision) is int and spec.revision >= 1)
        except (TypeError, KeyError, ValueError):
            return False

    def _result_eligible(self, account, prefs, run):
        spec, state = run.get('spec') or {}, run.get('status')
        if (spec.get('kind', 'scheduled') not in ('scheduled', 'manual') or spec.get('account_id') != account['_id']
                or type(spec.get('due_at')) not in (int, float) or type(run.get('created')) not in (int, float)
                or max(spec['due_at'], run.get('created', 0)) < prefs.get('results_since', self.clock())
                or state not in ('success', 'failed', 'uncertain', 'skipped')):
            return False
        if is_dual_row(run):
            return receipt_view(account, run) is not None or self._non_submission(account, run)
        if state == 'skipped':
            return self._non_submission(account, run)
        if state == 'success':
            return self._receipt(account, run) is not None
        if state == 'failed':
            return self._non_submission(account, run) or any(item.get('outcome') == 'rejected' and type(item.get('code')) is int
                       for item in run.get('attempts', []) if isinstance(item, dict))
        return bool(run.get('attempted_seat_id') and run.get('submitted_at'))

    def _cancelled(self, account_id, record):
        return bool(self.store.db.campus_cancellations.find_one({'account_id': account_id,
            'record.id': record['id'], 'record.student_id': record['student_id'],
            'record.seat_id': record['seat_id'], 'record.begin': record['begin'], 'record.end': record['end'],
            'state': 'success', 'ack': True}))

    def _dual_cancelled(self, account, view):
        rows = list(self.store.db.campus_cancellations.find({'account_id': account['_id'],
            'record.begin': f'{view.spec.target_date} {view.spec.begin}:00',
            'record.end': f'{view.spec.target_date} {view.spec.end}:00',
            'record.seat_id': view.seat_id, 'state': 'success', 'ack': True}).limit(1001))
        if len(rows) > 1000:
            return True
        return any(row.get('ack') is True
            and type(row.get('completed_at')) in (int, float) and math.isfinite(row['completed_at'])
            and view.matches_record(account, row.get('record')) for row in rows)

    def _dual_receipts(self, account):
        now = self.clock()
        today = datetime.fromtimestamp(now, ZONE).date()
        rows = self.store.runs.find({'account_id': account['_id'], 'status': 'success',
            'dual_binding.mode': 'production',
            'target_date': {'$gte': today.isoformat(), '$lte': (today+timedelta(days=1)).isoformat()}}).sort('dual_result.result_at', -1).limit(50)
        values = []
        for row in rows:
            view = receipt_view(account, row)
            if view and view.phase == 'completed':
                values.append((row, view))
        return values

    def _dual_reminder_due(self, prefs, view):
        now = self.clock()
        minutes = prefs.get('reminder_minutes')
        if type(minutes) is not int or not 1 <= minutes <= 1440 or not 0 <= now-view.result_at <= MAX_SNAPSHOT_SECONDS:
            return None
        begin = _epoch(f'{view.spec.target_date} {view.spec.begin}:00')
        due = begin-minutes*60
        if due < prefs.get('reminders_since', now) or not due <= now < min(begin, due+REMINDER_GRACE_SECONDS):
            return None
        return due, begin

    @staticmethod
    def _reminder_key(account_id, record):
        identity = {key: record[key] for key in ('id', 'student_id', 'seat_id', 'begin', 'end', 'status')}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return 'reminder:' + account_id + ':' + digest

    @staticmethod
    def _dual_reminder_key(account_id, view):
        return 'reminder:dual:' + account_id + ':' + view.task_id + ':' + view.receipt_ref

    def _reminder_donor_blocks(self, event, destination):
        """Hand off only mail that durably never reached an SMTP attempt."""
        if ('attempted_at' in event or event.get('status') not in ('pending', 'cancelled')):
            return True
        released = event.get('reminder_alias_released_to')
        if released is not None:
            return event['status'] != 'cancelled' or released != destination
        changed = self.outbox.update_one({'_id': event['_id'], 'status': event['status'],
            'kind': event['kind'], 'account_id': event['account_id'], 'source': event['source'],
            'attempted_at': {'$exists': False}, 'reminder_alias_released_to': {'$exists': False}},
            {'$set': {'status': 'cancelled', 'reminder_alias_released_to': destination}})
        return not (changed.acknowledged and changed.modified_count == 1)

    def _dual_reminder_blocks_snapshot(self, account, view, record, handoffs):
        event = self.outbox.find_one({'_id': self._dual_reminder_key(account['_id'], view)})
        if event is None:
            return False
        source = {'run_id': view.spec.key, 'task_id': view.task_id,
                  'receipt_ref': view.receipt_ref, 'grant_id': view.grant_id}
        if (event.get('account_id') != account['_id'] or event.get('kind') != 'dual_reminder'
                or event.get('source') != source):
            return True
        blocked = self._reminder_donor_blocks(event, self._reminder_key(account['_id'], record))
        if not blocked:
            handoffs.append(event['_id'])
        return blocked

    def _queue_dual_reminder(self, account, prefs, row, view):
        # A real, digest-matched school receipt can carry the existing exact
        # cancellation capability. Keep it ahead of the UUID-free proof path.
        if any(view.matches_record(account, record) for record in self._fresh_records(account)):
            return
        timing = self._dual_reminder_due(prefs, view)
        if timing is None:
            return
        due, begin = timing
        key = self._dual_reminder_key(account['_id'], view)
        # Snapshot outbox sources outlive cache refreshes. Attempted donors hold
        # delivery permanently; only an unattempted donor may hand off by CAS.
        previous = list(self.outbox.find({'account_id': account['_id'], 'kind': 'reminder',
            'source.begin': f'{view.spec.target_date} {view.spec.begin}:00',
            'source.end': f'{view.spec.target_date} {view.spec.end}:00',
            'source.seat_id': view.seat_id}).limit(1001))
        duplicate, handoffs = len(previous) > 1000, []
        for previous_row in previous:
            if view.matches_record(account, previous_row.get('source')):
                if self._reminder_donor_blocks(previous_row, key):
                    duplicate = True
                else:
                    handoffs.append(previous_row['_id'])
        if duplicate:
            return
        self._queue(key, account['_id'], 'dual_reminder', due, min(begin, due+REMINDER_GRACE_SECONDS),
            {'run_id': row['_id'], 'task_id': view.task_id, 'receipt_ref': view.receipt_ref, 'grant_id': view.grant_id},
            binding_id=prefs.get('binding_id'), handoff_from=handoffs)

    def _fresh_records(self, account):
        checked = _epoch(account.get('snapshot_at'))
        if (checked is None or not 0 <= self.clock() - checked <= MAX_SNAPSHOT_SECONDS
                or account.get('query_error') or account.get('login_error')
                or (account.get('identity') or {}).get('student_id') != account['student_id']):
            return []
        records = account.get('snapshot', [])
        if not isinstance(records, list):
            return []
        if len(records) > 500:
            return []
        if any(not self._record(record, account) for record in records):
            return []
        if len({record['id'] for record in records}) != len(records):
            return []
        windows = sorted((_epoch(record['begin']), _epoch(record['end'])) for record in records)
        if any(end > following for (_, end), (following, _) in zip(windows, windows[1:])):
            return []
        return [record for record in records if not self._cancelled(account['_id'], record)]

    def _preference_page(self):
        query = {'verified': True, '$or': [{'results_enabled': True}, {'reminders_enabled': True}]}
        if self._preference_cursor is not None:
            query['_id'] = {'$gt': self._preference_cursor}
        page = list(self.preferences.find(query).sort('_id', 1).limit(200))
        if not page and self._preference_cursor is not None:
            self._preference_cursor = None
            query.pop('_id')
            page = list(self.preferences.find(query).sort('_id', 1).limit(200))
        if page:
            self._preference_cursor = page[-1]['_id']
        return page

    def _discover(self):
        now = self.clock()
        for prefs in self._preference_page():
            if self._stop.is_set() or self._quiet():
                return
            try:
                account = self._account(prefs['_id'])
            except AppError:
                continue
            if prefs.get('results_enabled'):
                runs = self.store.runs.find({'account_id': account['_id'],
                    'status': {'$in': ['success', 'failed', 'uncertain', 'skipped']},
                    '$or': [{'spec.due_at': {'$gte': prefs.get('results_since', now)}},
                            {'created': {'$gte': prefs.get('results_since', now)}}]}).sort('created', -1).limit(50)
                for run in runs:
                    if self._result_eligible(account, prefs, run):
                        key = 'result:' + account['_id'] + ':' + run['_id'] + ':' + run['status']
                        self._queue(key, account['_id'], 'result', now,
                                    min(now + 3600, max(run.get('created', now), run['spec']['due_at']) + 86400),
                                    {'run_id': run['_id'], 'state': run['status']}, binding_id=prefs.get('binding_id'))
                for cancellation in self.store.db.campus_cancellations.find({'account_id': account['_id'],
                        'state': 'success', 'ack': True, 'completed_at': {'$gte': prefs.get('results_since', now)}}).limit(50):
                    if self._record(cancellation.get('record'), account):
                        self._queue('cancel:' + account['_id'] + ':' + cancellation['_id'] + ':success',
                                    account['_id'], 'cancellation', now, cancellation['completed_at'] + 3600,
                                    {'cancellation_id': cancellation['_id']}, binding_id=prefs.get('binding_id'))
            if prefs.get('reminders_enabled'):
                dual_receipts = self._dual_receipts(account)
                for record in self._fresh_records(account):
                    matches = [view for _, view in dual_receipts if view.matches_record(account, record)]
                    handoffs = []
                    if any(self._dual_reminder_blocks_snapshot(account, view, record, handoffs) for view in matches):
                        continue
                    begin = _epoch(record['begin'])
                    due = begin - prefs['reminder_minutes'] * 60
                    if due < prefs.get('reminders_since', now) or not due <= now < min(begin, due + REMINDER_GRACE_SECONDS):
                        continue
                    identity = {key: record[key] for key in ('id', 'student_id', 'seat_id', 'begin', 'end', 'status')}
                    self._queue(self._reminder_key(account['_id'], record), account['_id'], 'reminder', due,
                                min(begin, due + REMINDER_GRACE_SECONDS), identity, binding_id=prefs.get('binding_id'),
                                handoff_from=handoffs)
                for run, view in dual_receipts:
                    self._queue_dual_reminder(account, prefs, run, view)

    def _url(self, account_id, record_id=None, *, intent=None, local_notice=True):
        origin = self.settings.public_origin or f'http://127.0.0.1:{self.settings.port}'
        query = {'account_id': account_id}
        if record_id:
            query['record_id'] = record_id
        if intent:
            query['intent'] = intent
        url = origin + '/#/pages/reservation_info/reservation_info?' + urlencode(query)
        return url + ('' if self.settings.public_origin or not local_notice else '\n此链接仅能在运行预约后台的电脑上打开。')

    def _payload(self, row):
        now = self.clock()
        if now >= row['expires_at']:
            return None
        account = self._account(row['account_id'])
        source = row['source']
        if row['kind'] == 'verification':
            challenge = self.challenges.find_one({'_id': row['account_id'], 'challenge_id': source['challenge_id'],
                'expires_at': {'$gt': now}, 'consumed': {'$ne': True}, 'attempts': {'$lt': 5}})
            if challenge is None:
                return None
            return json.loads(self.vault.unseal('notification-outbox:' + row['_id'], row['payload']))
        prefs = self._preference(row['account_id'])
        if not prefs.get('verified') or prefs.get('binding_id') != row.get('binding_id'):
            return None
        action = None
        if row['kind'] == 'result':
            run = self.store.get_run(source['run_id'])
            if (not prefs.get('results_enabled') or not run or run.get('status') != source['state']
                    or not self._result_eligible(account, prefs, run)):
                return None
            dual = receipt_view(account, run) if is_dual_row(run) else None
            receipt = self._receipt(account, run) if run['status'] == 'success' and dual is None else None
            if dual and dual.verification_source == 'local_query':
                receipt = run['dual_reconciliation']['record']
            if receipt and self._cancelled(account['_id'], receipt):
                return None
            if dual and dual.phase == 'completed' and (self._dual_cancelled(account, dual)
                                                       or dual.snapshot_conflict(account)):
                return None
            names = {'success': '预约成功', 'failed': '预约失败', 'uncertain': '预约结果待确认', 'skipped': '本轮预约未执行'}
            subject = names[run['status']]
            spec = run['spec']
            interval = f"{spec.get('target_date', '')} {spec.get('begin', '')}–{spec.get('end', '')}"
            seat = self._seat_name(dual.seat_id if dual and dual.seat_id else
                                   receipt['seat_id'] if receipt else run.get('attempted_seat_id') or '未确定')
            explanation = {'success': '已取得身份、座位和时段一致的学校有效回执。',
                           'failed': '学校明确拒绝了此次预约，请打开页面查看记录。',
                           'uncertain': '尚未取得可确认的有效回执，不代表预约失败。请打开页面查询学校记录。',
                           'skipped': '系统记录本轮预约未执行，未自动补交。请打开页面核对计划和执行状态。'}[run['status']]
            if self._non_submission(account, run):
                subject = '本轮预约未执行' if run['status'] == 'skipped' else '预约执行异常'
                explanation = ('本轮未取得有效的预约执行结果，未自动补交。'
                               '请打开页面核对计划、学校记录和执行状态；必要时联系管理员。')
            if dual:
                explanation = (f'{self._verification_message(dual)}\n回执摘要：{dual.receipt_ref}'
                    if dual.phase == 'completed' else
                    f'{dual.owner_label}执行节点报告预约失败，请打开页面查询学校记录。'
                    if dual.phase == 'failed' else
                    f'{dual.owner_label}执行节点报告预约结果待确认，不代表预约失败，请打开页面查询学校记录。')
            body = f'{subject}\n使用时段：{interval}\n座位：{seat}\n{explanation}\n' + self._url(account['_id'], receipt['id'] if receipt else None)
        elif row['kind'] == 'cancellation':
            cancellation = self.store.db.campus_cancellations.find_one({'_id': source['cancellation_id'],
                'account_id': account['_id'], 'state': 'success', 'ack': True,
                'completed_at': {'$gte': prefs.get('results_since', now)}})
            if not prefs.get('results_enabled') or not cancellation or not self._record(cancellation.get('record'), account):
                return None
            record = cancellation['record']
            subject = '预约已取消'
            body = (f"学校已确认取消预约。\n使用时段：{record['begin']}–{record['end']}\n座位：{self._seat_name(record['seat_id'])}\n"
                    + self._url(account['_id'], record['id']))
        elif row['kind'] == 'dual_reminder':
            run = self.store.get_run(source.get('run_id'))
            view = receipt_view(account, run)
            timing = self._dual_reminder_due(prefs, view) if view and view.phase == 'completed' else None
            if (not prefs.get('reminders_enabled') or timing is None or row['due_at'] != timing[0]
                    or source.get('task_id') != view.task_id or source.get('receipt_ref') != view.receipt_ref
                    or source.get('grant_id') != view.grant_id or self._dual_cancelled(account, view)
                    or view.snapshot_conflict(account)
                    or any(view.matches_record(account, record) for record in self._fresh_records(account))):
                return None
            subject = '图书馆预约使用提醒'
            body = (f"您已确认的预约即将开始。\n使用时段：{view.spec.target_date} {view.spec.begin}–{view.spec.end}\n"
                    f"座位：{self._seat_name(view.seat_id)}\n{self._verification_message(view)}\n"
                    f"最近核对：{datetime.fromtimestamp(view.result_at, ZONE).isoformat()}\n回执摘要：{view.receipt_ref}\n"
                    '请按学校要求及时签到。如需取消，请打开页面查询学校记录后操作。\n' + self._url(account['_id']))
        elif row['kind'] == 'reminder':
            live = [{key: record[key] for key in ('id', 'student_id', 'seat_id', 'begin', 'end', 'status')}
                    for record in self._fresh_records(account)]
            if (not prefs.get('reminders_enabled') or source not in live
                    or row['due_at'] != _epoch(source['begin']) - prefs['reminder_minutes'] * 60
                    or row['due_at'] < prefs.get('reminders_since', now)):
                return None
            subject = '图书馆预约使用提醒'
            origin = self.settings.public_origin or f'http://127.0.0.1:{self.settings.port}'
            ticket = issue_ticket(self.vault.key, account, prefs, row, now)
            action = {'label': '取消预约', 'url': origin + '/static/mail-cancel.html#ticket=' + ticket}
            summary = (f"您已确认的预约即将开始。\n使用时段：{source['begin']}–{source['end']}\n座位：{self._seat_name(source['seat_id'])}\n"
                       f"最近核对：{account['snapshot_at']}\n请按学校要求及时签到。点击“取消预约”将直接取消本条预约。")
            notice = '' if self.settings.public_origin else '\n此链接仅能在运行预约后台的电脑上打开。'
            body = summary + '\n' + action['url'] + notice
        else:
            return None
        payload = {'to': self.vault.unseal('notification-address:' + account['_id'], prefs['address']),
                   'subject': subject, 'body': body}
        if action is not None:
            payload['action'] = action
            payload['html_body'] = summary + notice
        return payload

    @staticmethod
    def _verification_message(view):
        if view.verification_source == 'local_query':
            return '本机查询已核实学校有效回执；原双机授权和节点报告保留。'
        return f'{view.owner_label}执行节点已查询核验有效回执。'

    def _seat_name(self, seat_id):
        seat = self.accounts.catalog.get(seat_id)
        return getattr(seat, 'name', None) or (seat.get('name') if isinstance(seat, dict) else None) or seat_id

    def run_once(self):
        if self.sender is None or self._stop.is_set() or not self._poll.acquire(blocking=False):
            return 0
        sent = 0
        try:
            if self._quiet():
                return 0
            self._discover()
            for row in self.outbox.find({'status': 'pending', 'due_at': {'$lte': self.clock()}}).sort('due_at', 1).limit(10):
                if self._stop.is_set() or self._quiet():
                    break
                with self._delivery:
                    try:
                        payload = self._payload(row)
                    except AppError:
                        payload = None
                    claim_query = {'_id': row['_id'], 'status': 'pending', 'kind': row['kind'],
                                   'source': row['source'], 'binding_id': row.get('binding_id'),
                                   'attempted_at': {'$exists': False}}
                    if payload is None:
                        self.outbox.update_one(claim_query, {'$set': {'status': 'cancelled'}})
                        continue
                    claim = self.outbox.update_one(claim_query,
                        {'$set': {'status': 'dispatching', 'attempted_at': self.clock()}})
                    if not claim.modified_count:
                        continue
                    payload['message_id'] = '<' + hashlib.sha256(row['_id'].encode()).hexdigest() + '@njfu-local.invalid>'
                    try:
                        self.sender(payload)
                    except Exception:
                        self.outbox.update_one({'_id': row['_id'], 'status': 'dispatching'},
                            {'$set': {'status': 'unknown', 'error': 'delivery_not_confirmed'}})
                        self._worker_error = 'delivery_not_confirmed'
                    else:
                        self.outbox.update_one({'_id': row['_id'], 'status': 'dispatching'},
                            {'$set': {'status': 'sent', 'sent_at': self.clock()}})
                        sent += 1
            return sent
        finally:
            self._poll.release()

    def start(self):
        if self.sender is None or self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def work():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    self._worker_error = 'notification_worker_unavailable'
                self._stop.wait(15)
        self._thread = threading.Thread(target=work, name='notification-mail', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if isinstance(self.sender, SMTPSender):
            self.sender.close()
        if self._thread:
            self._thread.join(timeout=6)

    def update_preferences(self, account_id, data):
        self._account(account_id)
        fields = {'revision', 'results_enabled', 'reminders_enabled', 'reminder_minutes'}
        if (not isinstance(data, dict) or set(data) != fields or type(data.get('revision')) is not int
                or data['revision'] < 0 or type(data.get('results_enabled')) is not bool
                or type(data.get('reminders_enabled')) is not bool or type(data.get('reminder_minutes')) is not int
                or not 1 <= data['reminder_minutes'] <= 1440):
            raise AppError('invalid_notification_settings', '提醒时间须为 1–1440 分钟，请重新读取通知设置')
        with self._delivery:
            previous = self._ensure(account_id)
            if data['results_enabled'] or data['reminders_enabled']:
                self._require_sender()
                if not previous.get('verified'):
                    raise AppError('email_not_verified', '请先验证接收邮箱')
            changes = {key: data[key] for key in fields - {'revision'}}
            for kind in ('results', 'reminders'):
                if data[kind + '_enabled'] and not previous.get(kind + '_enabled'):
                    changes[kind + '_since'] = self.clock()
            if not self.preferences.update_one({'_id': account_id, 'revision': data['revision']},
                    {'$set': changes, '$inc': {'revision': 1}}).matched_count:
                raise AppError('notification_conflict', '通知设置已在另一处修改，请重新读取', 409)
        return self.get_settings(account_id)
