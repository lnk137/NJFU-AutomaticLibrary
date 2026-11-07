"""Account-scoped locks, credential changes, verified sessions and query snapshots."""
from datetime import datetime, timedelta
from dataclasses import replace
import hmac
import logging
import re
import threading
import time
import uuid
from .models import AppError, SegmentedPlan, credentials, stamp, validate_plan, resolve_runs, ZONE
from .date_exceptions import MAX_EXCEPTION_DAYS, exception_date, guard_date_edit, next_scheduled_run, validate_date_exceptions
from .seat_preferences import validate_preferences


class AccountService:
    def __init__(self, store, vault, catalog, client_factory, *, access_allowed=None, wall_clock=None):
        self.store, self.vault, self.catalog, self.client_factory = store, vault, catalog, client_factory
        self._guard = threading.Lock()
        self._locks, self._clients, self.busy = {}, {}, set()
        self.after_query = None
        self.auth_observer = None
        self.last_connection = {'state': 'unchecked', 'checked_at': None}
        self.is_allowed = access_allowed or (lambda student: True)
        self.wall_clock = wall_clock or time.time
        self._managed_gate = None

    @property
    def managed_gate(self):return self._managed_gate

    @managed_gate.setter
    def managed_gate(self, gate):
        self._managed_gate=gate
        if gate is not None:
            gate.set_auth_resolver(self._auth_accounts_for_student,self._auth_student_for_account)

    def _auth_accounts_for_student(self, student):
        # Projections exclude encrypted credentials as well as the password.
        rows=list(self.store.accounts.find({'student_id':{'$regex':'^'+re.escape(student.strip())+'$',
            '$options':'i'}},{'_id':1,'student_id':1}).limit(2).max_time_ms(750))
        if any(row.get('student_id','').casefold()!=student.strip().casefold() for row in rows):
            raise AppError('dual_auth_window_unavailable','双机身份映射尚未核实，请稍后重新登录',409)
        return [row['_id'] for row in rows]

    def _auth_student_for_account(self, account_id):
        row=self.store.accounts.find_one({'_id':account_id},{'_id':1,'student_id':1},max_time_ms=750)
        if row is None:raise AppError('dual_auth_window_unavailable','双机身份映射尚未核实，请稍后重新登录',409)
        return row['student_id']

    def _require_fresh_auth(self, student, account_id=None):
        gate=self.managed_gate
        if gate is not None:
            if account_id is not None:gate.require_auth(account_id)
            gate.require_auth_student(student)

    def _require_ordinary_scope(self, account_id, plan, exceptions):
        gate = self.managed_gate
        if not plan.enabled or gate is None or not gate.is_managed(account_id):return
        def refuse():
            raise AppError('dual_segmented_unsupported',
                '本轮双机仅支持普通预约计划；原计划和日期例外未修改',409)
        if isinstance(plan,SegmentedPlan) or plan.time_mode == 'segmented':refuse()
        now = datetime.fromtimestamp(self.wall_clock(),ZONE)
        if isinstance(exceptions,dict) and len(exceptions)<=MAX_EXCEPTION_DAYS:
            for text,rule in exceptions.items():
                if not isinstance(rule,dict) or rule.get('action')!='override':continue
                target = exception_date(text)
                if target<=now.date():continue
                release = target-timedelta(days=plan.day_offset)
                due = datetime.fromisoformat(f'{release}T{plan.trigger}').replace(tzinfo=ZONE)
                if due<=now:continue
                if (any(key in rule for key in ('segments','week_segments'))
                        or rule.get('operation_id') is not None or rule.get('segment_id') is not None):refuse()
        normalized = validate_date_exceptions(exceptions,self.catalog)
        for text,rule in normalized.items():
            if rule['action']!='override':continue
            target = exception_date(text)
            if target<=now.date():continue
            release = target-timedelta(days=plan.day_offset)
            runs = resolve_runs(account_id,1,plan,release,date_exceptions=normalized)
            if runs and runs[0].due_at>now.timestamp() and (len(runs)!=1 or any(run.operation_id is not None for run in runs)):
                refuse()

    def account_lock(self, account_id):
        with self._guard: return self._locks.setdefault(account_id, threading.RLock())

    def discard(self, account_id):
        old = self._clients.pop(account_id, None)
        if old: old[2].close()

    def _observe_auth(self, account_id, code=None):
        if self.auth_observer:
            try: self.auth_observer(account_id, code)
            except Exception: logging.getLogger(__name__).warning('Offline authentication state update failed')

    def client(self, account_id, *, force=False, _checked_account=None):
        # execute() alone supplies its freshly checked row while holding the
        # account lock. Reuse it only for an existing session, never for login.
        # The caller still reads current account/access state at the HTTP guard.
        if not force and _checked_account is not None and _checked_account['_id'] == account_id:
            cached = self._clients.get(account_id)
            if (cached and cached[0] == _checked_account['credential_revision']
                    and 0 <= time.monotonic()-cached[1] <= 900
                    and cached[2].identity is not None
                    and cached[2].identity.student_id == _checked_account['student_id']):
                return cached[2]
        row = self.store.get_account(account_id)
        if not self.is_allowed(row['student_id']):
            raise AppError('access_denied', '此学号的预约权限已停用', 403)
        cached = self._clients.get(account_id)
        if force or not cached or cached[0] != row['credential_revision'] or time.monotonic()-cached[1] > 900:
            self._require_fresh_auth(row['student_id'],account_id)
            self.discard(account_id)
            client = self.client_factory(row['student_id'], self.vault.unseal(account_id, row['secret']), allow_live=False)
            try:
                identity = client.login()
                if identity.student_id != row['student_id']: raise AppError('identity_mismatch', '学校身份不一致', 502)
            except Exception as exc:
                if isinstance(exc,AppError):self._observe_auth(account_id,exc.code)
                client.close(); raise
            self._observe_auth(account_id)
            self._clients[account_id] = (row['credential_revision'], time.monotonic(), client)
        return self._clients[account_id][2]

    def verify_and_save(self, student_id, password, alias='', expected_revision=None):
        if not isinstance(student_id, str): raise AppError('invalid_student', '请填写学号')
        student_id = student_id.strip()
        if not isinstance(alias, str) or len(alias) > 80: raise AppError('invalid_alias', '备注最多 80 字符')
        with self.account_lock('student:'+student_id):
            if not self.is_allowed(student_id):raise AppError('access_denied', '此学号暂未获授权', 403)
            old = self.store.find_account_by_student(student_id)
            if old and (type(expected_revision) is not int or expected_revision != old['revision']):
                raise AppError('conflict', '账号已存在或配置已变化，请切换并重新载入', 409)
            self._require_fresh_auth(student_id,old['_id'] if old else None)
            if not password and old: password = self.vault.unseal(old['_id'], old['secret'])
            student_id, password = credentials(student_id, password)
            account_id = old['_id'] if old else uuid.uuid4().hex
            with self.account_lock(account_id):
                if account_id in self.busy:
                    raise AppError('account_busy', '此账号正在执行预约，请稍后登录', 409)
                credential_changed = True
                if old:
                    try:
                        previous_password = self.vault.unseal(account_id, old['secret'])
                    except AppError as exc:
                        # A verified explicit password may replace an unreadable
                        # envelope, but its rejection cannot invalidate that envelope.
                        if exc.code != 'credential_unavailable': raise
                    else:
                        credential_changed = not hmac.compare_digest(
                            previous_password.encode('utf-8'), password.encode('utf-8'))
                c = self.client_factory(student_id, password, allow_live=False)
                try:
                    identity = c.login()
                    if identity.student_id != student_id: raise AppError('identity_mismatch', '学校身份不一致', 502)
                    envelope = self.vault.seal(account_id, password) if credential_changed else old['secret']
                    result = self.store.save_verified_account(account_id, identity, alias,
                        envelope, expected_revision, credential_changed=credential_changed)
                except Exception as exc:
                    if old and not credential_changed and isinstance(exc,AppError):
                        self._observe_auth(account_id,exc.code)
                        if exc.code in {'login_failed', 'verification_required', 'login_incomplete', 'identity_mismatch'}:
                            # Only the trusted gateway consumes this internal flag.
                            exc.saved_credential_rejected = True
                    c.close(); raise
                self._observe_auth(account_id)
                self.discard(account_id)
                self._clients[account_id] = (result.credential_revision, time.monotonic(), c)
                self.last_connection = {'state': 'ok', 'checked_at': stamp()}
                return result

    def _validated_plan_update(self, row, data):
        """Shared admission while the caller holds the account lock; does not write."""
        account_id = row['_id']
        if isinstance(data, dict) and data.get('enabled') and not self.is_allowed(row['student_id']):
            raise AppError('access_denied', '此学号尚未获准开启预约，请先授权', 403)
        checked = validate_plan(data, self.catalog)
        self._require_ordinary_scope(account_id,checked,row.get('date_exceptions',{}))
        if checked.enabled and (isinstance(checked, SegmentedPlan) or any(key in (row.get('plan') or {}) for key in ('segments', 'week_segments'))):
            if account_id in self.busy:
                raise AppError('account_busy', '此账号正在执行预约，请稍后保存', 409)
            for trigger in {checked.trigger, (row.get('plan') or {}).get('trigger')} - {None, ''}:
                guard_date_edit(datetime.fromtimestamp(self.wall_clock(), ZONE), trigger)
        return checked

    def save_plan(self, account_id, data, expected_revision):
        if type(expected_revision) is not int: raise AppError('invalid_revision', '请重新载入账号配置', 409)
        with self.account_lock(account_id):
            row = self.store.get_account(account_id)
            checked = self._validated_plan_update(row, data)
            return self.store.save_plan(account_id, checked, expected_revision)

    def update_date_exceptions(self, account_id, data, expected_revision):
        if (isinstance(data,dict) and data.get('action')=='override'
                and any(key in data for key in ('segments','week_segments','operation_id','segment_id'))
                and self.managed_gate is not None and self.managed_gate.is_managed(account_id)):
            with self.account_lock(account_id):
                row = self.store.get_account(account_id)
                plan = validate_plan(row.get('plan'),self.catalog)
                self._require_ordinary_scope(account_id,plan,{data.get('date'):data})
        fields = {'skip_next': set(), 'skip_range': {'start_date', 'end_date'},
                  'override': {'date', 'begin', 'end', 'seat_ids', 'segments'}, 'remove': {'start_date', 'end_date'}}
        if (not isinstance(data, dict) or not isinstance(data.get('action'), str) or data.get('action') not in fields
                or set(data) - fields[data['action']] - {'action', 'expected_revision'}):
            raise AppError('invalid_exception', '请选择有效的日期调整')
        if (data['action'] == 'override' and 'segments' in data
                and any(key in data for key in ('begin', 'end', 'seat_ids'))):
            raise AppError('invalid_exception', '单日调整不能同时包含单段和多段内容')
        if type(expected_revision) is not int:
            raise AppError('invalid_revision', '请重新载入账号配置', 409)
        with self.account_lock(account_id):
            row = self.store.get_account(account_id)
            if row['revision'] != expected_revision:
                raise AppError('conflict', '配置已变化，请重新载入后调整日期', 409)
            if not row.get('plan'):
                raise AppError('incomplete_plan', '请先保存基本预约计划')
            plan = validate_plan(row['plan'], self.catalog)
            if not plan.trigger:
                raise AppError('incomplete_plan', '请先保存放号时间')
            now = datetime.fromtimestamp(self.wall_clock(), ZONE)
            managed = self.managed_gate is not None and self.managed_gate.is_managed(account_id)
            if not managed:
                guard_date_edit(now, plan.trigger)
            exceptions = validate_date_exceptions(row.get('date_exceptions', {}), self.catalog)
            action = data['action']
            if action == 'skip_next':
                if not plan.seat_ids and not isinstance(plan, SegmentedPlan):
                    raise AppError('incomplete_plan', '请先保存首选座位')
                upcoming = next_scheduled_run(account_id, expected_revision, replace(plan, enabled=True), now, exceptions)
                if not upcoming:
                    raise AppError('incomplete_plan', '没有可跳过的后续预约日期')
                start = end = exception_date(upcoming.target_date)
            elif action == 'override':
                start = end = exception_date(data.get('date'))
            else:
                start, end = exception_date(data.get('start_date')), exception_date(data.get('end_date'))
            if start > end or (end - start).days >= MAX_EXCEPTION_DAYS:
                raise AppError('invalid_exception_range', '请选择先后正确、最多366天的日期范围')
            targets = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            dates = [target.isoformat() for target in targets]
            guard_date_edit(now, plan.trigger, None if action == 'remove' else start,
                            day_offset=plan.day_offset, affected_dates=targets if managed else None)
            if action != 'remove':
                handled = self.store.runs.find_one({'account_id': account_id, 'target_date': {'$in': dates},
                    '$or': [{'attempted_seat_id': {'$ne': None}},
                            {'status': {'$in': ['submitting', 'uncertain', 'success', 'failed']}}]})
                if handled:
                    raise AppError('exception_executed', '所选日期已有执行记录，请先核对预约结果', 409)
            for text in dates:
                if action == 'remove':
                    exceptions.pop(text, None)
                elif action == 'override':
                    fields = ('segments',) if 'segments' in data else ('begin', 'end', 'seat_ids')
                    exceptions[text] = {key: data.get(key) for key in fields} | {'action': 'override'}
                else:
                    exceptions[text] = {'action': 'skip'}
            normalized = validate_date_exceptions(exceptions, self.catalog)
            self._require_ordinary_scope(account_id,plan,normalized)
            guard_date_edit(datetime.fromtimestamp(self.wall_clock(), ZONE), plan.trigger,
                            None if action == 'remove' else start, day_offset=plan.day_offset,
                            affected_dates=targets if managed else None)
            return self.store.save_date_exceptions(account_id, normalized, expected_revision)

    def seat_preferences(self, account_id):
        return self.store.preference_view(self.store.get_account(account_id))

    def save_seat_preferences(self, account_id, preferences, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise AppError('invalid_preferences_revision', '请重新读取收藏与分组', 409)
        row = self.store.get_account(account_id)
        normalized = validate_preferences(preferences, self.catalog, previous=row.get('seat_preferences'))
        # Metadata has its own CAS revision and never acquires a booking lock.
        return self.store.save_seat_preferences(account_id, normalized, expected_revision)

    def recheck(self, account_id):
        with self.account_lock(account_id):
            if account_id in self.busy: raise AppError('account_busy', '此账号正在准备预约，请稍后查询', 409)
            row = self.store.get_account(account_id)
            try: self.client(account_id, force=True)
            except AppError as exc:
                self.store.query_failed(account_id, row['revision'], exc.code); raise
            self.store.accounts.update_one({'_id': account_id, 'credential_revision': row['credential_revision']}, {'$set': {'verified_at': stamp(), 'login_error': None}})
            self.last_connection = {'state': 'ok', 'checked_at': stamp()}
            return self.store.view(self.store.get_account(account_id))

    def snapshot(self, account_id):
        row = self.store.get_account(account_id)
        return {'account_id': account_id, 'revision': row['revision'], 'records': row.get('snapshot', []),
                'checked_at': row.get('snapshot_at'), 'error': row.get('query_error'), 'error_at': row.get('query_error_at')}

    def query(self, account_id):
        with self.account_lock(account_id):
            if account_id in self.busy: raise AppError('account_busy', '此账号正在准备预约，请稍后查询', 409)
            row = self.store.get_account(account_id)
            try:
                client = self.client(account_id)
                today = datetime.now(ZONE).date()
                records = client.query(today, today+timedelta(days=3))
            except AppError as exc:
                self.store.query_failed(account_id, row['revision'], exc.code)
                self.discard(account_id)
                self.last_connection = {'state': 'error', 'checked_at': stamp()}
                raise
            self.store.save_snapshot(account_id, row['revision'], records, stamp())
            if self.after_query:
                self.after_query(account_id,records,client.identity,getattr(client,'active_statuses',frozenset()))
            self.last_connection = {'state': 'ok', 'checked_at': stamp()}
            return self.snapshot(account_id)

    def close(self):
        for account_id in list(self._clients): self.discard(account_id)
