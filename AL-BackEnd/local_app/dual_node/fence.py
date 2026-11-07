"""Sticky legacy exclusion and a non-activating enrollment boundary.

No coordinator calls, credentials or school I/O. A separate anchor remembers
enrollment even if the optional dual-node configuration disappears.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ..models import AppError


@dataclass(frozen=True)
class NativeFenceAck:
    account_id: str
    native_identity: str
    state_version: int
    enabled: bool
    plan: bool
    drained: bool


def _deny(code='dual_legacy_fenced'):
    raise AppError(code, '此账号已由双机执行器管理，原发送路径保持停用；请检查双机状态', 409)


def _account(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        _deny('dual_fence_invalid')
    return value


def _private(path, directory=False):
    info=path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        _deny('dual_fence_invalid')
    return info


class ManagedAccountGate:
    """No unmanage/reset API. Registry faults deny legacy for every account."""
    def __init__(self, data_dir, *, wall_clock=time.time):
        configured=Path(data_dir)
        # Existing macOS Runtime directories use the OS /var and /tmp aliases.
        # Canonicalize those only; operator-created aliases remain a refusal.
        aliases={'/var','/tmp'} if os.uname().sysname=='Darwin' else set()
        self._unsafe_alias=(not configured.is_absolute() or configured.is_symlink()
            or any(path.is_symlink() and str(path) not in aliases for path in configured.parents))
        self.directory=configured.resolve()
        self.anchor_path=self.directory/'dual-managed.anchor.json'
        self.registry_path=self.directory/'dual-managed.registry.json'
        self._registration_lock=self.directory/'dual-managed.lock'
        self._post_lock=self.directory/'dual-legacy-post.lock'
        self._post_anchor=self.directory/'dual-legacy-post.anchor.json'
        self.auth_windows_path=self.directory/'dual-managed-auth-windows.json'
        self.wall_clock=wall_clock
        self._auth_accounts=self._auth_student=None

    def _directory(self):
        if (self._unsafe_alias or not self.directory.is_absolute()
                or any(path.is_symlink() for path in (self.directory,*self.directory.parents))):
            _deny('dual_fence_invalid')
        _private(self.directory, True)

    def _read(self, path):
        expected=_private(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino):_deny('dual_fence_invalid')
            raw=stream.read(262145)
        if len(raw)>262144:_deny('dual_fence_invalid')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError()
                result[key]=value
            return result
        return json.loads(raw,object_pairs_hook=unique)

    def _load(self, *, initializing=False):
        self._directory()
        present=[os.path.lexists(path) for path in (self.anchor_path,self.registry_path)]
        if not any(present):
            if not initializing and os.path.lexists(self._registration_lock):_deny('dual_fence_invalid')
            return None,{}
        if not all(present):_deny('dual_fence_invalid')
        anchor,registry=self._read(self.anchor_path),self._read(self.registry_path)
        if (type(anchor) is not dict or set(anchor)!={'schema','registry_id','accounts'}
                or type(anchor['schema']) is not int or anchor['schema']!=1
                or type(anchor['registry_id']) is not str or not re.fullmatch('[0-9a-f]{32}',anchor['registry_id'])
                or type(anchor['accounts']) is not list or not anchor['accounts']
                or any(type(value) is not str for value in anchor['accounts'])
                or anchor['accounts']!=sorted(set(anchor['accounts']))
                or type(registry) is not dict or set(registry)!={'schema','registry_id','accounts'}
                or type(registry['schema']) is not int or registry['schema']!=1
                or registry['registry_id']!=anchor['registry_id'] or type(registry['accounts']) is not dict
                or sorted(registry['accounts'])!=anchor['accounts']):_deny('dual_fence_invalid')
        for account,row in registry['accounts'].items():
            _account(account)
            if (type(row) is not dict or not {'fence_id','state','native_ack'}<=set(row)
                    or set(row)-{'fence_id','state','native_ack','student_hash','auth_seeded','auth_window_hash'}
                    or type(row['fence_id']) is not str or not re.fullmatch('[0-9a-f]{32}',row['fence_id'])
                    or row['state'] not in ('pending','staged')):_deny('dual_fence_invalid')
            if row['state']=='staged':self._ack(account,NativeFenceAck(**row['native_ack']))
            elif row['native_ack'] is not None:_deny('dual_fence_invalid')
            if ('student_hash' in row and (type(row['student_hash']) is not str
                    or not re.fullmatch('[0-9a-f]{64}',row['student_hash']))):_deny('dual_fence_invalid')
            if 'auth_seeded' in row and row['auth_seeded'] is not True:_deny('dual_fence_invalid')
            if ('auth_window_hash' in row and (type(row['auth_window_hash']) is not str
                    or not re.fullmatch('[0-9a-f]{64}',row['auth_window_hash']))):_deny('dual_fence_invalid')
            if row.get('auth_seeded') and not {'student_hash','auth_window_hash'}<=set(row):_deny('dual_fence_invalid')
        identities=[row['student_hash'] for row in registry['accounts'].values() if 'student_hash' in row]
        if len(identities)!=len(set(identities)):_deny('dual_fence_invalid')
        return anchor,registry['accounts']

    def _snapshot(self):
        try:return self._load()
        except (OSError,ValueError,TypeError,KeyError):_deny('dual_fence_invalid')

    def is_managed(self, account_id):
        _account(account_id)
        try:
            accounts=self._snapshot()[1]
            return account_id in accounts or self._bound_identity_account(account_id,accounts)
        except Exception:return True

    def has_managed_scope(self):
        """Read-only lifecycle evidence; registry faults must remain visible."""
        return bool(self._snapshot()[1])

    def require_legacy(self, run_or_account):
        account_id=_account(getattr(run_or_account,'account_id',run_or_account))
        accounts=self._snapshot()[1]
        if account_id in accounts or self._bound_identity_account(account_id,accounts):_deny()

    def _bound_identity_account(self, account_id, accounts):
        if not accounts or self._auth_student is None:return False
        try:
            digest=self._student_hash(self._auth_student(account_id))
            return any(row.get('student_hash')==digest for row in accounts.values())
        except Exception:_deny('dual_auth_window_unavailable')

    @staticmethod
    def _student_hash(student):
        if type(student) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{2,64}',student.strip()):
            _deny('dual_auth_window_unavailable')
        return hashlib.sha256(student.strip().casefold().encode()).hexdigest()

    def set_auth_resolver(self, accounts_for_student, student_for_account):
        if not callable(accounts_for_student) or not callable(student_for_account):
            _deny('dual_auth_window_unavailable')
        self._auth_accounts,self._auth_student=accounts_for_student,student_for_account

    def _identity_accounts(self, student, accounts):
        digest=self._student_hash(student)
        bound=[key for key,row in accounts.items() if row.get('student_hash')==digest]
        if bound:return bound
        if self._auth_accounts is None:
            if any('student_hash' not in row for row in accounts.values()):
                _deny('dual_auth_window_unavailable')
            return []
        ids=self._auth_accounts(student)
        if type(ids) is not list or len(ids)>1:_deny('dual_auth_window_unavailable')
        return [_account(value) for value in ids if value in accounts]

    def require_legacy_student(self, student):
        _,accounts=self._snapshot()
        if not accounts:return
        digest=self._student_hash(student)
        if self._auth_accounts is None and not any(row.get('student_hash')==digest for row in accounts.values()):
            return
        if self._identity_accounts(student,accounts):_deny()

    def _auth_windows(self, anchor, accounts, now):
        value=self._read(self.auth_windows_path)
        if (type(value) is not dict or set(value)!={'schema','registry_id','generated_at','windows'}
                or type(value['schema']) is not int or value['schema']!=1
                or value['registry_id']!=anchor['registry_id']
                or not self._auth_time(value['generated_at']) or value['generated_at']>now+5
                or type(value['windows']) is not dict
                or set(value['windows'])!={key for key,row in accounts.items() if row.get('auth_seeded')}):
            _deny('dual_auth_window_unavailable')
        for account_id,releases in value['windows'].items():
            if (type(releases) is not list or len(releases)>128
                    or any(not self._auth_time(due) or due>value['generated_at']+8*86400 for due in releases)
                    or releases!=sorted(set(releases))):_deny('dual_auth_window_unavailable')
            if accounts[account_id]['auth_window_hash']!=self._window_hash(releases):
                _deny('dual_auth_window_unavailable')
        return value

    @staticmethod
    def _window_hash(releases):
        return hashlib.sha256(json.dumps(releases,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _auth_time(value):
        try:return type(value) in (int,float) and math.isfinite(value) and 0<=value<=4102444800
        except OverflowError:return False

    def remember_auth_window(self, account_id, release_at):
        """Persist before publishing; an old granted release survives plan edits."""
        lock=None
        try:
            account_id=_account(account_id);now=self.wall_clock()
            if (not self._auth_time(now) or not self._auth_time(release_at)
                    or not now-40<=release_at<=now+8*86400):_deny('dual_auth_window_unavailable')
            lock=self._open_lock(self._registration_lock)
            self._lock_until(lock,fcntl.LOCK_EX,time.monotonic()+1)
            anchor,accounts=self._load()
            if account_id not in accounts or self._auth_student is None:_deny('dual_auth_window_unavailable')
            digest=self._student_hash(self._auth_student(account_id))
            row=accounts[account_id]
            if row.get('student_hash',digest)!=digest:_deny('dual_auth_window_unavailable')
            if any(key!=account_id and old.get('student_hash')==digest for key,old in accounts.items()):
                _deny('dual_auth_window_unavailable')
            if os.path.lexists(self.auth_windows_path):
                value=self._auth_windows(anchor,accounts,now)
            else:
                if any(old.get('auth_seeded') for old in accounts.values()):_deny('dual_auth_window_unavailable')
                value={'schema':1,'registry_id':anchor['registry_id'],'generated_at':now,'windows':{}}
            # A repeated publisher sync performs reads only. Expired windows are
            # removed only when a genuinely new release must be recorded.
            if row.get('auth_seeded') and release_at in value['windows'][account_id]:return
            windows={key:[due for due in dues if due+40>=now] for key,dues in value['windows'].items()}
            windows[account_id]=sorted(set((*windows.get(account_id,[]),float(release_at))))
            if len(windows[account_id])>128:_deny('dual_auth_window_unavailable')
            accounts[account_id]={**row,'student_hash':digest,'auth_seeded':True}
            for key,dues in windows.items():accounts[key]={**accounts[key],'auth_window_hash':self._window_hash(dues)}
            # A crash before the following snapshot write stays closed. The
            # permanent registry also detects restoration of an older snapshot.
            self._replace(self.registry_path,{'schema':1,'registry_id':anchor['registry_id'],'accounts':accounts})
            self._replace(self.auth_windows_path,{**value,'generated_at':now,'windows':windows})
        except AppError:raise
        except Exception:_deny('dual_auth_window_unavailable')
        finally:
            if lock is not None:os.close(lock)

    def require_auth(self, account_id):
        try:
            account_id=_account(account_id);anchor,accounts=self._snapshot()
            if account_id not in accounts:return
            now=self.wall_clock()
            if not self._auth_time(now):_deny('dual_auth_window_unavailable')
            windows=self._auth_windows(anchor,accounts,now)['windows']
            if account_id not in windows:_deny('dual_auth_window_unavailable')
            if any(due-183<=now<=due+40 for due in windows[account_id]):
                raise AppError('dual_auth_window','双机任务正在准备或执行，请稍后重新登录；现有会话仍可查询',409)
        except AppError:raise
        except Exception:_deny('dual_auth_window_unavailable')

    def require_auth_student(self, student):
        try:
            _,accounts=self._snapshot()
            if not accounts:return
            for account_id in self._identity_accounts(student,accounts):self.require_auth(account_id)
        except AppError:raise
        except Exception:_deny('dual_auth_window_unavailable')

    def project(self, account_id, desired, binding):
        if not self.is_managed(account_id):return desired,binding
        try:row=self._snapshot()[1].get(account_id)
        except AppError:row=None
        fence={'state':row['state'],'fence_id':row['fence_id']} if row else {'state':'blocked'}
        return {**desired,'enabled':False,'plan':False},{**binding,'dual_legacy_fence':fence}

    def _open_lock(self, path):
        self._directory()
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=_private(path);actual=os.fstat(fd)
            if (info.st_dev,info.st_ino)!=(actual.st_dev,actual.st_ino):_deny('dual_fence_invalid')
            return fd
        except BaseException:os.close(fd);raise

    def _replace(self, path, value):
        temporary=self.directory/('.dual-fence-'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream,sort_keys=True,allow_nan=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
            if os.path.lexists(path):_private(path)
            os.replace(temporary,path)
            parent=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if temporary.exists():temporary.unlink()  # This call's exclusive temporary file only.

    def _open_post_lock(self):
        """The independent identity record rejects a replaced drain lock."""
        self._directory()
        try:
            fd=os.open(self._post_lock,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            created=True
        except FileExistsError:
            fd=os.open(self._post_lock,os.O_RDWR|os.O_NOFOLLOW)
            created=False
        try:
            info=_private(self._post_lock);actual=os.fstat(fd)
            if (info.st_dev,info.st_ino)!=(actual.st_dev,actual.st_ino):_deny('dual_fence_invalid')
            expected={'device':info.st_dev,'inode':info.st_ino}
            if created and not os.path.lexists(self._post_anchor):
                self._replace(self._post_anchor,expected)
            elif not created:
                # A concurrent first POST may still be fsyncing its anchor.
                deadline=time.monotonic()+.1
                while not os.path.lexists(self._post_anchor) and time.monotonic()<deadline:time.sleep(.002)
            recorded=self._read(self._post_anchor)
            if (type(recorded) is not dict or set(recorded)!=set(expected)
                    or any(type(value) is not int for value in recorded.values())
                    or recorded!=expected):_deny('dual_fence_invalid')
            return fd
        except BaseException:os.close(fd);raise

    @staticmethod
    def _ack(account, ack):
        if (type(ack) is not NativeFenceAck or ack.account_id!=account
                or type(ack.native_identity) is not str or not re.fullmatch('[0-9a-f]{64}',ack.native_identity)
                or type(ack.state_version) is not int or ack.state_version<1
                or ack.enabled is not False or ack.plan is not False or ack.drained is not True):
            _deny('dual_native_fence_unconfirmed')
        return ack

    def stage(self, account_id, *, native_disable, timeout=1.):
        """Permanently fence first, then require native ACK/drain and drain HTTP.

        native_disable must read back disabled native state and establish that
        earlier native sends/results are drained/classified. Failure keeps the
        pending fence. No server activation or claim of undoing an old POST.
        """
        account_id=_account(account_id)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<=timeout<=30:_deny('dual_fence_invalid')
        try:
            self._directory()
            fresh=not any(os.path.lexists(path) for path in (self.anchor_path,self.registry_path,self._registration_lock))
            lock=self._open_lock(self._registration_lock)
            try:
                deadline=time.monotonic()+timeout
                self._lock_until(lock,fcntl.LOCK_EX,deadline)
                anchor,accounts=self._load(initializing=fresh)
                if anchor is None:anchor={'schema':1,'registry_id':uuid.uuid4().hex,'accounts':[]}
                if account_id not in accounts:
                    accounts[account_id]={'fence_id':uuid.uuid4().hex,'state':'pending','native_ack':None}
                    anchor={**anchor,'accounts':sorted(accounts)}
                    # A crash between these independent records refuses legacy.
                    self._replace(self.anchor_path,anchor)
                    self._replace(self.registry_path,{'schema':1,'registry_id':anchor['registry_id'],'accounts':accounts})
                if self._auth_student is not None:
                    digest=self._student_hash(self._auth_student(account_id))
                    if accounts[account_id].get('student_hash',digest)!=digest:_deny('dual_auth_window_unavailable')
                    if any(key!=account_id and row.get('student_hash')==digest for key,row in accounts.items()):
                        _deny('dual_auth_window_unavailable')
                    if 'student_hash' not in accounts[account_id]:
                        accounts[account_id]={**accounts[account_id],'student_hash':digest}
                        self._replace(self.registry_path,{'schema':1,'registry_id':anchor['registry_id'],'accounts':accounts})
                ack=self._ack(account_id,native_disable())
                post_lock=self._open_post_lock()
                try:
                    self._lock_until(post_lock,fcntl.LOCK_EX,deadline)
                    accounts[account_id]={**accounts[account_id],'state':'staged','native_ack':asdict(ack)}
                    self._replace(self.registry_path,{'schema':1,'registry_id':anchor['registry_id'],'accounts':accounts})
                finally:os.close(post_lock)
                return {'account_id':account_id,**accounts[account_id],'server_activated':False}
            finally:os.close(lock)
        except AppError:raise
        except (OSError,ValueError,TypeError,KeyError):_deny('dual_fence_invalid')

    @staticmethod
    def _lock_until(fd, operation, deadline):
        while True:
            try:fcntl.flock(fd,operation|fcntl.LOCK_NB);return
            except BlockingIOError:
                if time.monotonic()>=deadline:_deny('dual_drain_pending')
                time.sleep(.005)

    @contextmanager
    def post_scope(self, run, *, student_id=None):
        """Shared lock spans actual POST, closing activation/check races."""
        fd=None
        try:
            fd=self._open_post_lock()
            self._lock_until(fd,fcntl.LOCK_SH,time.monotonic()+.5)
            if student_id is not None:self.require_legacy_student(student_id)
            if run is None:
                if self._snapshot()[1]:_deny()
            else:self.require_legacy(run)
            yield
        except AppError:raise
        except (OSError,ValueError,TypeError,KeyError):_deny('dual_fence_invalid')
        finally:
            if fd is not None:os.close(fd)

    def bind_client(self, client):
        client.ownership_guard=_LegacyClientGuard(self,getattr(client,'student_id',None))
        return client

    def client_factory(self, factory):
        def create(*args,**kwargs):return self.bind_client(factory(*args,**kwargs))
        create.isolated_fixture=getattr(factory,'isolated_fixture',False)
        return create


class _LegacyClientGuard:
    def __init__(self, gate, student_id):self.gate,self.student_id=gate,student_id
    def __call__(self):return False
    def auth(self):self.gate.require_auth_student(self.student_id)
    def post(self, run):return self.gate.post_scope(run,student_id=self.student_id)


def require_legacy(accounts, run_or_account):
    gate=getattr(accounts,'managed_gate',None)
    if gate is not None:gate.require_legacy(run_or_account)


def legacy_allowed(accounts, run_or_account):
    try:require_legacy(accounts,run_or_account);return True
    except AppError:return False


class GrantClientPermit:
    """A separate fresh headless client, bound to one verified signed grant.

    This cannot replace a bound legacy guard. It permits only the original
    scheduled run and its signed seat priorities while the executor's local
    readiness/lease remains valid. It performs no transport or SSH operation.
    """
    def __init__(self, grant, *, task, run, node_id, mode, authority_id,
                 ready, wall_clock=time.time):
        from .contracts import task_identity,validate_task
        from .grants import require_verified_grant
        self.grant=require_verified_grant(grant)
        self.task=validate_task(task)
        task_id,binding=task_identity(self.task)
        if (grant.mode!=mode or mode not in ('fixture','production')
                or grant.node_id!=node_id or node_id not in ('mac','ubuntu')
                or grant.authority_id!=authority_id or grant.task_id!=task_id
                or grant.binding_sha256!=binding
                or grant.credential_revision!=self.task['credential_revision']
                or not callable(ready) or not callable(wall_clock)):
            raise ValueError('grant_binding_denied')
        self.ready,self.wall_clock=ready,wall_clock
        self._run_binding=self._binding(run)
        def text(minute):return f'{minute//60:02d}:{minute%60:02d}'
        expected=(self.task['account_ref'],self.task['revision'],self.task['day'],
                  'scheduled',None,self.task['release_at'],text(self.task['start_minute']),
                  text(self.task['end_minute']),tuple(self.task['seat_priorities']))
        if self._run_binding!=expected:raise ValueError('grant_run_denied')

    @staticmethod
    def _binding(run):
        if run is None:raise ValueError('grant_run_denied')
        try:
            return (run.account_id,run.revision,run.target_date,run.kind,run.operation_id,
                    run.due_at,run.begin,run.end,tuple(run.seat_ids))
        except (AttributeError,TypeError):raise ValueError('grant_run_denied') from None

    def __call__(self):
        try:
            if self.ready() is not True:return False
            # Readiness may inspect the local executor. Clock is sampled last.
            now=self.wall_clock()
            return (type(now) in (int,float) and math.isfinite(now)
                    and self.grant.release_at<=now<=self.grant.expires_at)
        except Exception:return False

    @contextmanager
    def post(self, run):
        if self._binding(run)!=self._run_binding or not self():raise ValueError('grant_send_denied')
        yield
