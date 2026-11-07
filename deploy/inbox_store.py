"""Private cloud inbox. No school passwords, school client or booking operations."""
from contextlib import contextmanager
import base64
import binascii
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import threading
import uuid
from werkzeug.security import check_password_hash
from local_app.models import AppError

SESSION = 8 * 3600
SNAPSHOT_TTL = 24 * 3600
QUEUE_TTL = 48 * 3600
COOKIE = 'njfu_inbox_session'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class InboxStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.hash_slots = threading.BoundedSemaphore(2)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError('Inbox storage cannot be a symlink')
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError('Inbox storage directory must be private')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError('Inbox database must be private')
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mirror (student TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, student TEXT NOT NULL,
                    generation TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, student TEXT NOT NULL,
                    version INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                    generation TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                    lease TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT, UNIQUE(student,version));
                CREATE INDEX IF NOT EXISTS queue_state ON queue(state,retry_at,created);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def _get(db, key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, encoded(value)))

    @staticmethod
    def _row(db, student):
        row = db.execute('SELECT body FROM mirror WHERE student=?', (student,)).fetchone()
        return json.loads(row[0]) if row else None

    def _eligible(self, db, student):
        row = self._row(db, student)
        fresh = 0 <= self.clock() - self._get(db, 'snapshot_at', 0) <= SNAPSHOT_TTL
        if (not fresh or not row or row.get('enabled') is not True or not row.get('account')
                or not row.get('auth') or row['auth'].get('blocked')
                or not row['auth'].get('password_hash')):
            raise AppError('offline_unavailable', '此账号暂不具备离线登录条件，请等待执行电脑上线并通过学校认证', 403)
        return row

    def publish(self, packet):
        generation = packet.get('generation')
        if type(generation) is not int or generation < 1 or not isinstance(packet.get('accounts'), list) or len(packet['accounts']) > 1000:
            raise AppError('invalid_snapshot', '同步快照无效')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if generation <= self._get(db, 'generation', 0): return False
            students = set()
            for row in packet['accounts']:
                student = row.get('student_id')
                if not isinstance(student, str) or not re.fullmatch(r'[0-9]{5,20}', student) or student in students:
                    raise AppError('invalid_snapshot', '同步账号标识无效')
                auth = row.get('auth')
                if auth and (not isinstance(auth.get('generation'), str)
                        or not isinstance(auth.get('password_hash'), str)
                        or not auth['password_hash'].startswith('scrypt:32768:8:1$')
                        or len(auth['password_hash']) > 512):
                    raise AppError('invalid_snapshot', '离线校验器格式无效')
                students.add(student)
                old = self._row(db, student)
                if row.get('sync_error') and old:
                    row = row | {'account':old.get('account'),'records':old.get('records'),'runs':old.get('runs')}
                if old and old.get('cloud_block_generation') and (not row.get('auth')
                        or row['auth'].get('success_id') == old.get('cloud_block_success')):
                    row = row | {k:old.get(k) for k in ('cloud_block_generation','cloud_block_success','cloud_block_code')}
                    if row.get('auth'):row['auth']=row['auth'] | {'blocked':old.get('cloud_block_code')}
                if (old and old.get('account') and row.get('account')
                        and old['account']['id'] == row['account']['id']
                        and old['account']['revision'] > row['account']['revision']):
                    row = row | {'account': old['account']}
                db.execute('INSERT OR REPLACE INTO mirror VALUES (?,?)', (student, encoded(row)))
            for row in db.execute('SELECT student FROM mirror').fetchall():
                if row[0] not in students: db.execute('DELETE FROM mirror WHERE student=?', (row[0],))
            for key in ('generation', 'runtime', 'policy', 'seats'):
                self._set(db, key, packet.get(key))
            self._set(db, 'snapshot_at', self.clock())
            self._set(db, 'heartbeat', self.clock())
            self._set(db, 'last_sync', self.clock())
        return True

    def authenticate(self, student, password, source):
        now = self.clock()
        if not isinstance(student, str) or not re.fullmatch(r'[0-9]{5,20}', student) or not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise AppError('invalid_login', '请填写有效学号和学校密码', 401)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE start<?', (now-300,))
            for raw, limit in ((student + ':' + source, 5), ('source:' + source, 30)):
                key = hashlib.sha256(raw.encode()).hexdigest()
                row = db.execute('SELECT count FROM attempts WHERE key=?', (key,)).fetchone()
                if row and row[0] >= limit:
                    raise AppError('login_limited', '登录尝试过多，请5分钟后再试', 429)
                db.execute('INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now))
        with self.db() as db: row = self._eligible(db, student)
        if not self.hash_slots.acquire(blocking=False):
            raise AppError('auth_busy', '登录校验正在处理其他请求，请稍后重试', 429)
        try:
            if not check_password_hash(row['auth']['password_hash'], password):
                raise AppError('invalid_login', '学号或密码未通过上次学校认证的密码校验', 401)
        finally:self.hash_slots.release()
        return {'role': 'student', 'account_id': row['account']['id'], 'student_id': student,
                'name': row['account'].get('alias') or student, 'auth_mode': 'offline',
                'generation': row['auth']['generation']}

    def issue(self, user):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            row = self._eligible(db, user['student_id'])
            if row['auth']['generation'] != user['generation']:
                raise AppError('authentication_required', '认证状态已变化，请重新登录', 401)
            db.execute('DELETE FROM sessions WHERE expires<=?', (self.clock(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),
                       user['student_id'], user['generation'], self.clock()+SESSION))
        return token

    def _current(self, db, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token): return None
        session = db.execute('SELECT * FROM sessions WHERE digest=? AND expires>?',
                   (hashlib.sha256(token.encode()).hexdigest(), self.clock())).fetchone()
        if not session: return None
        try: row = self._eligible(db, session['student'])
        except AppError: return None
        if row['auth']['generation'] != session['generation']: return None
        return {'role': 'student', 'account_id': row['account']['id'], 'student_id': session['student'],
                'name': row['account'].get('alias') or session['student'], 'auth_mode': 'offline'}

    def current(self, token):
        with self.db() as db: return self._current(db, token)

    def logout(self, token):
        if isinstance(token, str):
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def block(self, student, code):
        with self.db() as db:
            row = self._row(db, student)
            if row and row.get('auth'):
                row['auth']['blocked'] = code
                row['cloud_block_generation'] = row['auth']['generation']
                row['cloud_block_success'] = row['auth'].get('success_id')
                row['cloud_block_code'] = code
                db.execute('UPDATE mirror SET body=? WHERE student=?', (encoded(row), student))
                db.execute('DELETE FROM sessions WHERE student=?', (student,))

    def public_account(self, student):
        with self.db() as db: return self._eligible(db, student)['account']

    def public_data(self, student, field):
        with self.db() as db: return self._eligible(db, student).get(field)

    def cached_runs(self, student, filters):
        """Filter/page only the exported history, with explicit cache coverage."""
        with self.db() as db:
            account = self._eligible(db, student)
            source = account.get('runs')
            cached_at = self._get(db, 'snapshot_at')
        account_id = account['account']['id']
        if filters.get('account_id') and filters['account_id'] != account_id:
            raise AppError('not_found', '账号不存在', 404)
        target = filters.get('date') or None
        if target is not None:
            try:
                if date.fromisoformat(target).isoformat() != target: raise ValueError()
            except (TypeError, ValueError): raise AppError('invalid_date', '日期须为 YYYY-MM-DD') from None
        try: limit = int(filters.get('limit', '100'))
        except (TypeError, ValueError): raise AppError('invalid_limit', '分页条数无效') from None
        if not 1 <= limit <= 100: raise AppError('invalid_limit', '每页条数须为 1–100')
        if (not isinstance(source, dict) or not isinstance(source.get('items'), list)
                or len(source['items']) > 100
                or any(not isinstance(row, dict) or row.get('account_id') != account_id
                       or not isinstance(row.get('status'), str) for row in source['items'])):
            raise AppError('offline_history_unavailable', '历史缓存暂不可用，请等待执行端同步', 503)
        cached = source['items']
        rows = [row for row in cached if target is None or row.get('target_date') == target]
        fingerprint = hashlib.sha256(encoded({'account_id': account_id, 'runs': source}).encode()).hexdigest()
        offset = 0
        cursor = filters.get('cursor')
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024: raise ValueError()
                value = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
                if (not isinstance(value, dict) or set(value) != {'cache', 'date', 'offset'}
                        or not isinstance(value['cache'], str) or not re.fullmatch('[0-9a-f]{64}', value['cache'])
                        or value['date'] != target or type(value['offset']) is not int or value['offset'] < 1):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, binascii.Error):
                raise AppError('invalid_cursor', '离线历史分页位置无效，请重新载入') from None
            if value['cache'] != fingerprint:
                raise AppError('history_cache_changed', '历史缓存已更新，请重新载入后继续翻页', 409)
            offset = value['offset']
            if offset >= len(rows): raise AppError('invalid_cursor', '离线历史分页位置无效，请重新载入')
        counts = {'total': len(rows)}
        for row in rows: counts[row['status']] = counts.get(row['status'], 0) + 1
        next_cursor = None
        if offset + limit < len(rows):
            next_cursor = base64.urlsafe_b64encode(encoded({'cache': fingerprint, 'date': target,
                                                          'offset': offset + limit}).encode()).decode()
        total = (source.get('counts') or {}).get('total')
        truncated = bool(source.get('next_cursor')) or (type(total) is int and total > len(cached))
        return {'items': rows[offset:offset + limit], 'counts': counts, 'next_cursor': next_cursor,
                'cache_only': True, 'cached_at': cached_at, 'cache_limit': 100,
                'cache_truncated': truncated, 'cached_total': len(cached)}

    def shared(self, name):
        if name not in ('policy', 'seats', 'runtime'): raise ValueError('Not a public field')
        with self.db() as db: return self._get(db, name, {} if name != 'seats' else [])

    @staticmethod
    def _receipt(row):
        result = json.loads(row['result']) if row['result'] else {}
        return {'request_id': row['id'], 'version': row['version'], 'state': row['state'],
                'created_at': row['created'], 'expires_at': row['expires'],
                'applied_revision': result.get('revision') if row['state'] == 'applied' else None,
                'code': result.get('code'), 'message': result.get('message')}

    def enqueue(self, student, data, session_token):
        request_id, revision, plan = data.get('request_id'), data.get('expected_revision'), data.get('plan')
        try:
            if str(uuid.UUID(request_id)) != request_id: raise ValueError()
        except (ValueError, TypeError, AttributeError): raise AppError('invalid_request', '请提供有效提交编号') from None
        if type(revision) is not int or not isinstance(plan, dict): raise AppError('invalid_request', '预约配置无效')
        body = encoded({'expected_revision': revision, 'plan': plan})
        if len(body.encode()) > 16384: raise AppError('invalid_request', '预约配置过大')
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self._current(db, session_token)
            if not context or context['student_id'] != student:
                raise AppError('authentication_required', '认证或授权状态已变化，请重新登录', 401)
            account = self._eligible(db, student)
            old = db.execute('SELECT * FROM queue WHERE id=?', (request_id,)).fetchone()
            if old:
                if old['student'] != student or old['digest'] != digest: raise AppError('conflict', '提交编号已被使用', 409)
                return self._receipt(old)
            if revision != account['account']['revision']: raise AppError('conflict', '本机配置版本已变化，请重新载入', 409)
            if db.execute("SELECT 1 FROM queue WHERE student=? AND state IN ('pending','leased')", (student,)).fetchone():
                raise AppError('inbox_pending', '该账号已有待同步配置，请等待处理后再保存', 409)
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM queue WHERE student=?', (student,)).fetchone()[0]
            now = self.clock()
            db.execute('INSERT INTO queue (id,student,version,digest,body,generation,state,created,expires) VALUES (?,?,?,?,?,?,\'pending\',?,?)',
                       (request_id, student, version, digest, body, account['auth']['generation'], now, now+QUEUE_TTL))
            return self._receipt(db.execute('SELECT * FROM queue WHERE id=?', (request_id,)).fetchone())

    def receipt(self, student, request_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM queue WHERE student=? AND id=?', (student, request_id)).fetchone()
            if not row: raise AppError('not_found', '收件记录不存在', 404)
            return self._receipt(row)

    def poll(self, quiet_until=None):
        now = self.clock(); jobs = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._set(db, 'heartbeat', now)
            self._set(db, 'quiet_until', quiet_until if type(quiet_until) in (int,float) and 0 < quiet_until-now <= 90 else 0)
            rows = db.execute("SELECT * FROM queue WHERE (state='pending' OR (state='leased' AND lease_until<=?)) "
                              "AND (retry_at<=? OR expires<=?) ORDER BY created LIMIT 100", (now, now, now)).fetchall()
            for row in rows:
                current = self._row(db, row['student'])
                if row['expires'] <= now and row['attempts'] == 0:
                    db.execute("UPDATE queue SET state='failed',result=? WHERE id=?",
                        (encoded({'code':'inbox_expired','message':'配置已过期，未在执行电脑落库；请重新载入后提交'}),row['id']))
                    continue
                if current and current.get('sync_error') and row['attempts']==0:
                    db.execute('UPDATE queue SET retry_at=?,result=? WHERE id=?',
                        (now+3,encoded({'code':'snapshot_error','message':'该账号快照暂不可用，等待恢复；其他账号继续同步'}),row['id']))
                    continue
                code = ('inbox_expired' if row['expires'] <= now else
                        'auth_changed' if not current or not current.get('enabled') or not current.get('auth')
                        or current['auth'].get('blocked') or current['auth']['generation'] != row['generation'] else None)
                if code and row['attempts'] == 0:
                    db.execute("UPDATE queue SET state='failed',result=? WHERE id=?", (encoded({'code': code, 'message': '配置已过期或认证/授权状态已变化，请重新登录提交'}), row['id']))
                    continue
                if row['retry_at'] > now: continue
                lease = secrets.token_urlsafe(24)
                db.execute("UPDATE queue SET state='leased',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (lease, now+60, row['id']))
                jobs.append({'request_id': row['id'], 'student_id': row['student'], 'version': row['version'],
                             'generation': row['generation'], 'lease': lease, 'expires_at': row['expires'],
                             'created_at': row['created'], 'attempt': row['attempts']+1,
                             'reconcile_only':bool(code),'reconcile_code':code, **json.loads(row['body'])})
                if len(jobs) >= 50: break
        return jobs

    def ack(self, request_id, lease, result):
        state = result.get('state')
        if state not in ('applied', 'failed', 'deferred') or state == 'applied' and type(result.get('revision')) is not int:
            raise AppError('invalid_ack', '落库确认无效')
        safe = {k: result[k] for k in ('state', 'revision', 'code', 'message') if k in result}
        safe['message'] = str(safe.get('message', ''))[:200]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM queue WHERE id=?', (request_id,)).fetchone()
            if not row or not row['lease'] or not secrets.compare_digest(row['lease'], lease):
                raise AppError('lease_changed', '收件租约已变化', 409)
            if row['state'] in ('applied', 'failed'):
                if json.loads(row['result']) != safe: raise AppError('ack_conflict', '已确认的收件结果不可覆盖', 409)
                return
            if state == 'applied':
                queued = json.loads(row['body'])
                if result['revision'] != queued['expected_revision']+1:
                    raise AppError('invalid_ack', '落库版本与原提交不一致')
                mirror = self._row(db, row['student'])
                if mirror and mirror.get('account') and mirror['account']['revision'] <= queued['expected_revision']:
                    mirror['account'] = mirror['account'] | {'plan': queued['plan'], 'revision': result['revision']}
                    db.execute('UPDATE mirror SET body=? WHERE student=?', (encoded(mirror), row['student']))
            db.execute('UPDATE queue SET state=?,result=?,retry_at=?,lease_until=? WHERE id=?',
                       ('pending' if state == 'deferred' else state, encoded(safe), self.clock()+3, 0, request_id))
            self._set(db, 'last_sync', self.clock())

    def summary(self, student=None):
        with self.db() as db:
            query, args = (' AND q.student=?', (student,)) if student else ('', ())
            rows = db.execute('SELECT q.* FROM queue q WHERE q.version=(SELECT MAX(x.version) FROM queue x WHERE x.student=q.student)' + query + ' ORDER BY q.created DESC', args).fetchall()
            latest = {}
            for row in rows:
                latest.setdefault(row['student'], self._receipt(row) | {'student_id': row['student']})
            age = self.clock()-self._get(db, 'heartbeat', 0)
            quiet_until = self._get(db, 'quiet_until', 0)
            paused = 0 <= age <= 90 and self.clock() <= quiet_until
            return {'executor_online': 0 <= age < 15 or paused, 'heartbeat_paused':paused, 'quiet_until':quiet_until,
                    'heartbeat_at': self._get(db, 'heartbeat'), 'last_sync_at': self._get(db, 'last_sync'),
                    'snapshot_at': self._get(db, 'snapshot_at'),
                    'pending': sum(r['state'] in ('pending', 'leased') for r in rows),
                    'failed': sum(r['state'] == 'failed' for r in rows), 'accounts': list(latest.values())}
