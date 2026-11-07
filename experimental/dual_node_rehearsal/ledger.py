"""One-owner decisions on one private local SQLite ledger, for rehearsal only."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from .contracts import (NODE_IDS, RehearsalError, canonical_hash, envelope,
                        intent_identity, safe_read_json, wall_time)

STATES = {'selected', 'consumed', 'expired', 'revoked', 'uncertain', 'completed'}
TERMINAL = {'expired', 'revoked', 'uncertain', 'completed'}
FILES = ('identity.json', 'owner.lock', 'state.db')


def _fail(code='ledger_invalid'):
    raise RehearsalError(code)


def _state_path(value):
    try:
        path = Path(value)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            _fail('unsafe_state_path')
        return path.resolve()
    except (OSError, TypeError, ValueError):
        _fail('unsafe_state_path')


def _private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        _fail('unsafe_state_path')
    return info


def _coordinates(path, names=FILES[1:]):
    values = {'path': str(path)}
    for name in ('.', *names):
        info = _private(path if name == '.' else path/name, directory=name == '.')
        prefix = 'directory' if name == '.' else name
        values[prefix + '_device'] = info.st_dev
        values[prefix + '_inode'] = info.st_ino
    return values


def _new_file(path, data=b''):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def _evidence(value):
    if (type(value) is not dict or set(value) != set(NODE_IDS)
            or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}', v)
                   for v in value.values())):
        _fail('invalid_proposal')
    return dict(value)


def _flags(value):
    return (type(value) is dict and value.get('mode') == envelope()['mode']
            and value.get('rehearsal_only') is True
            and value.get('live_execution_authorized') is False)


def _proposal(intent, proposal, task, binding, now):
    try:
        if (not _flags(proposal) or proposal.get('status') != 'proposed'
                or proposal['selected_node'] not in NODE_IDS
                or proposal['task_id'] != task or proposal['binding_sha256'] != binding
                or wall_time(proposal['release_at']) != wall_time(intent['release_at'])
                or wall_time(proposal['expires_at']) != intent['release_at'] + 30
                or not 305 <= intent['release_at'] - now <= 900
                or not 0 <= now - wall_time(proposal['evaluated_at']) <= 5
                or proposal['evidence_trust'] != 'operator_imported_unsigned'
                or proposal['booking_readiness'] != 'not_checked'
                or proposal['clock_accuracy'] != 'not_verified'
                or type(proposal['eligible_nodes']) is not list
                or not proposal['eligible_nodes']
                or len(set(proposal['eligible_nodes'])) != len(proposal['eligible_nodes'])
                or any(n not in NODE_IDS for n in proposal['eligible_nodes'])
                or proposal['selected_node'] not in proposal['eligible_nodes']
                or proposal['incumbent'] not in NODE_IDS):
            _fail('invalid_proposal')
        return proposal['selected_node'], _evidence(proposal['evidence_sha256'])
    except (KeyError, TypeError, ValueError):
        _fail('invalid_proposal')


class Authority:
    """Local file identity checks and SQLite transactions, not remote fencing.

    Mutations durably observe a valid ``now`` after acquiring the owner lock and
    validating this private ledger. That highwater commits before any business
    validation or writes, so semantic refusals cannot erase an observed time.
    Invalid clocks/identity, lock contention or a failed highwater commit never
    enter the business transaction. Inspect is read-only and observes no time.
    """
    @classmethod
    def initialize(cls, state_path):
        path = _state_path(state_path)
        try:
            path.mkdir(mode=0o700, exist_ok=False)
            _new_file(path/'owner.lock'); _new_file(path/'state.db')
            marker = {'schema': 1, 'ledger_id': uuid.uuid4().hex, **_coordinates(path)}
            _new_file(path/'identity.json', (json.dumps(marker, sort_keys=True)+'\n').encode())
            info = _private(path/'identity.json')
            metadata = {'schema': '1', 'ledger_id': marker['ledger_id'],
                        'marker_sha256': canonical_hash(marker),
                        'marker_device': str(info.st_dev), 'marker_inode': str(info.st_ino),
                        'last_wall': '0.0'}
            db = sqlite3.connect(path/'state.db', isolation_level=None, timeout=.5)
            try:
                db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA synchronous=EXTRA')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, '
                           'intent_json TEXT NOT NULL, owner TEXT NOT NULL, decision_id TEXT NOT NULL UNIQUE, '
                           'state TEXT NOT NULL, created_at REAL NOT NULL, release_at REAL NOT NULL, '
                           'expires_at REAL NOT NULL, evidence_json TEXT NOT NULL)')
                db.executemany('INSERT INTO meta VALUES(?,?)', metadata.items())
                db.commit()
            finally:
                db.close()
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except RehearsalError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            _fail('initialize_refused')
        return cls(path)

    def __init__(self, state_path):
        self.db = None; self.lock_fd = None; self._mutex = threading.RLock()
        try:
            self.path = _state_path(state_path); _private(self.path, directory=True)
            _private(self.path/'identity.json'); self.marker = safe_read_json(self.path/'identity.json')
            if (type(self.marker) is not dict or type(self.marker.get('schema')) is not int
                    or self.marker['schema'] != 1
                    or type(self.marker.get('ledger_id')) is not str
                    or not re.fullmatch('[0-9a-f]{32}', self.marker['ledger_id'])
                    or self.marker != {'schema': 1, 'ledger_id': self.marker['ledger_id'],
                                       **_coordinates(self.path)}):
                _fail()
            self.ledger_id = self.marker['ledger_id']
            self.identities = {name: (_private(self.path/name).st_dev,
                                     _private(self.path/name).st_ino) for name in FILES}
            with (self.path/'state.db').open('rb') as stream:
                if stream.read(16) != b'SQLite format 3\0': _fail()
            self.lock_fd = os.open(self.path/'owner.lock', os.O_RDWR | os.O_NOFOLLOW)
            self.db = sqlite3.connect((self.path/'state.db').as_uri()+'?mode=rw', uri=True,
                                      isolation_level=None, timeout=.5, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete': _fail()
            self.db.execute('PRAGMA synchronous=EXTRA')
            if self.db.execute('PRAGMA synchronous').fetchone()[0] != 3: _fail()
            self._check()
        except RehearsalError:
            self.close(); raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self.close(); _fail()

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd); self.lock_fd = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def _check(self):
        if self.db is None or self.lock_fd is None: _fail('authority_closed')
        if _coordinates(self.path) != {k: v for k, v in self.marker.items()
                                      if k not in ('schema', 'ledger_id')}: _fail()
        for name, identity in self.identities.items():
            info = _private(self.path/name)
            if (info.st_dev, info.st_ino) != identity: _fail()
        # SQLite can retain a cached first page after out-of-band damage.
        # Inspect the physical header as well as its logical integrity check.
        fd = os.open(self.path/'state.db', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if ((info.st_dev, info.st_ino) != self.identities['state.db']
                    or stream.read(16) != b'SQLite format 3\0'): _fail()
        info = os.fstat(self.lock_fd)
        if (info.st_dev, info.st_ino) != self.identities['owner.lock']: _fail()
        if safe_read_json(self.path/'identity.json') != self.marker: _fail()
        if self.db.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok': _fail()
        meta = dict(self.db.execute('SELECT key,value FROM meta'))
        expected = {'schema': '1', 'ledger_id': self.ledger_id,
                    'marker_sha256': canonical_hash(self.marker),
                    'marker_device': str(self.identities['identity.json'][0]),
                    'marker_inode': str(self.identities['identity.json'][1])}
        if set(meta) != {*expected, 'last_wall'} or any(meta[k] != v for k, v in expected.items()): _fail()
        wall_time(float(meta['last_wall']))
        return meta

    @contextmanager
    def _mutation(self, now):
        now = wall_time(now)
        with self._mutex:
            locked = False
            try:
                deadline = time.monotonic() + .75
                while True:
                    try: fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB); locked=True; break
                    except BlockingIOError:
                        if time.monotonic() >= deadline: _fail('authority_busy')
                        time.sleep(.01)
                self._check(); self.db.execute('BEGIN IMMEDIATE')
                last = float(self.db.execute("SELECT value FROM meta WHERE key='last_wall'").fetchone()[0])
                if now < last: _fail('clock_rollback')
                self.db.execute("UPDATE meta SET value=? WHERE key='last_wall'", (repr(now),))
                # Keep the same flock across both transactions. A refusal or
                # rollback of task changes must not roll back this observation.
                self.db.commit()
                self.db.execute('BEGIN IMMEDIATE')
                yield now
                self.db.commit()
            except RehearsalError:
                if self.db is not None: self.db.rollback()
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                if self.db is not None: self.db.rollback()
                _fail('authority_busy' if time.monotonic() >= deadline else 'ledger_invalid')
            finally:
                if locked: fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _row(self, task_id):
        if type(task_id) is not str or not re.fullmatch('[0-9a-f]{64}', task_id): _fail('invalid_task_id')
        row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None: return None
        value = dict(row)
        intent = json.loads(value.pop('intent_json'))
        task, binding = intent_identity(intent)
        evidence = _evidence(json.loads(value.pop('evidence_json')))
        if (task != value['task_id'] or binding != value['binding_sha256']
                or value['owner'] not in NODE_IDS or value['state'] not in STATES
                or not re.fullmatch('[0-9a-f]{32}', value['decision_id'])
                or wall_time(value['release_at']) != intent['release_at']
                or wall_time(value['expires_at']) != intent['release_at']+30
                or not 305 <= value['release_at']-wall_time(value['created_at']) <= 900): _fail()
        return envelope(**value, ledger_id=self.ledger_id, evidence_sha256=evidence,
                        created=False, simulation_consumed=False, reason='stored_assignment')

    def assign(self, intent, proposal, *, now):
        with self._mutation(now) as current:
            task, binding = intent_identity(intent)
            existing = self._row(task)
            if existing is not None:
                if existing['binding_sha256'] != binding: _fail('binding_conflict')
                return existing | {'reason': 'existing_assignment'}
            owner, evidence = _proposal(intent, proposal, task, binding, current)
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                (task, binding, json.dumps(intent, sort_keys=True), owner, uuid.uuid4().hex,
                 'selected', current, float(intent['release_at']), float(intent['release_at'])+30,
                 json.dumps(evidence, sort_keys=True)))
            return self._row(task) | {'created': True, 'reason': 'selected_once'}

    def _matching(self, assignment, node):
        if not _flags(assignment) or node not in NODE_IDS: _fail('invalid_assignment')
        stored = self._row(assignment.get('task_id'))
        if stored is None: _fail('unknown_assignment')
        for key in ('ledger_id', 'task_id', 'binding_sha256', 'decision_id', 'owner',
                    'created_at', 'release_at', 'expires_at', 'evidence_sha256'):
            if assignment.get(key) != stored[key]: _fail('wrong_assignment')
        if node != stored['owner']: _fail('wrong_owner')
        return stored

    def consume(self, assignment, *, node, now):
        with self._mutation(now) as current:
            stored = self._matching(assignment, node)
            if stored['state'] != 'selected': return stored | {'reason': 'already_spent'}
            if current < stored['release_at']: return stored | {'reason': 'before_release'}
            if current > stored['expires_at']:
                self.db.execute("UPDATE tasks SET state='expired' WHERE task_id=? AND state='selected'", (stored['task_id'],))
                return self._row(stored['task_id']) | {'reason': 'expired'}
            cursor = self.db.execute("UPDATE tasks SET state='consumed' WHERE task_id=? AND state='selected' AND owner=? AND decision_id=?",
                                     (stored['task_id'], node, stored['decision_id']))
            return self._row(stored['task_id']) | {'simulation_consumed': cursor.rowcount == 1,
                                                  'reason': 'simulation_consumed_once'}

    def finish(self, assignment, *, node, status, now):
        with self._mutation(now):
            if status not in ('revoked', 'uncertain', 'completed'): _fail('invalid_outcome')
            stored = self._matching(assignment, node)
            if stored['state'] in TERMINAL: return stored | {'reason': 'terminal_unchanged'}
            if status != 'revoked' and stored['state'] != 'consumed': _fail('invalid_transition')
            self.db.execute('UPDATE tasks SET state=? WHERE task_id=? AND state=?',
                            (status, stored['task_id'], stored['state']))
            return self._row(stored['task_id']) | {'reason': 'finished'}

    def inspect(self, task_id):
        with self._mutex:
            try:
                self._check()
                return self._row(task_id)
            except RehearsalError: raise
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError): _fail()
