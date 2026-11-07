"""One headless node, twenty local workers, durable grant before native ARM."""
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import threading
import time

from ..models import RunSpec
from ..school import CampusClient
from ..recovery_policy import (remaining_candidate_attempts, BUSY_RETRY_RESPONSE_COOLDOWN,
                               busy_retry_spacing, BUSY_REJECTION, OCCUPIED_REJECTION)
from ..reservations import records_match
from ..native.build import private_directory, read_build
from ..native.clock_pair import sample_clock_pair, wall_deadline_ns
from ..native.protocol import NativeProcess, Endpoint, clock_ns
from ..native.outbox import DurableOutbox
from ..native.platform_policy import validate_native_policy
from ..native.request import prepared_session, remember_preparation, capture_request, decode_result
from .contracts import task_identity, canonical_hash, result_diagnostics
from .credentials import CredentialProvider
from .grants import GrantVerifier
from .journal import BurnJournal
from .native_launcher import ClockPermit, LinuxVerifiedLauncher


def _run(task):
    def text(minute): return f'{minute//60:02d}:{minute%60:02d}'
    return RunSpec(task['account_ref'], task['revision'], task['day'],
        text(task['start_minute']), text(task['end_minute']), tuple(task['seat_priorities']),
        task['release_at'], expires_at=task['release_at']+30)


class HeadlessExecutor:
    def __init__(self, directory, build, node_id, credentials, verifier, *, endpoint=None,
                 time_permit=None, initialize=False, client_factory=CampusClient,
                 source_root=None, wall_clock=time.time):
        self.endpoint = endpoint or Endpoint.production(); self.endpoint.arguments()
        self.mode = 'fixture' if self.endpoint.fixture_mode else 'production'
        if type(verifier) is not GrantVerifier or verifier.mode != self.mode:
            raise ValueError('pinned endpoint-specific grant verifier required')
        if not isinstance(credentials, CredentialProvider): raise ValueError('local credential provider required')
        self.node_id, self.credentials, self.verifier = node_id, credentials, verifier
        self.build, self.client_factory, self.wall_clock = build, client_factory, wall_clock
        self.source_root = Path(source_root or Path(__file__).resolve().parents[3])
        checked = read_build(self.source_root, output=Path(build.executable).parent.parent)
        if (checked.binary_sha256 != build.binary_sha256 or checked.source_sha256 != build.source_sha256
                or checked.executable != build.executable): raise ValueError('native release changed')
        self.directory = Path(directory).absolute()
        if initialize:
            self.directory.mkdir(mode=0o700, exist_ok=False)
            journal = BurnJournal.initialize(self.directory/'grants'); journal.close()
        self.directory = private_directory(self.directory)
        if len(str(self.directory/'native'/'n00000000.sock').encode()) >= 104:
            raise ValueError('headless runtime path too long')
        self.journal = BurnJournal(self.directory/'grants')
        self.lock = threading.RLock(); self.contexts = {}; self.desired = {}
        self.native = self.outbox = None; self.time_permit = None
        self.stopping = threading.Event(); self.closed = False; self.heartbeat = None
        try: self.set_time_permit(time_permit)
        except Exception: self.journal.close(); raise

    def set_time_permit(self, permit):
        if (type(permit) is not ClockPermit or permit.mode != self.mode or permit.node_id != self.node_id
                or permit.verifier is not self.verifier): raise ValueError('bound signed time evidence required')
        permit.require_valid()
        self.time_permit = permit
        if self.native and self.native.launcher: self.native.launcher.time_permit = permit

    def _project(self, key):
        value = self.desired.get(key, {'enabled':False, 'plan':False, 'credential':0})
        return value, dict(value)

    def health(self):
        """Report process health without resetting a journal or reusing a worker."""
        with self.lock:
            if self.closed or self.stopping.is_set() or (self.outbox and self.outbox.failed):
                return 'unavailable'
            if self.native is not None:
                if (self.native.process is None or self.native.process.poll() is not None
                        or self.native.control is None or self.heartbeat is None
                        or not self.heartbeat.is_alive()):
                    return 'unavailable'
            # An uncertain ARM owns its worker until this native session ends.
            # Keep other live workers running, then reopen the original ledgers
            # through the service supervisor before accepting a later day's work.
            if any(ctx.get('finished') and not ctx.get('released') for ctx in self.contexts.values()):
                return 'restart_when_idle'
            return 'ready'

    def _start_native(self):
        if self.native:
            if self.health() == 'unavailable':
                raise RuntimeError('node_executor_restart_required')
            return
        root = self.directory/'native'; old = self.journal.native_identity
        if root.exists() != bool(old): raise ValueError('native ledger missing or unanchored')
        launcher = (LinuxVerifiedLauncher(self.source_root, self.build, self.time_permit)
                    if self.mode == 'production' and sys.platform.startswith('linux') else None)
        native = NativeProcess(root, self.build.executable, endpoint=self.endpoint, count=20,
            initialize=not bool(old), verified_binary_sha256=self.build.binary_sha256, launcher=launcher)
        try:
            validate_native_policy(native.config())
            if old and old != native.identity: raise ValueError('native identity changed')
            self.journal.bind_native(native.identity)
            self.outbox = DurableOutbox(root/'outbox', native, initialize=not bool(old))
            self.outbox.recover(self._project, lambda: [])
            self.native = native
        except Exception:
            if self.outbox: self.outbox.close(); self.outbox = None
            native.close(); raise
        self.heartbeat = threading.Thread(target=self._renew, name='dual-node-lease', daemon=True)
        self.heartbeat.start()

    def prepare(self, task):
        task_id, binding_hash = task_identity(task)
        self.time_permit.require_valid(task)
        local = self.credentials.binding(task)
        with self.lock:
            if self.closed or self.stopping.is_set(): raise ValueError('node stopped')
            if self.journal.status(task_id): raise ValueError('task grant already consumed')
            previous = self.contexts.get(task_id)
            if previous and previous.get('released'):
                del self.contexts[task_id]; previous = None
            if previous:
                if previous['binding_hash'] != binding_hash: raise ValueError('task changed after preparation')
                if previous['local'] != local: raise ValueError('prepared school identity changed')
                if not previous.get('ready') or previous['cancel'].is_set():
                    raise ValueError('task preparation busy or failed')
                return self._readiness(previous)
            if any(not old.get('released') and (old['task']['account_ref'] == task['account_ref']
                    or old['local'].student_id.casefold() == local.student_id.casefold()) for old in self.contexts.values()):
                raise ValueError('school identity already owns a worker')
            self._start_native()
            occupied = {ctx['worker'] for ctx in self.contexts.values() if not ctx.get('released')}
            idle = [index for index in range(20) if index not in occupied]
            if not idle: raise ValueError('node worker capacity exhausted')
            ctx = {'task':json.loads(json.dumps(task)), 'task_id':task_id, 'binding_hash':binding_hash,
                   'run':_run(task), 'worker':idle[0], 'cancel':threading.Event(), 'lock':threading.RLock(),
                   'grant':None, 'ready':False, 'finished':False, 'released':False, 'client':None,
                   'local':local, 'lease_until':0, 'executing':False, 'arm_attempted':False,
                   'drained':False, 'disable_acked':False}
            ctx['lock'].acquire()
            self.contexts[task_id] = ctx
        try:
            preparation_started = clock_ns()
            client = self.credentials.open_client(task, client_factory=self.client_factory)
            ctx['client'] = client
            if ctx['cancel'].is_set(): raise ValueError('preparation canceled')
            identity = client.login()
            if identity.student_id != local.student_id: raise ValueError('authenticated identity mismatch')
            if self.credentials.binding(task) != local: raise ValueError('credential changed during login')
            if ctx['cancel'].is_set(): raise ValueError('preparation canceled')
            self.native.workers[ctx['worker']].warm(1500)
            if ctx['cancel'].is_set(): raise ValueError('preparation canceled')
            ctx.update(local=local, ready=True, authenticated_ns=clock_ns())
            ctx['result_diagnostics'] = {'stage': 'prepare', 'reason': 'not_sent', 'attempts': [],
                'prepare_ms': round((clock_ns()-preparation_started)/1e6, 3)}
            return self._readiness(ctx)
        except Exception:
            self._release_ctx(ctx)
            raise ValueError('node preparation failed') from None
        finally:
            ctx['lock'].release()

    def _release_ctx(self, ctx):
        # Caller holds the context lock: authentication/POST/result collection
        # has returned before its client or worker can be retired.
        if ctx['client']:
            ctx['client'].close(); ctx['client'] = None
        with self.lock:
            ctx.update(finished=True, ready=False, released=True)

    def _readiness(self, ctx):
        self.time_permit.require_valid(ctx['task'])
        if clock_ns()-ctx['authenticated_ns'] > 900_000_000_000:
            raise ValueError('prepared authentication expired')
        config = self.native.config(); now = self.wall_clock()
        return {'schema':1, 'task_id':ctx['task_id'], 'binding_sha256':ctx['binding_hash'],
            'node_id':self.node_id, 'credential_revision':ctx['task']['credential_revision'],
            'checked_at':now, 'expires_at':min(now+120, self.time_permit.valid_until, ctx['task']['release_at']+30),
            'authenticated':True, 'identity_matches':True, 'payload_protocol':'campus-v1',
            'native_identity':self.native.identity, 'native_binary_sha256':self.build.binary_sha256,
            'platform':config['platform'], 'durability_profile':config['durability_profile'],
            'worker_count':20, 'clock_checked':True}

    def _allowed(self, ctx, *, require_lease=True):
        try:
            if self.closed or self.stopping.is_set() or ctx['finished'] or ctx['cancel'].is_set() or self.outbox.failed:
                return False
            self.time_permit.require_valid(ctx['task'], for_send=True)
            grant = ctx['grant']
            if grant is None or grant.authority_id != self.time_permit.authority_id: return False
            if self.credentials.binding(ctx['task']) != ctx['local']: return False
            if not 0 <= clock_ns()-ctx['authenticated_ns'] <= 900_000_000_000: return False
            if self.native.process is None or self.native.process.poll() is not None: return False
            if require_lease and clock_ns() >= ctx['lease_until']: return False
            now = self.wall_clock()
            return grant.issued_at <= now <= grant.expires_at
        except Exception: return False

    def _renew(self):
        while not self.stopping.wait(.1):
            try:
                with self.lock:
                    active = [ctx for ctx in self.contexts.values() if ctx['grant'] is not None and not ctx['finished']]
                if not active: continue
                for ctx in active:
                    if not self._allowed(ctx, require_lease=False): self.abort(ctx['task'])
                active = [ctx for ctx in active if not ctx['cancel'].is_set()]
                if active:
                    before = clock_ns(); self.native.lease(500)
                    for ctx in active: ctx['lease_until'] = before+500_000_000
            except Exception:
                self.stopping.set()
                if self.outbox: self.outbox.failed = True
                if self.native: self.native.close_control()

    def _enable(self, ctx):
        with self.outbox.lock:
            self.outbox.require_ready()
            if not self._allowed(ctx, require_lease=False): raise ValueError('execution permission changed')
            key = 'a'+hashlib.sha256(ctx['task']['account_ref'].encode()).hexdigest()
            ctx['account_key'] = key
            desired = {'enabled':True, 'plan':True, 'credential':ctx['task']['credential_revision']}
            self.desired[key] = desired
            account = self.outbox.publish(key, desired, {'binding_sha256':ctx['binding_hash']})
            master = self.outbox.applied('global') or {}
            if not master.get('enabled') or not master.get('plan'):
                self.outbox.set_control(live=True, clock_ok=True)
            return account, self.outbox.applied('global')

    def execute(self, task, signed_grant):
        task_id, binding_hash = task_identity(task)
        grant = self.verifier.verify_grant(signed_grant, node_id=self.node_id, task=task, now=self.wall_clock())
        self.time_permit.require_valid(task, for_send=True)
        if grant.authority_id != self.time_permit.authority_id: raise ValueError('grant and clock authority differ')
        with self.lock: ctx = self.contexts.get(task_id)
        if not ctx: raise ValueError('task not prepared')
        with ctx['lock']:
            if (not ctx['ready'] or ctx['released'] or ctx['cancel'].is_set()
                    or ctx['binding_hash'] != binding_hash): raise ValueError('task not prepared')
            self.journal.consume(grant.grant_id, task_id, binding_hash, self.node_id)
            ctx['grant'] = grant; ctx['executing'] = True
            outcome = {'status':'uncertain', 'attempt_count':0, 'verified':False}
            try:
                from .fence import GrantClientPermit
                ctx['client'].ownership_guard = GrantClientPermit(grant, task=ctx['task'], run=ctx['run'],
                    node_id=self.node_id, mode=self.mode, authority_id=self.time_permit.authority_id,
                    ready=lambda:self._allowed(ctx), wall_clock=self.wall_clock)
                outcome = self._execute(ctx)
            except Exception as exc:
                self.abort(task)
                ctx.setdefault('result_diagnostics', {'stage':'submit','attempts':[]})['reason'] = 'execution_interrupted'
                outcome = {'status':'uncertain', 'attempt_count':ctx.get('attempt_count',0),
                           'verified':False, 'reason':type(exc).__name__}
            finally:
                self.abort(task)
                ctx['finished'] = True; ctx['executing'] = False
                try:
                    outcome['diagnostics'] = result_diagnostics(ctx['result_diagnostics'], task)
                except (ValueError, TypeError, KeyError):
                    pass
                try: self.journal.record(task_id, outcome['status'], {key:value for key,value in outcome.items() if key!='status'})
                except Exception:
                    outcome = {'status':'uncertain', 'attempt_count':ctx.get('attempt_count',0),
                               'verified':False, 'reason':'result_persistence_failed'}
                    self.stopping.set()
                    self.native.close_control()
                # A missing ARM acknowledgement leaves the worker reserved.
                # Only a complete result stream or a proven pre-ARM exit may
                # release it after the durable disable acknowledgement.
                if ctx['disable_acked'] and (not ctx['arm_attempted'] or ctx['drained']):
                    self._release_ctx(ctx)
                elif ctx['client']:
                    ctx['client'].close(); ctx['client'] = None
            return {'task_id':task_id, **outcome}

    def _wait_until(self, ctx, wall_deadline):
        while self.wall_clock() < wall_deadline:
            if not self._allowed(ctx, require_lease=False): raise ValueError('execution permission changed')
            ctx['cancel'].wait(min(.05, max(0, wall_deadline-self.wall_clock())))

    def _execute(self, ctx):
        task, run, client = ctx['task'], ctx['run'], ctx['client']
        ctx['result_diagnostics']['stage'] = 'submit'
        self._wait_until(ctx, task['release_at']-1.5)
        account, master = self._enable(ctx)
        if not self._allowed(ctx, require_lease=False): raise ValueError('execution permission changed')
        budget = min(1000, max(50, int((task['release_at']-self.wall_clock()-.1)*1000)))
        worker = self.native.workers[ctx['worker']]
        worker.warm(budget)
        before = prepared_session(client); client._native_prepared_session = None
        client.prepare_scheduled_submission(run, run.seat_ids[0],
            clock_ns()/1e9 + max(0, task['release_at']-self.wall_clock()-.02))
        remember_preparation(client, before)
        cap = capture_request(client, run, run.seat_ids[0])
        pair = sample_clock_pair(self.wall_clock, clock_ns, deadline_wall=task['release_at'])
        pair.require_consistent(self.time_permit.anchor.wall, self.time_permit.anchor.midpoint_ns)
        if pair.width_ns > 1_000_000 or not .05 <= task['release_at']-pair.wall <= 1.8:
            raise ValueError('native preparation window missed')
        due = pair.deadline_ns(task['release_at'])
        job_id = 'r'+ctx['task_id']
        if self.native.job_status(job_id) != 'NOTFOUND': raise ValueError('native task already exists')
        job = {'id':job_id, 'worker':ctx['worker'], 'account':ctx['account_key'], 'day':task['day'],
            'kind':'S', 'operation':'-', 'account_version':account['version'], 'global_version':master['version'],
            'credential':task['credential_revision'], 'due_ns':due, 'created_ns':cap.created_ns,
            'expires_ns':min(cap.expires_ns, due+1_000_000_000), 'body':cap.encode(),
            'pilot_wall_due_ns':wall_deadline_ns(task['release_at'])}
        self.journal.record(ctx['task_id'], 'armed', {'job_id':job_id,
            'native_epoch':self.native.epoch, 'native_session':self.native.session})
        if (not self._allowed(ctx, require_lease=False) or clock_ns()+20_000_000 > due
                or job['expires_ns'] <= due): raise ValueError('native deadline not safe')
        before = clock_ns(); self.native.lease(500); ctx['lease_until'] = before+500_000_000
        ctx['arm_attempted'] = True
        self.native.arm([job], 'D'); worker.arm_wait(job)
        result = worker.result()
        ctx['drained'] = True
        fields = self.native.job_status(job_id).split()
        same = fields[:4] == ['JOB', job_id, str(self.native.epoch), str(self.native.session)]
        proven = same and len(fields) == 6 and fields[4] == '1' and fields[5] in {'done', 'uncertain', 'denied'}
        denied = same and len(fields) == 6 and fields[5] == 'denied' and result['dispatch_ns'] == 0
        if denied and result['journal_ok']:
            ctx['result_diagnostics']['reason'] = 'not_sent'
            return {'status':'not_sent', 'attempt_count':0, 'verified':False}
        start = result.get('metrics', {}).get('curl_start_ns') or result['dispatch_ns']
        timings = {'request_started_at':pair.wall_at(start) if start else self.wall_clock(),
            'request_ms':max(0, result['network_done_ns']-start)/1e6 if start else 0}
        first = decode_result(result['http_status'], result['body'].encode(), timings,
            sent=True if result['dispatch_ns'] else None,
            journal_ok=bool(proven and result['journal_ok']), transport_ok=result['curl_code'] == 0)
        ctx['attempt_count'] = 1 if result['dispatch_ns'] else 0
        return self._continue(ctx, first)

    def _read_records(self, ctx, seats):
        ctx['result_diagnostics']['stage'] = 'readback'
        day = date.fromisoformat(ctx['task']['day'])
        records = ctx['client'].query(day, day)
        if not isinstance(records, list): raise ValueError('incomplete school readback')
        for seat in dict.fromkeys(seats):
            for record in records:
                if not records_match(ctx['run'], seat, [record], ctx['client'].identity,
                        active_statuses=ctx['client'].active_statuses): continue
                if not isinstance(record.id, str) or not 0 < len(record.id) <= 256:
                    raise ValueError('matched school receipt identity invalid')
                reference = canonical_hash({'task_id':ctx['task_id'], 'receipt_id':record.id,
                    'seat_id':record.seat_id, 'begin':record.begin, 'end':record.end, 'status':record.status})
                return {'receipt_ref':reference, 'seat_index':ctx['run'].seat_ids.index(record.seat_id)}, bool(records)
        return None, bool(records)

    def _continue(self, ctx, response):
        task, run, client = ctx['task'], ctx['run'], ctx['client']
        attempts = []
        seat = run.seat_ids[0]
        while True:
            attempts.append({'seat_id':seat, 'code':response.code, 'message':response.message})
            count = len(attempts); ctx['attempt_count'] = count
            diagnostic = ctx.setdefault('result_diagnostics', {'stage':'submit','reason':'not_sent','attempts':[]})
            reason = ('not_sent' if response.sent is False else
                'accepted' if response.outcome == 'accepted' else
                'transport_uncertain' if response.outcome == 'uncertain' else
                'busy' if (response.code,response.message) == BUSY_REJECTION else
                'occupied' if (response.code,response.message) == OCCUPIED_REJECTION else
                'rate_limited' if (response.code,response.message) == (1,'请求频繁,请稍后重试') else 'school_rejected')
            observed = {'seat_index':run.seat_ids.index(seat), 'outcome':response.outcome,
                        'reason':reason, 'sent':response.sent}
            for name in ('request_ms', 'request_started_at'):
                value = (response.timings or {}).get(name)
                limit = 4102444800 if name == 'request_started_at' else 900000
                if type(value) in (int,float) and 0 <= value <= limit and math.isfinite(value):
                    observed[name] = value
            diagnostic['attempts'].append(observed)
            diagnostic.update(stage='submit', reason=reason)
            if response.outcome == 'uncertain':
                return {'status':'uncertain', 'attempt_count':count, 'verified':False}
            if response.outcome == 'accepted':
                matched, _ = self._read_records(ctx, [a['seat_id'] for a in attempts])
                if matched:
                    diagnostic.update(stage='complete', reason='receipt_matched')
                    return {'status':'completed', 'attempt_count':count, 'verified':True, **matched}
                diagnostic['reason'] = 'receipt_unconfirmed'
                return {'status':'uncertain', 'attempt_count':count, 'verified':False}
            candidates = remaining_candidate_attempts(run.seat_ids, attempts) if response.retryable else ()
            if not candidates: return {'status':'failed', 'attempt_count':count, 'verified':False}
            duration = max(0, (response.timings or {}).get('request_ms', 0))/1000
            wait_for = max(BUSY_RETRY_RESPONSE_COOLDOWN, busy_retry_spacing(count)-duration)
            self._wait_until(ctx, self.wall_clock()+wait_for)
            matched, records = self._read_records(ctx, [a['seat_id'] for a in attempts])
            if matched:
                diagnostic.update(stage='complete', reason='receipt_matched')
                return {'status':'completed', 'attempt_count':count, 'verified':True, **matched}
            if records:
                diagnostic['reason'] = 'receipt_unconfirmed'
                return {'status':'uncertain', 'attempt_count':count, 'verified':False}
            if not self._allowed(ctx) or not task['release_at'] <= self.wall_clock() <= ctx['grant'].expires_at:
                diagnostic['reason'] = 'permission_changed'
                return {'status':'failed', 'attempt_count':count, 'verified':False}
            seat = candidates[0]
            # Persist the next attempt before any Python POST. Restart never resumes it.
            self.journal.record(ctx['task_id'], 'retrying', {'attempt_count':count+1,
                'seat_index':run.seat_ids.index(seat)})
            client.allow_live = True
            client.submission_guard = lambda:self._allowed(ctx) and task['release_at'] <= self.wall_clock() <= ctx['grant'].expires_at
            response = client.submit(run, seat)

    def abort(self, task=None):
        with self.lock:
            if task is None:
                targets = list(self.contexts.values()); self.stopping.set()
            else:
                task_id, _ = task_identity(task)
                targets = [self.contexts[task_id]] if task_id in self.contexts else []
            for ctx in targets: ctx['cancel'].set()
        if not self.outbox: return
        try:
            with self.outbox.lock:
                for ctx in targets:
                    key = ctx.get('account_key')
                    if key and (self.outbox.projection(key) or {}).get('binding_sha256') == ctx['binding_hash']:
                        desired = {'enabled':False, 'plan':False, 'credential':ctx['task']['credential_revision']}
                        self.desired[key] = desired
                        self.outbox.publish(key, desired, {'binding_sha256':ctx['binding_hash']})
                        ctx['disable_acked'] = True
                    elif not key:
                        ctx['disable_acked'] = True
                if task is None: self.outbox.set_control(live=False, clock_ok=False)
        except Exception:
            self.outbox.failed = True
            if self.native: self.native.close_control()
        for ctx in targets:
            if ctx['lock'].acquire(blocking=False):
                try:
                    if ctx['grant'] is None and ctx['disable_acked']:
                        self._release_ctx(ctx)
                finally:
                    ctx['lock'].release()

    def status(self, task):
        task_id, binding_hash = task_identity(task)
        row = self.journal.status(task_id)
        with self.lock: ctx = self.contexts.get(task_id)
        execution = None
        if row is not None:
            fields = dict(row['fields']); status = row['status']
            if row['binding_sha256'] != binding_hash:
                status = 'uncertain'; fields = {'verified':False, 'attempt_count':0}
            else:
                proof = (fields.get('verified') is True
                    and isinstance(fields.get('receipt_ref'), str)
                    and re.fullmatch('[0-9a-f]{64}', fields['receipt_ref'])
                    and type(fields.get('seat_index')) is int
                    and 0 <= fields['seat_index'] < len(task['seat_priorities'])
                    and type(fields.get('attempt_count')) is int
                    and 1 <= fields['attempt_count'] <= 6)
                if status != 'completed' or not proof:
                    if status == 'completed': status = 'uncertain'
                    fields.pop('receipt_ref', None); fields.pop('seat_index', None)
                    fields['verified'] = False
            execution = {'status':status, **fields}
        return {'task_id':task_id, 'prepared':bool(ctx and ctx['ready'] and not ctx['finished']),
            'execution':execution}

    def close(self):
        if self.closed: return
        self.abort(); self.closed = True
        if self.heartbeat: self.heartbeat.join(timeout=3)
        if self.native: self.native.close()
        for ctx in self.contexts.values():
            with ctx['lock']:
                if ctx.get('client'): ctx['client'].close()
        if self.outbox: self.outbox.close()
        self.journal.close()
