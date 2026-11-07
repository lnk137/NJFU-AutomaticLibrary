"""Opt-in node loop: coalesced public probing, winner-only preparation and execution."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import re
from contextlib import nullcontext,contextmanager
import time
import uuid

from local_app.models import ZONE
from local_app.native.protocol import clock_ns
from local_app.native.trusted_clock import boot_identifier
from . import public_probe
from .native_launcher import ClockPermit
from .diagnostics import NodeDiagnostics, error_code


TASK_REJECTIONS = frozenset((
    "node_not_ready", "invalid_report", "stale_report", "release_window",
    "captcha_required", "release_schedule_mismatch", "report_not_accepted",
    "unknown_task", "binding_conflict", "credential_enrollment_mismatch",
    "account_inactive", "account_not_assigned", "account_not_enrolled"))


class NodeSafetyError(ValueError):
    """Shared safety evidence cannot be downgraded into an account failure."""


class RPCError(RuntimeError):
    pass


class RPCClient:
    def __init__(self, transport_factory):
        self.factory = transport_factory
        self.transport = None
        self.lock = threading.RLock()

    def call(self, op, *, deadline=None, **body):
        wait = 5 if deadline is None else min(5,max(0,deadline-time.time()))
        if not self.lock.acquire(timeout=wait):raise RPCError('rpc_queue_deadline')
        try:
            if deadline is not None and time.time() >= deadline:
                raise RPCError('rpc_queue_deadline')
            if self.transport is None:
                self.transport = self.factory()
            request_id = uuid.uuid4().hex
            if op == 'heartbeat' and 'seq' in body:
                body['seq'] = clock_ns()
            if op in ('report','ready') and 'readiness' in body:
                body['readiness'] = dict(body['readiness'], seq=clock_ns())
            request = {'schema':1, 'op':op, 'request_id':request_id, 'body':body}
            previous_timeout=self.transport.timeout
            if deadline is not None:
                self.transport.timeout=min(previous_timeout,max(.001,deadline-time.time()))
            try:
                response = self.transport.call(request)
            except Exception:
                self.transport.close(); self.transport = None
                raise RPCError('rpc_result_unknown') from None
            self.transport.timeout=previous_timeout
            if (response.get('schema') != 1 or response.get('request_id') != request_id
                    or type(response.get('ok')) is not bool):
                self.transport.close(); self.transport = None
                raise RPCError('rpc_response_invalid')
            if not response['ok']:
                raise RPCError(response.get('code', 'rpc_rejected'))
            return response['result']
        finally:
            self.lock.release()

    @contextmanager
    def serialized(self, timeout=5):
        if not self.lock.acquire(timeout=timeout):raise RPCError('rpc_queue_deadline')
        try:yield
        finally:self.lock.release()

    def close(self):
        with self.lock:
            if self.transport is not None:
                self.transport.close(); self.transport = None


def all_tasks(rpc):
    rows, after, seen, quiet = [], None, set(), set()
    for _ in range(64):
        body = {'limit':32}
        if after is not None: body['after'] = after
        page = rpc.call('tasks', **body)
        rows.extend(page['tasks'])
        for value in page.get('quiet_release_times', []):
            public_probe.release_time(value);quiet.add(value)
        after = page.get('next_cursor')
        if after is None:
            rpc.quiet_release_times = tuple(sorted(quiet))
            return rows
        if after in seen: raise RPCError('rpc_pagination_cycle')
        seen.add(after)
    raise RPCError('rpc_task_limit')


class NodeAgent:
    """One node, one native process, at most 20 simultaneous task contexts.

    The injected executor verifies signed grants again and durably burns them.
    A worker exception is reported as uncertainty; it never starts a second send.
    """
    def __init__(self, rpc, node_id, executor, verifier, capability, *,
                 sample=public_probe.sample_node, clock=time.time, sleeper=time.sleep,
                 boot=None, continuous_ns=clock_ns, power_guard=None, diagnostics=None):
        if node_id not in ('mac', 'ubuntu'):
            raise ValueError('invalid_node')
        self.rpc, self.node_id, self.executor, self.verifier = rpc, node_id, executor, verifier
        self.diagnostics = diagnostics if diagnostics is not None else NodeDiagnostics(node_id=node_id, clock=clock)
        self.probe_attempts, self.probe_retry_at = {}, {}
        self._task_stage = "status"
        self.capability = capability
        self.power_guard = power_guard
        self.sample, self.clock, self.sleep = sample, clock, sleeper
        self.continuous_ns = continuous_ns
        self.boot = boot or boot_identifier()
        self.pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix='dual-task')
        self.probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dual-probe')
        self.probes, self.reported, self.tasks = {}, {}, {}
        self.outcomes = {}
        self.last_heartbeat = float('-inf')
        self.cached_tasks, self.tasks_refreshed = [], float('-inf')
        self._closed = False
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.time_lock = threading.RLock()
        self.cached_time, self.time_refreshed_ns = None, 0

    def time_permit(self):
        if not hasattr(self, 'time_lock'):
            self.time_lock = threading.RLock()
            self.cached_time, self.time_refreshed_ns = None, 0
        with self.time_lock:
            if (self.cached_time is not None
                    and 0 <= self.continuous_ns()-self.time_refreshed_ns < 10_000_000_000):
                self.cached_time.require_valid()
                return self.cached_time
            # Establish SSH before timing a frame. Connection setup is not a
            # sample of the persistent RPC channel used for clock evidence.
            self.rpc.call('time', nonce=uuid.uuid4().hex)
            samples = []
            # One bounded acquisition covers the batch; status polling cannot
            # insert queue delays between the three fresh clock samples.
            channel=self.rpc.serialized() if hasattr(self.rpc,'serialized') else nullcontext()
            with channel:
                for _ in range(3):
                    nonce = uuid.uuid4().hex
                    before = self.continuous_ns()
                    signed = self.rpc.call('time', nonce=nonce)
                    after = self.continuous_ns()
                    samples.append({'signed_time':signed.get('signed_time', signed),
                                    'before_ns':before, 'after_ns':after, 'nonce':nonce})
            permit = ClockPermit(self.verifier, samples, node_id=self.node_id)
            self.cached_time, self.time_refreshed_ns = permit, self.continuous_ns()
            return permit

    def _seq(self):
        return self.continuous_ns()

    def step(self):
        if self.stopping.is_set():
            return
        self._check_executor_health()
        now = self.clock()
        if self.power_guard is not None:
            self.power_guard.update(self._power_tasks(now), now)
        if now-self.last_heartbeat >= 10:
            self.rpc.call('heartbeat', boot_id=self.boot, seq=self._seq())
            self.last_heartbeat = now
        self._flush_outcomes()
        if now-self.tasks_refreshed >= 10:
            self.cached_tasks = all_tasks(self.rpc)
            self.tasks_refreshed = now
        rows = self.cached_tasks
        if self.power_guard is not None:
            self.power_guard.update(self._power_tasks(now), now)
        releases = tuple(sorted({'07:00:00', *getattr(self.rpc, 'quiet_release_times', ()), *(datetime.fromtimestamp(t['release_at'], ZONE).strftime('%H:%M:%S') for t in rows)}))
        for task in rows:
            if self.stopping.is_set():break
            try:
                self._step_task(task, now, releases)
            except RPCError as error:
                self.diagnostics.record(self._task_stage, error_code(error), task=task)
                if error_code(error) not in TASK_REJECTIONS:
                    raise
            except (ValueError, OSError) as error:
                self.diagnostics.record(self._task_stage, error_code(error), task=task)
                # Unknown data, clock, identity and storage failures remain
                # node-wide failures; only explicit task rejections are isolated.
                raise
        self._prune(now)

    def _step_task(self, task, now, releases):
        from .contracts import task_identity
        task_id, binding = task_identity(task)
        key = (task_id, binding)
        due = task['release_at']
        if due-now > 450:
            return
        self._task_stage = 'status'
        state = self.rpc.call('status', task=task)
        phase, owner = state['phase'], state['owner']
        if state.get('operator_paused') and state.get('grant') is None:
            return
        if state.get('cancel_requested') or phase == 'cancel_requested' or state.get('operator_paused'):
            if owner == self.node_id:
                self.executor.abort(task)
                if state.get('grant') is not None and (key not in self.tasks or self.tasks[key].done()):
                    local = self.executor.status(task).get('execution')
                    self._record_outcome(task, key, state['grant'],
                                         {'status':'uncertain'} if local is None else dict(local))
            return
        if phase == 'collecting':
            # A single six-sample probe is shared across all accounts with
            # the same release, so 20 accounts never cause 20 probe bursts.
            attempts = self.probe_attempts.get(due, 0)
            retry = due in self.probe_retry_at and now >= self.probe_retry_at[due]
            # At most one replacement public sample. The full six-sample
            # budget must fit before the five-second quiet-window guard.
            start = due-425 <= now <= due-360 and due not in self.probes
            restart = retry and attempts < 2 and now <= due-365
            if start or restart:
                self._start_probe(due, releases)
            future = self.probes.get(due)
            if (future is not None and future.done()
                    and now-self.reported.get(key, float('-inf')) >= 20):
                self._task_stage = "probe"
                try:
                    report = future.result()
                except (ValueError, OSError):
                    self._retry_probe(due, now)
                    raise
                self.diagnostics.record("probe", "probe_complete", task=task, report=report)
                self._task_stage = "capability"
                readiness = dict(self.capability(task))
                readiness.update(boot_id=self.boot, seq=self._seq())
                self._task_stage = 'report'
                try:
                    self.rpc.call('report', task=task, report=report, readiness=readiness)
                except RPCError as error:
                    if error_code(error) in ('invalid_report','stale_report'):
                        self._retry_probe(due, now)
                    raise
                self.diagnostics.record('report', 'ok', task=task)
                self.reported[key] = now
        elif phase in ('selected','ready') and owner == self.node_id:
            if 3 < due-now <= 90 and key not in self.tasks:
                self.tasks[key] = self.pool.submit(self._run, task, key)
        elif (phase == 'authorized' and owner == self.node_id
              and (key not in self.tasks or self.tasks[key].done())):
            # A new agent process has no in-memory worker. Its exclusive
            # local journal supplies only a durable result, never permission
            # to repeat an ARM with an already issued server grant.
            local = self.executor.status(task).get('execution')
            outcome = {'status':'uncertain'} if local is None else dict(local)
            self._record_outcome(task, key, state['grant'], outcome)
        if key in self.tasks and self.tasks[key].done():
            self._task_stage = 'worker'
            self.tasks[key].result()

    def _start_probe(self, due, releases):
        options = {}
        if self.sample is public_probe.sample_node:
            options['release_provider'] = self._current_releases
        self.probe_attempts[due] = self.probe_attempts.get(due, 0)+1
        self.probe_retry_at.pop(due, None)
        self.probes[due] = self.probe_pool.submit(self.sample, self.node_id, 6, 10, releases, **options)
        self.diagnostics.record('probe', 'probe_started')

    def _retry_probe(self, due, now):
        if self.probe_attempts.get(due, 1) < 2 and now+2 <= due-365:
            self.probe_retry_at.setdefault(due, now+2)
        else:
            self.diagnostics.record('probe', 'probe_budget_exhausted')

    def _power_tasks(self, now):
        releases = sorted({t['release_at'] for t in self.cached_tasks
                           if now-305 <= t['release_at'] <= now+48*3600})
        return [{'release_at':value} for value in releases]

    def _current_releases(self):
        rows = all_tasks(self.rpc)
        return tuple(sorted({'07:00:00', *getattr(self.rpc, 'quiet_release_times', ()), *(datetime.fromtimestamp(t['release_at'], ZONE).strftime('%H:%M:%S') for t in rows)}))

    def _check_executor_health(self):
        check = getattr(self.executor, 'health', None)
        if not callable(check): return
        health = check()
        if health not in ('unavailable', 'restart_when_idle'): return
        if health == 'restart_when_idle' and any(not future.done() for future in self.tasks.values()):
            return
        # A broken executor must not advertise fresh presence indefinitely.
        # Results are already durable locally; give their delivery a bounded
        # opportunity, then let the existing service reopen the same journals.
        try:
            self._flush_outcomes(deadline=time.time()+2)
        except (RPCError, OSError, ValueError):
            pass
        raise RuntimeError('node_executor_restart_required')

    def _flush_outcomes(self, *, deadline=None):
        with self.lock:
            pending = list(self.outcomes.items())
        for key, (task, result) in pending:
            if deadline is not None and time.time() >= deadline: return
            options = {} if deadline is None else {'deadline':deadline}
            self.rpc.call('result', task=task, **result, **options)
            with self.lock:
                if self.outcomes.get(key) == (task, result):
                    self.outcomes.pop(key, None)

    def _require_running(self):
        if self.stopping.is_set():
            raise RuntimeError('node_stopping')

    def _prune(self, now):
        # Probe observations cannot be reused on another release/day.
        for due in list(self.probes):
            if due < now-300 and self.probes[due].done():
                del self.probes[due]
                self.probe_attempts.pop(due, None)
                self.probe_retry_at.pop(due, None)

    def _run(self, task, key):
        grant = None
        attempted_authorize = False
        stage = 'clock'
        try:
            self._require_running()
            stage = 'clock'
            self.executor.set_time_permit(self.time_permit())
            self._require_running()
            stage = 'prepare'
            readiness = self.executor.prepare(task)
            self._require_running()
            readiness.update(boot_id=self.boot, seq=self._seq())
            stage = 'ready'
            self.rpc.call('ready', task=task, readiness=readiness, deadline=task['release_at']-3)
            while self.clock() < task['release_at']-20:
                if self.stopping.wait(min(1, max(.001, task['release_at']-20-self.clock()))):
                    self.executor.abort(task); return
                state = self.rpc.call('status', task=task)
                if state['owner'] != self.node_id or state['phase'] not in ('selected','ready'):
                    self.executor.abort(task); return
            self._require_running()
            stage = 'clock'
            self.executor.set_time_permit(self.time_permit())
            self._require_running()
            stage = 'prepare'
            readiness = self.executor.prepare(task)
            self._require_running()
            readiness.update(boot_id=self.boot, seq=self._seq())
            stage = 'ready'
            self.rpc.call('ready', task=task, readiness=readiness, deadline=task['release_at']-3)
            self._require_running()
            stage = 'authorize'
            attempted_authorize = True
            grant = self.rpc.call('authorize', task=task, deadline=task['release_at']-3)['grant']
            self._require_running()
            stage = 'execute'
            outcome = self.executor.execute(task, grant)
            self._record_outcome(task, key, grant, outcome)
        except Exception as error:
            self.diagnostics.record(stage, error_code(error), task=task)
            # Fixed errors only; request capsules, auth pages and credentials
            # must never appear in logs or server result bodies.
            if grant is not None:
                self._record_outcome(task, key, grant, {'status':'uncertain'})
            else:
                self.executor.abort(task)
                if not attempted_authorize:
                    try:
                        self.rpc.call('prepare_failed', task=task, code='preparation_failed')
                    except RPCError:
                        pass

    def _record_outcome(self, task, key, grant, outcome):
        verified = self.verifier.verify_grant(grant, node_id=self.node_id, task=task)
        payload = dict(verified)
        status = outcome.get('status', 'uncertain')
        if status not in ('completed','failed','not_sent','uncertain'):
            status = 'uncertain'
        attempts = outcome.get('attempt_count',0)
        if type(attempts) is not int or not 0 <= attempts <= 6:attempts = 0
        result = {'grant_id':payload['grant_id'], 'status':status,
                  'verified':False,'attempt_count':attempts}
        if outcome.get('diagnostics') is not None:
            from .contracts import result_diagnostics
            try:
                diagnostic = result_diagnostics(outcome['diagnostics'], task)
                if len(diagnostic['attempts']) <= attempts: result['diagnostics'] = diagnostic
            except (ValueError, TypeError, KeyError):
                pass  # Observation cannot weaken or prevent the durable result.
        if status == 'completed':
            receipt, seat = outcome.get('receipt_ref'), outcome.get('seat_index')
            if (outcome.get('verified') is True and type(receipt) is str
                    and re.fullmatch(r'[0-9a-f]{64}',receipt) and type(seat) is int
                    and 0 <= seat < len(task['seat_priorities']) and attempts >= 1):
                result.update(verified=True,receipt_ref=receipt,seat_index=seat)
            else:
                result['status'] = 'uncertain'
        with self.lock:
            self.outcomes[key] = (task, result)
        try:
            self.rpc.call('result', task=task, **result)
        except RPCError as error:
            self.diagnostics.record('result', error_code(error), task=task)
            return
        with self.lock:
            self.outcomes.pop(key, None)

    def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    self.step()
                except (RPCError, OSError, ValueError) as error:
                    self.diagnostics.record('loop', error_code(error))
                    # Transport restores only its connection. Server and local
                    # journals decide ownership; reconnect never re-arms a task.
                    pass
                self.stopping.wait(2)
        finally:
            self.close()

    def request_stop(self):
        self.stopping.set()
        self.executor.abort()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.request_stop()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.probe_pool.shutdown(wait=True, cancel_futures=True)
        try:
            self.executor.close()
        finally:
            try:
                if self.power_guard is not None:self.power_guard.close()
            finally:
                self.rpc.close()
