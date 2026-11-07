"""Opaque coordinator references resolve through an injected local vault only."""
from dataclasses import dataclass
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from ..models import credentials
from ..school import CampusClient
from ..vault import CredentialVault
from .journal import _private, _create


@dataclass(frozen=True, repr=False)
class CredentialBinding:
    student_id: str
    credential_revision: int
    secret: object


class CredentialProvider:
    def __init__(self, resolve, unseal):
        if not callable(resolve) or not callable(unseal): raise ValueError('local vault callbacks required')
        self._resolve, self._unseal = resolve, unseal

    def binding(self, task):
        try:
            row = self._resolve(task['account_ref'])
            revision = row['credential_revision']
            if type(revision) is not int or revision != task['credential_revision'] or revision < 1:
                raise ValueError()
            student, _ = credentials(row['student_id'], 'validation-only')
            return CredentialBinding(student, revision, row['secret'])
        except Exception:
            raise ValueError('local credential binding unavailable') from None

    def open_client(self, task, *, client_factory=CampusClient):
        binding = self.binding(task)
        try:
            password = self._unseal(task['account_ref'], binding.secret)
            student, password = credentials(binding.student_id, password)
            return client_factory(student, password, allow_live=False)
        except Exception:
            raise ValueError('local credential unavailable') from None


class CredentialStore:
    """Explicit private AES-GCM store; anchor/key loss never creates replacement state."""
    @classmethod
    def initialize(cls, directory):
        root = Path(directory).absolute(); root.mkdir(mode=0o700, exist_ok=False)
        _create(root/'key', os.urandom(32)); _create(root/'owner.lock'); _create(root/'accounts.db')
        db = sqlite3.connect(root/'accounts.db')
        try:
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE accounts(ref TEXT PRIMARY KEY,student TEXT NOT NULL COLLATE NOCASE UNIQUE,revision INTEGER NOT NULL,secret TEXT NOT NULL)')
            store_id = uuid.uuid4().hex
            db.execute('INSERT INTO meta VALUES(?,?)', ('identity', store_id)); db.commit()
        finally: db.close()
        marker = {'schema':1, 'identity':store_id,
            'key_hash':hashlib.sha256((root/'key').read_bytes()).hexdigest(),
            'data_hash':hashlib.sha256(b'[]').hexdigest()}
        _create(root/'identity.json', json.dumps(marker, sort_keys=True).encode())
        fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return cls(root)

    def __init__(self, directory):
        self.path = Path(directory).absolute(); self.owner = self.db = None
        self.lock = threading.RLock()
        try:
            _private(self.path, True)
            for name in ('key', 'owner.lock', 'accounts.db', 'identity.json'): _private(self.path/name)
            self.key = (self.path/'key').read_bytes()
            if len(self.key) != 32: raise ValueError('invalid credential key')
            fd = os.open(self.path/'owner.lock', os.O_RDWR|os.O_NOFOLLOW)
            self.owner = os.fdopen(fd, 'r+'); fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with (self.path/'accounts.db').open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\0': raise ValueError('invalid credential database')
            self.db = sqlite3.connect((self.path/'accounts.db').as_uri()+'?mode=rw', uri=True,
                isolation_level=None, check_same_thread=False)
            if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ValueError('credential journal mode changed')
            self.db.execute('PRAGMA synchronous=EXTRA')
            if self.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]: raise ValueError('credential integrity')
            indexes = self.db.execute('PRAGMA index_list(accounts)').fetchall()
            if not any(row[2] and [entry[2] for entry in self.db.execute('PRAGMA index_info('+row[1]+')')] == ['student']
                       for row in indexes):
                raise ValueError('credential identity uniqueness missing; no automatic migration')
            self.vault = CredentialVault(self.key); self._check()
        except Exception:
            self.close(); raise ValueError('credential store unavailable; original state retained') from None

    def _data_hash(self):
        rows = self.db.execute('SELECT ref,student,revision,secret FROM accounts ORDER BY ref').fetchall()
        return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()

    def _check(self):
        if self.db is None: raise ValueError('credential store closed')
        for name in ('key', 'owner.lock', 'accounts.db', 'identity.json'): _private(self.path/name)
        if (self.path/'key').read_bytes() != self.key: raise ValueError('credential key changed')
        raw = (self.path/'identity.json').read_bytes()
        if len(raw) > 1024: raise ValueError('credential anchor invalid')
        marker = json.loads(raw)
        if (set(marker) != {'schema','identity','key_hash','data_hash'} or marker['schema'] != 1
                or marker['identity'] != self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0]
                or marker['key_hash'] != hashlib.sha256(self.key).hexdigest()
                or marker['data_hash'] != self._data_hash()): raise ValueError('credential anchor mismatch')
        return marker

    def _anchor(self, marker):
        temporary = self.path/('identity-'+uuid.uuid4().hex+'.part')
        _create(temporary, json.dumps(marker, sort_keys=True).encode())
        os.replace(temporary, self.path/'identity.json')
        fd = os.open(self.path, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def enroll(self, account_ref, student_id, password, credential_revision, *, expected_revision=None):
        if (not isinstance(account_ref, str) or not re.fullmatch(r'(?:acct|fixture)-[A-Za-z0-9_-]{1,80}', account_ref)
                or type(credential_revision) is not int or not 1 <= credential_revision <= 2147483647):
            raise ValueError('invalid local credential enrollment')
        student, password = credentials(student_id, password)
        with self.lock:
            marker = self._check()
            if self.db.execute('SELECT 1 FROM accounts WHERE student=? COLLATE NOCASE AND ref!=?', (student, account_ref)).fetchone():
                raise ValueError('school identity already belongs to another account reference')
            previous = self.db.execute('SELECT revision FROM accounts WHERE ref=?', (account_ref,)).fetchone()
            if (previous and (type(expected_revision) is not int or expected_revision != previous[0]
                              or credential_revision <= previous[0])) or (not previous and expected_revision is not None):
                raise ValueError('credential revision changed')
            sealed = self.vault.seal(account_ref, json.dumps({'student_id':student,
                'credential_revision':credential_revision, 'password':password}, separators=(',', ':')))
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT INTO accounts VALUES(?,?,?,?) ON CONFLICT(ref) DO UPDATE SET '
                    'student=excluded.student,revision=excluded.revision,secret=excluded.secret',
                    (account_ref, student, credential_revision, json.dumps(sealed, sort_keys=True)))
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise
            marker['data_hash'] = self._data_hash(); self._anchor(marker)

    def enroll_from_terminal(self, account_ref, student_id, credential_revision, *, expected_revision=None):
        password = getpass.getpass('School password (local encrypted storage): ')
        self.enroll(account_ref, student_id, password, credential_revision, expected_revision=expected_revision)

    def resolve(self, account_ref):
        with self.lock:
            self._check()
            row = self.db.execute('SELECT student,revision,secret FROM accounts WHERE ref=?', (account_ref,)).fetchone()
            if row is None: raise ValueError('local account reference missing')
            return {'student_id':row[0], 'credential_revision':row[1], 'secret':json.loads(row[2])}

    def unseal(self, account_ref, secret):
        with self.lock:
            row = self.resolve(account_ref)
            if secret != row['secret']: raise ValueError('local credential changed')
            value = json.loads(self.vault.unseal(account_ref, secret))
            if (value['student_id'] != row['student_id'] or value['credential_revision'] != row['credential_revision']):
                raise ValueError('local credential metadata mismatch')
            return credentials(value['student_id'], value['password'])[1]

    def provider(self):
        return CredentialProvider(self.resolve, self.unseal)

    def close(self):
        if self.db is not None: self.db.close(); self.db = None
        if self.owner is not None: self.owner.close(); self.owner = None
