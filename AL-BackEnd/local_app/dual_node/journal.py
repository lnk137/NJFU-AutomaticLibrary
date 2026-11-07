"""Private local grant burns. Opening never creates or resets execution state."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from ..native.platform_policy import configure_sqlite, durability_policy

TERMINAL = frozenset({'completed', 'failed', 'not_sent', 'uncertain'})
FIELDS = frozenset({'job_id', 'attempt_count', 'verified', 'http_status', 'curl_code',
                    'sent', 'code', 'seat_index', 'native_epoch', 'native_session', 'reason', 'receipt_ref'})


def _progress_hash(db):
    value = {'burns':db.execute('SELECT * FROM burns ORDER BY task_key').fetchall(),
             'native_identity':db.execute("SELECT value FROM meta WHERE key='native_identity'").fetchone()[0]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _write_progress(path, progress, *, initialize=False):
    raw = json.dumps(progress, sort_keys=True).encode()
    target = path/'progress.json'
    if initialize:
        _create(target, raw)
    else:
        temporary = path/('progress-'+uuid.uuid4().hex+'.part')
        _create(temporary, raw); os.replace(temporary, target)
    fd = os.open(path, os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _private(path, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise ValueError('unsafe grant journal')
    return info


def _create(path, data=b''):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


class BurnJournal:
    @classmethod
    def initialize(cls, directory):
        path = Path(directory).absolute()
        path.mkdir(mode=0o700, exist_ok=False)
        _create(path/'owner.lock'); _create(path/'state.db')
        info = _private(path/'state.db')
        marker = {'schema': 1, 'identity': uuid.uuid4().hex,
                  'device': info.st_dev, 'inode': info.st_ino}
        raw = json.dumps(marker, sort_keys=True).encode()
        _create(path/'identity.json', raw)
        db = sqlite3.connect(path/'state.db', isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL'); configure_sqlite(db)
            db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE burns(task_key TEXT PRIMARY KEY,grant_id TEXT NOT NULL UNIQUE,'
                'binding TEXT NOT NULL,node TEXT NOT NULL,status TEXT NOT NULL,fields TEXT NOT NULL);')
            policy = durability_policy()
            db.executemany('INSERT INTO meta VALUES(?,?)', {
                'identity': marker['identity'], 'marker_hash': hashlib.sha256(raw).hexdigest(),
                'native_identity': '', 'platform': policy['platform'],
                'durability_profile': policy['durability_profile'], 'sequence':'0'}.items())
            state_hash = _progress_hash(db)
            db.execute('INSERT INTO meta VALUES(?,?)', ('progress_hash', state_hash))
        finally:
            db.close()
        _write_progress(path, {'identity':marker['identity'], 'sequence':0, 'state_hash':state_hash}, initialize=True)
        fd = os.open(path, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return cls(path)

    def __init__(self, directory):
        self.path = Path(directory).absolute(); self.db = self.owner = None
        self.lock = threading.RLock()
        try:
            _private(self.path, True)
            self.identities = {}
            for name in ('identity.json', 'owner.lock', 'state.db'):
                info = _private(self.path/name)
                self.identities[name] = (info.st_dev, info.st_ino)
            raw = (self.path/'identity.json').read_bytes()
            if len(raw) > 1024: raise ValueError('invalid journal marker')
            self.marker = json.loads(raw)
            if (set(self.marker) != {'schema', 'identity', 'device', 'inode'}
                    or self.marker['schema'] != 1
                    or not re.fullmatch('[0-9a-f]{32}', self.marker['identity'])
                    or (self.marker['device'], self.marker['inode']) != self.identities['state.db']):
                raise ValueError('journal identity mismatch')
            fd = os.open(self.path/'owner.lock', os.O_RDWR|os.O_NOFOLLOW)
            self.owner = os.fdopen(fd, 'r+')
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with (self.path/'state.db').open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\0': raise ValueError('corrupt journal')
            self.db = sqlite3.connect((self.path/'state.db').as_uri()+'?mode=rw', uri=True,
                                     isolation_level=None, check_same_thread=False)
            policy = durability_policy()
            values = dict(self.db.execute('SELECT key,value FROM meta'))
            if (values.get('identity') != self.marker['identity']
                    or values.get('marker_hash') != hashlib.sha256(raw).hexdigest()
                    or any(values.get(key) != policy[key] for key in ('platform', 'durability_profile'))):
                raise ValueError('journal profile mismatch')
            if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                raise ValueError('journal WAL unavailable')
            configure_sqlite(self.db)
            if self.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('journal integrity failure')
            self._check()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            self.close(); raise ValueError('grant journal unavailable; state retained') from None

    def _check(self):
        if self.db is None: raise ValueError('journal closed')
        for name, identity in self.identities.items():
            info = _private(self.path/name)
            if (info.st_dev, info.st_ino) != identity: raise ValueError('journal replaced')
        with (self.path/'state.db').open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0': raise ValueError('journal corrupt')
        _private(self.path/'progress.json')
        raw = (self.path/'progress.json').read_bytes()
        if len(raw) > 1024: raise ValueError('journal progress invalid')
        progress = json.loads(raw)
        meta = dict(self.db.execute("SELECT key,value FROM meta WHERE key IN ('sequence','progress_hash')"))
        expected = {'identity':self.marker['identity'], 'sequence':int(meta['sequence']), 'state_hash':meta['progress_hash']}
        if progress != expected or _progress_hash(self.db) != expected['state_hash']:
            raise ValueError('journal rollback or interrupted commit')

    @contextmanager
    def transaction(self):
        with self.lock:
            self._check(); self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                sequence = int(self.db.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()[0])+1
                state_hash = _progress_hash(self.db)
                self.db.execute("UPDATE meta SET value=? WHERE key='sequence'", (str(sequence),))
                self.db.execute("UPDATE meta SET value=? WHERE key='progress_hash'", (state_hash,))
                self.db.execute('COMMIT')
                _write_progress(self.path, {'identity':self.marker['identity'],
                    'sequence':sequence, 'state_hash':state_hash})
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise

    @property
    def native_identity(self):
        with self.lock:
            self._check()
            return self.db.execute("SELECT value FROM meta WHERE key='native_identity'").fetchone()[0]

    def bind_native(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('invalid native identity')
        with self.transaction():
            previous = self.native_identity
            if previous and previous != identity: raise ValueError('native identity changed')
            self.db.execute("UPDATE meta SET value=? WHERE key='native_identity'", (identity,))

    def consume(self, grant_id, task_key, binding_sha256, node_id):
        for token in (grant_id, task_key, node_id):
            if not isinstance(token, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', token):
                raise ValueError('invalid grant binding')
        if not isinstance(binding_sha256, str) or not re.fullmatch('[0-9a-f]{64}', binding_sha256):
            raise ValueError('invalid grant binding')
        try:
            with self.transaction():
                self.db.execute('INSERT INTO burns VALUES(?,?,?,?,?,?)',
                    (task_key, grant_id, binding_sha256, node_id, 'consumed', '{}'))
        except sqlite3.IntegrityError:
            raise ValueError('grant already consumed; no automatic replay') from None

    def status(self, task_key):
        with self.lock:
            self._check()
            row = self.db.execute('SELECT grant_id,binding,node,status,fields FROM burns WHERE task_key=?', (task_key,)).fetchone()
            return None if row is None else dict(zip(('grant_id','binding_sha256','node_id','status','fields'),
                (*row[:4], json.loads(row[4])), strict=True))

    def record(self, task_key, status, fields):
        if status not in TERMINAL|{'armed', 'retrying'} or not isinstance(fields, dict) or set(fields)-FIELDS:
            raise ValueError('invalid safe result')
        for value in fields.values():
            if value is not None and type(value) not in (str, int, bool): raise ValueError('invalid safe result')
            if isinstance(value, str) and not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
                raise ValueError('invalid safe result')
        with self.transaction():
            row = self.status(task_key)
            if row is None or row['status'] in TERMINAL: raise ValueError('grant is terminal')
            combined = row['fields']|fields
            self.db.execute('UPDATE burns SET status=?,fields=? WHERE task_key=?',
                            (status, json.dumps(combined, sort_keys=True, allow_nan=False), task_key))

    def close(self):
        if self.db is not None: self.db.close(); self.db = None
        if self.owner is not None: self.owner.close(); self.owner = None
