"""Durable, fail-closed state publication and fenced Mongo mutation intents."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

from local_app.models import AppError
from .protocol import make_state
from .platform_policy import configure_sqlite, durability_policy


class SyncPending(AppError):
    def __init__(self):
        super().__init__('native_sync_pending', '执行器同步尚未确认，已停止新任务；请先恢复同步', 503)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class DurableOutbox:
    """ACK loss retries state only. Business writes are never replayed."""

    def __init__(self, directory, native, *, initialize=False):
        self.native, self.lock, self.failed = native, threading.RLock(), True
        self.active_operations=0
        self.db = self.owner = None
        root = Path(directory)
        if initialize:
            root.mkdir(mode=0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private, non-symlink outbox directory required')
        self.path = root/'management.db'
        if initialize and self.path.exists():
            raise ValueError('refuse management ledger reset')
        if not initialize:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('unsafe management ledger')
            with self.path.open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\0':
                    raise ValueError('corrupt management ledger header')
        try:
            fd = os.open(root/'owner.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
            self.owner = os.fdopen(fd, 'r+')
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            if initialize:
                fd = os.open(self.path, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
                os.close(fd)
            self.db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, isolation_level=None, check_same_thread=False)
            policy = durability_policy()
            if not initialize:
                stored = dict(self.db.execute("SELECT key,value FROM meta WHERE key IN ('platform','durability_profile')"))
                binding = {key:policy[key] for key in ('platform','durability_profile')}
                legacy_mac = not stored and policy['platform'] == 'darwin'
                if not legacy_mac and stored != binding:
                    raise ValueError('management platform durability profile mismatch')
            self.db.execute('PRAGMA busy_timeout=2000')
            if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise ValueError('management WAL unavailable')
            configure_sqlite(self.db)
            if initialize:
                self.db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                    'CREATE TABLE states(key TEXT PRIMARY KEY,applied TEXT,pending TEXT,projection TEXT,pending_projection TEXT);'
                    'CREATE TABLE intents(id TEXT PRIMARY KEY,scope TEXT NOT NULL,operation TEXT NOT NULL,status TEXT NOT NULL);')
                with self.transaction():
                    self._meta('format', '1')
                    self._meta('platform', policy['platform'])
                    self._meta('durability_profile', policy['durability_profile'])
                    self._meta('identity', native.identity)
                    self._meta('epoch', str(native.epoch))
                    self._meta('control', encoded({'live':False, 'clock_ok':False}))
                parent = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            if self.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('management ledger integrity failure')
            if self.meta('format') != '1' or self.meta('identity') != native.identity:
                raise ValueError('management/native ledger identity mismatch')
            if int(self.meta('epoch')) > native.epoch:
                raise ValueError('native epoch rollback')
            with self.transaction():
                self._meta('epoch', str(native.epoch))
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    @contextmanager
    def operation(self):
        self.active_operations+=1
        try:yield
        finally:self.active_operations-=1

    def _meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key,value))

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise ValueError('missing management metadata')
        return row[0]

    def _value(self, key, column):
        assert column in {'applied','pending','projection'}
        row = self.db.execute('SELECT '+column+' FROM states WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def applied(self, key):
        with self.lock:
            return self._value(key, 'applied')

    def pending(self, key):
        with self.lock:
            return self._value(key, 'pending')

    def projection(self, key):
        with self.lock:
            return self._value(key, 'projection')

    def _stage(self, key, desired, projection):
        if self.pending(key):
            raise SyncPending()
        old = self.applied(key)
        state = make_state(key, (old['version'] if old else 0)+1, **desired)
        self.db.execute('INSERT INTO states(key,pending,pending_projection) VALUES(?,?,?) '
                        'ON CONFLICT(key) DO UPDATE SET pending=excluded.pending,pending_projection=excluded.pending_projection',
                        (key, encoded(state), encoded(projection)))
        return state

    def flush(self, key):
        with self.lock:
            pending = self.pending(key)
            if pending is None:
                return self.applied(key)
            self.native.state_update(key, pending)
            # Native has durably committed and published before this matching ACK.
            with self.transaction():
                cursor = self.db.execute('UPDATE states SET applied=pending,projection=pending_projection,pending=NULL,pending_projection=NULL '
                                         'WHERE key=? AND pending=?', (key,encoded(pending)))
                if cursor.rowcount != 1:
                    raise ValueError('outbox changed while publishing')
            return pending

    def publish(self, key, desired, projection):
        with self.transaction():
            self._stage(key, desired, projection)
        return self.flush(key)

    def unresolved(self):
        with self.lock:
            return self.db.execute("SELECT id,scope,operation,status FROM intents WHERE status IN ('intent','mutating')").fetchall()

    def require_ready(self):
        if self.failed or self.unresolved() or self.db.execute('SELECT 1 FROM states WHERE pending IS NOT NULL LIMIT 1').fetchone():
            raise SyncPending()

    def mutate(self, keys, operation, action, project):
        with self.lock,self.operation():
            self.require_ready()
            token = uuid.uuid4().hex
            keys = sorted(set(keys))
            try:
                with self.transaction():
                    self.db.execute('INSERT INTO intents VALUES(?,?,?,?)', (token,encoded(keys),operation,'intent'))
                    for key in keys:
                        old = self.applied(key)
                        self._stage(key, {'enabled':False, 'plan':False, 'credential':old['credential'] if old else 0}, self.projection(key))
                for key in keys:
                    self.flush(key)
                with self.transaction():
                    self.db.execute("UPDATE intents SET status='mutating' WHERE id=?", (token,))
                failure = None
                try:
                    result = action()
                except Exception as exc:
                    failure = exc
                for key in keys:
                    desired, projection = project(key)
                    self.publish(key, desired, projection)
                with self.transaction():
                    self.db.execute('UPDATE intents SET status=? WHERE id=?', ('failed' if failure else 'done',token))
            except Exception as exc:
                self.failed = True
                raise SyncPending() from exc
            if failure:
                raise failure
            return result

    def recover(self, project, account_keys):
        """Reconcile readback only, pause globally, never rerun action/ARM."""
        with self.lock,self.operation():
            self.failed = True
            try:
                if self.meta('identity')!=self.native.identity or int(self.meta('epoch'))>self.native.epoch:
                    raise AppError('native_state_invalid','执行记录身份或代际不匹配，保持暂停且未重置记录',503)
                with self.transaction():self._meta('epoch',str(self.native.epoch))
                self.flush('global')
                with self.transaction():
                    self._meta('control', encoded({'live':False, 'clock_ok':False}))
                    self._stage('global', {'enabled':False,'plan':False,'credential':0}, {'live':False,'clock_ok':False})
                self.flush('global')
                pending_keys = [row[0] for row in self.db.execute("SELECT key FROM states WHERE pending IS NOT NULL AND key!='global'")]
                for key in pending_keys:
                    self.flush(key)
                known = {row[0] for row in self.db.execute("SELECT key FROM states WHERE key!='global'")}
                for row in self.unresolved():
                    known.update(json.loads(row[1]))
                known.update(account_keys() if callable(account_keys) else account_keys)
                for key in sorted(known-{'global'}):
                    desired, projection = project(key)
                    self.publish(key, desired, projection)
                with self.transaction():
                    self.db.execute("UPDATE intents SET status='reconciled' WHERE status IN ('intent','mutating')")
                self.failed = False
            except AppError as exc:
                if exc.code=='native_state_invalid':raise
                raise SyncPending() from exc
            except Exception as exc:
                raise SyncPending() from exc

    def set_control(self, *, live, clock_ok):
        if type(live) is not bool or type(clock_ok) is not bool:
            raise ValueError('boolean control gates required')
        with self.lock,self.operation():
            self.require_ready()
            try:
                with self.transaction():
                    self._meta('control', encoded({'live':live,'clock_ok':clock_ok}))
                    self._stage('global', {'enabled':live,'plan':clock_ok,'credential':0}, {'live':live,'clock_ok':clock_ok})
                return self.flush('global')
            except Exception as exc:
                self.failed = True
                raise SyncPending() from exc

    def status(self):
        with self.lock:
            return {'healthy': not self.failed, 'recovery_required':bool(self.unresolved()),
                    'applied':{row[0]:json.loads(row[1]) for row in self.db.execute('SELECT key,applied FROM states WHERE applied IS NOT NULL')},
                    'pending_keys':[row[0] for row in self.db.execute('SELECT key FROM states WHERE pending IS NOT NULL')]}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.owner is not None:
            self.owner.close()
            self.owner = None
