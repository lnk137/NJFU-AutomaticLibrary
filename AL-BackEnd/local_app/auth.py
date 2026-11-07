"""School-authenticated users, explicit access roster and opaque local sessions."""
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import re
import secrets
import threading
import time
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash
from .models import AppError, credentials, stamp

COOKIE_NAME = 'njfu_autolib_session'
SESSION_SECONDS = 8*3600


class AuthService:
    def __init__(self, store, accounts=None, *, clock=None):
        self.store, self.accounts = store, accounts
        self.db = store.db
        self.clock = clock or time.time
        self.access, self.security = self.db.campus_access, self.db.campus_security
        self.sessions, self.audit = self.db.campus_sessions, self.db.campus_audit
        self.sessions.create_index('expires_at', expireAfterSeconds=0)
        self.sessions.create_index('student_id')
        self.audit.create_index('at')
        self._attempts, self._rate_lock = defaultdict(deque), threading.Lock()

    def is_allowed(self, student):
        return bool(self.access.find_one({'_id': student, 'enabled': True}, {'_id': 1}))

    def _audit(self, action, *, subject=None, actor='root'):
        self.audit.insert_one({'at': stamp(), 'action': action, 'subject': subject, 'actor': actor})

    def initialize_root(self, password, allowed_students):
        """Explicit local setup only; no web route and never overwrite an existing root."""
        if self.security.find_one({'_id': 'root'}):
            raise AppError('admin_exists', '管理员已初始化，未覆盖现有密码', 409)
        if not isinstance(password, str) or not 6 <= len(password) <= 1024:
            raise AppError('invalid_password', '管理员密码长度须为6–1024字符')
        if not allowed_students or any(not re.fullmatch(r'[0-9]{5,20}', s) for s in allowed_students):
            raise AppError('invalid_student', '请明确提供初始准入学号')
        password_hash = generate_password_hash(password)
        for student in allowed_students:
            self.access.update_one({'_id': student}, {'$setOnInsert': {
                'enabled': True, 'revision': 1, 'note': '', 'updated_at': stamp()}}, upsert=True)
        try:self.security.insert_one({'_id': 'root', 'password_hash': password_hash, 'created_at': stamp()})
        except DuplicateKeyError:
            raise AppError('admin_exists', '管理员已初始化，未覆盖现有密码', 409) from None
        self._audit('root_initialized')

    def _attempt(self, kind, username, source):
        key = (kind, username, source)
        now = self.clock()
        with self._rate_lock:
            for old_key in list(self._attempts):
                if not self._attempts[old_key] or self._attempts[old_key][-1] < now-300:
                    del self._attempts[old_key]
            for value in (key, ('source', source)):
                queue = self._attempts[value]
                while queue and queue[0] < now-300:queue.popleft()
                if len(queue) >= (50 if value[0] == 'source' else 5):
                    raise AppError('login_limited', '登录尝试过多，请5分钟后再试', 429)
            self._attempts[key].append(now)
            self._attempts[('source', source)].append(now)
        return key

    def _issue(self, role, *, account_id=None, student_id=None, credential_revision=None):
        if role == 'student' and credential_revision is None:
            credential_revision = self.store.get_account(account_id)['credential_revision']
        token = secrets.token_urlsafe(32)
        expires = datetime.fromtimestamp(self.clock()+SESSION_SECONDS, timezone.utc)
        self.sessions.insert_one({'_id': hashlib.sha256(token.encode()).hexdigest(), 'role': role,
            'account_id': account_id, 'student_id': student_id, 'expires_at': expires,
            'credential_revision': credential_revision})
        return token

    def login(self, data, source):
        kind, username, password = data.get('kind'), data.get('username'), data.get('password')
        if kind not in ('student', 'root'):
            raise AppError('invalid_login', '请选择学生或管理员登录')
        username, password = credentials(username, password)
        key = self._attempt(kind, username, source)
        if kind == 'root':
            root = self.security.find_one({'_id': 'root'})
            if username != 'root' or not root or not check_password_hash(root['password_hash'], password):
                raise AppError('invalid_login', '管理员账号或密码不正确', 401)
            token = self._issue('root')
            self.security.update_one({'_id': 'root'}, {'$set': {'last_login': stamp()}})
        else:
            if not self.security.find_one({'_id': 'root'}):
                raise AppError('auth_not_ready', '登录服务尚未初始化', 503)
            with self.accounts.account_lock('student:'+username):
                if not self.is_allowed(username):
                    raise AppError('access_denied', '此学号暂未获授权，请联系管理员', 403)
                old = self.store.find_account_by_student(username)
                if old:
                    with self.accounts.account_lock(old['_id']):
                        if old['_id'] in self.accounts.busy:
                            raise AppError('account_busy', '此账号正在执行预约，请稍后登录', 409)
                account = self.accounts.verify_and_save(username, password,
                    old['alias'] if old else '', old['revision'] if old else None)
                token = self._issue('student', account_id=account.id, student_id=username,
                                    credential_revision=account.credential_revision)
                self.access.update_one({'_id': username}, {'$set': {'last_login': stamp()}})
        with self._rate_lock:self._attempts.pop(key, None)
        self._audit('login', subject=username, actor=kind)
        return token, self.current(token)

    def current(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):return None
        row = self.sessions.find_one({'_id': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': {'$gt': datetime.fromtimestamp(self.clock(), timezone.utc)}})
        if not row:return None
        if row['role'] == 'root':
            return {'role': 'root', 'name': '管理员', 'student_id': None, 'account_id': None}
        if row['role'] != 'student' or not self.is_allowed(row['student_id']):return None
        account = self.store.find_account_by_student(row['student_id'])
        if (not account or account['_id'] != row['account_id']
                or type(row.get('credential_revision')) is not int
                or row['credential_revision'] != account['credential_revision']):return None
        return {'role': 'student', 'name': account['identity']['display_name'],
                'student_id': row['student_id'], 'account_id': row['account_id']}

    def require(self, token):
        user = self.current(token)
        if not user:raise AppError('authentication_required', '请先登录，或登录状态已失效', 401)
        return user

    def logout(self, token):
        if isinstance(token, str):
            self.sessions.delete_one({'_id': hashlib.sha256(token.encode()).hexdigest()})

    def list_users(self):
        accounts = {a.student_id: a for a in self.store.list_accounts()}
        users = []
        for row in self.access.find().sort('_id', 1):
            account = accounts.get(row['_id'])
            raw = self.store.get_account(account.id) if account else None
            users.append({'student_id': row['_id'], 'enabled': row['enabled'], 'revision': row['revision'],
                'note': row.get('note', ''), 'last_login': row.get('last_login'),
                'school_name': (raw.get('identity') or {}).get('display_name') if raw else None,
                'account': asdict(account) if account else None})
        return users

    def set_access(self, student, data):
        if not re.fullmatch(r'[0-9]{5,20}', student):
            raise AppError('invalid_student', '学号须为5–20位数字')
        enabled, note, expected = data.get('enabled'), data.get('note', ''), data.get('expected_revision')
        if type(enabled) is not bool or not isinstance(note, str) or len(note) > 120:
            raise AppError('invalid_access', '请使用有效的准入状态和120字符以内备注')
        with self.accounts.account_lock('student:'+student):
            old = self.access.find_one({'_id': student})
            if (old and (type(expected) is not int or expected != old['revision'])) or (not old and expected is not None):
                raise AppError('conflict', '准入名单已变化，请刷新后重试', 409)
            account = self.store.find_account_by_student(student)
            lock = self.accounts.account_lock(account['_id'] if account else 'new:'+student)
            with lock:
                if account:account=self.store.get_account(account['_id'])
                if not enabled and account and isinstance(account.get('plan'),dict) and account['plan'].get('enabled'):
                    # Stop first with a revision check; do not revalidate or
                    # rewrite legacy time/seat fields as a side effect.
                    self.store.disable_plan(account['_id'],account['revision'])
                revision = old['revision']+1 if old else 1
                values = {'enabled': enabled, 'note': note.strip(), 'revision': revision, 'updated_at': stamp()}
                self.access.update_one({'_id': student}, {'$set': values}, upsert=True)
                if not enabled:
                    self.sessions.delete_many({'student_id': student})
                self._audit('access_enabled' if enabled else 'access_disabled', subject=student)
        return {'student_id': student, **values}

    def list_audit(self):
        return [{k: row.get(k) for k in ('at', 'action', 'subject', 'actor')}
                for row in self.audit.find().sort('_id', -1).limit(100)]
