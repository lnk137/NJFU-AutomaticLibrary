"""Separate plan polling and deadline thread; bounded preparation and submission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import logging
import threading
import time
from pymongo.errors import DuplicateKeyError
from .models import AppError, RunResult, RunSpec, ZONE, resolve_runs, validate_plan
from .recovery_policy import clear_busy_failure
from .date_exceptions import next_scheduled_run, validate_date_exceptions
from .elapsed_clock import elapsed_time, CLOCK_SOURCE
from .limits import PARALLEL_ACCOUNT_LIMIT
from .timing_diagnostics import finish_attempt,record_error,safe_timings

WARMUP_LEAD_SECONDS = 4.5
WARMUP_REFRESH_LEAD_SECONDS = 1.5
PREPARE_RETRY_DELAYS = (1, 3, 10, 20, 30, 30, 30, 30, 30, 30)
PREPARE_RETRY_MIN_GAP_SECONDS = 1
PREPARE_LAST_WARM_LEAD_SECONDS = 5
LOOP_STALE_SECONDS = 5
LOGGER = logging.getLogger(__name__)


def prepare_retry_at(run, attempt, now, *, deadline):
    """Bound preflight retries and reserve an opportunity near release."""
    if type(attempt) is not int or not 1 <= attempt <= len(PREPARE_RETRY_DELAYS):
        return None
    target = now + PREPARE_RETRY_DELAYS[attempt-1]
    for checkpoint in (run.due_at-PREPARE_LAST_WARM_LEAD_SECONDS, run.due_at-.25):
        if now+PREPARE_RETRY_MIN_GAP_SECONDS <= checkpoint < target:
            target = checkpoint
    return target if target <= deadline else None


def cohort_handled(store, run, *, segmented_history_only=False):
    """A use-date cohort retains any prior claim, native ownership or terminal evidence."""
    if store.db.campus_scheduled_cohorts.find_one({'_id': run.account_id + ':' + run.target_date}):
        return True
    query = {'account_id': run.account_id,
        'target_date': run.target_date, 'spec.kind': {'$ne': 'manual'}, '$or': [
            {'native_binding': {'$exists': True}},
            {'attempted_seat_id': {'$ne': None}}, {'submitted_at': {'$exists': True}},
            {'status': {'$in': ['submitting', 'uncertain', 'success', 'failed']}},
            {'status': 'skipped', 'result.scheduled_cohort_handled': True}]}
    if segmented_history_only:
        query['spec.operation_id'] = {'$ne': None}
    return bool(store.runs.find_one(query))


class LocalScheduler:
    def __init__(self, accounts, reservations, store, *, wall_clock=time.time, monotonic=None):
        self.accounts, self.reservations, self.store = accounts, reservations, store
        self.wall_clock, self.monotonic = wall_clock, monotonic or elapsed_time
        self.anchor_wall, self.anchor_mono = wall_clock(), self.monotonic()
        self.clock_ok, self.last_error = True, None
        self.stopping = threading.Event()
        self.prep_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='njfu-prepare')
        self.warm_pool = ThreadPoolExecutor(max_workers=PARALLEL_ACCOUNT_LIMIT, thread_name_prefix='njfu-warm')
        self.submit_pool = ThreadPoolExecutor(max_workers=PARALLEL_ACCOUNT_LIMIT, thread_name_prefix='njfu-submit')
        self.diagnostic_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='njfu-diagnostics')
        self._lock, self.jobs, self.rechecks = threading.RLock(), {}, {}
        self._loop_errors, self._account_errors = set(), set()
        self._last_success = {}
        self.threads, self.next_due, self.closed = [], None, False
        self.reservations.guard = lambda: self.clock_ok and not self.stopping.is_set()

    def _valid(self, run):
        from .dual_node.fence import legacy_allowed
        if not legacy_allowed(self.accounts,run):return False
        row = self.store.get_account(run.account_id)
        return (row['revision'] == run.revision and (row.get('plan') or {}).get('enabled')
                and self.accounts.is_allowed(row['student_id']))

    def _queue_stage(self, job, stage, pool, action):
        """Measure waits in memory; persist diagnostics after the submit path."""
        phases = job.setdefault('timings', {})
        phase = phases.setdefault(stage, {})
        for field in ('error_code','error_kind','error_stage','active_step','retryable'):
            phase.pop(field, None)
        for field in ('started_at','finished_at','queue_ms','elapsed_ms','account_lock_wait_ms',
                      'school_login_ms','booking_preflight_ms','session_ms','payload_prepare_ms',
                      'transport_ms','payload_remaining_ms','next_retry_at'):
            phase.pop(field, None)
        if stage in ('prepare', 'warm'):
            phase['attempt_count'] = phase.get('attempt_count', 0) + 1
        if stage == 'warm':phase.pop('refresh_needed', None)
        queued = self.monotonic()
        phase['queued_at'] = self.wall_clock()
        phase['outcome'] = 'queued'
        run = job['run']

        def measured():
            started = self.monotonic()
            phase.update(started_at=self.wall_clock(), queue_ms=round((started-queued)*1000, 3))
            phase['outcome'] = 'running'
            try:
                result = action(run, phase)
                phase['outcome'] = ('finished' if stage == 'submit' else
                                    'needs_refresh' if phase.get('refresh_needed') else
                                    'ready' if result else 'failed')
                return result
            except Exception as exc:
                record_error(phase, exc)
                phase['outcome'] = 'failed'
                raise
            finally:
                phase.update(finished_at=self.wall_clock(), elapsed_ms=round((self.monotonic()-started)*1000, 3))
                finish_attempt(phase)
                if stage == 'submit':
                    self._queue_timings(run,phases)
        return pool.submit(measured)

    def _queue_timings(self, run, phases):
        snapshot = safe_timings(phases)
        try:self.diagnostic_pool.submit(self._save_timings,run,snapshot)
        except RuntimeError:
            LOGGER.warning('Task phase timings were not saved: %s', run.key)

    def _record_stop(self, job, reason):
        """Keep the cutoff decision and the eventual result of its owned worker."""
        phases = job.setdefault('timings', {})
        now = self.wall_clock()
        waiting = 'warm' if job.get('warm') is not None else 'prepare'
        phases.setdefault('stop', {'reason':reason, 'stopped_at':now,
            'remaining_ms':round((job['run'].due_at-now)*1000,3), 'waiting_stage':waiting})
        self._queue_timings(job['run'],phases)
        for stage in ('prep','warm'):
            future = job.get(stage)
            if future is not None and not future.done():
                future.add_done_callback(lambda done: self._queue_timings(job['run'],phases))

    def _save_timings(self, run, phases):
        try:self.store.save_scheduler_timings(run, phases)
        except Exception:
            # Slow or failed diagnostics never hold a submission worker.
            LOGGER.warning('Task phase timings were not saved: %s', run.key)

    def _prepare(self, run, phase=None):
        phase = phase if phase is not None else {}
        waiting = self.monotonic()
        with self.accounts.account_lock(run.account_id):
            phase['account_lock_wait_ms'] = round((self.monotonic()-waiting)*1000, 3)
            try:
                if not self._valid(run) or not self.reservations.allow_live or self.stopping.is_set(): return False
                if self.wall_clock() > run.due_at + 30:
                    if run.operation_id:
                        self._skip_segments((run,), '已错过放号超过 30 秒，不补交')
                    else:
                        self.store.transition_run(run.key, ('queued','preparing'), 'skipped',
                            {'message':'已错过放号超过 30 秒，不补交'}, revision=run.revision)
                    return False
                if not self.store.transition_run(run.key,('queued',),'preparing',{},revision=run.revision): return False
                self.accounts.busy.add(run.account_id)
                started = self.monotonic()
                phase['active_step'] = 'session'
                # The first attempt refreshes authentication. A retry reuses
                # a valid session if login succeeded but the read-only
                # booking preflight failed; failed logins leave no session.
                attempt = phase.get('attempt_count', 1)
                force_login = type(attempt) is not int or attempt <= 1
                try:client = self.accounts.client(run.account_id,force=force_login)
                finally:phase['school_login_ms'] = round((self.monotonic()-started)*1000, 3)
                prepare = getattr(client, 'prepare_booking', None)
                if prepare:
                    phase['active_step'] = 'prepare'
                    started = self.monotonic()
                    try:prepare()
                    finally:phase['booking_preflight_ms'] = round((self.monotonic()-started)*1000, 3)
                return self._valid(run)
            except Exception as exc:
                record_error(phase, exc)
                attempt = phase.get('attempt_count', 1)
                if (isinstance(exc, AppError) and exc.code in ('network_error','school_http')
                        and attempt <= len(PREPARE_RETRY_DELAYS) and self.clock_ok
                        and self.reservations.allow_live and not self.stopping.is_set()
                        and self.wall_clock() + PREPARE_RETRY_MIN_GAP_SECONDS <= run.due_at + 30
                        and self._valid(run)):
                    phase['retryable'] = self.store.transition_run(run.key, ('preparing',), 'queued',
                        {'message':'提前登录连接暂时失败，等待有限重试'}, revision=run.revision)
                    return False
                self.store.transition_run(run.key,('queued','preparing'),'failed',
                    {'message':str(exc) if isinstance(exc,AppError) else '提前登录失败，请检查学校连接'},revision=run.revision)
                return False

    def _failed_worker(self, run):
        """A worker failure affects only its unsubmitted run; IO stays off the deadline thread."""
        try:
            self.store.transition_run(run.key, ('queued','preparing'), 'failed',
                {'message':'本账号准备中断，请检查连接或配置'}, revision=run.revision)
        except Exception:
            LOGGER.warning('Could not persist preparation failure: %s', run.key)
        return False

    def _submit(self, run, phase=None):
        phase = phase if phase is not None else {}
        try:
            if self.clock_ok and not self.stopping.is_set():
                waiting = self.monotonic()
                with self.accounts.account_lock(run.account_id):
                    phase['account_lock_wait_ms'] = round((self.monotonic()-waiting)*1000, 3)
                    phase['execution_started_at'] = self.wall_clock()
                    if (run.kind == 'scheduled' and not run.operation_id
                            and self._cohort_handled(run, segmented_history_only=True)):
                        message = '该使用日期已有多时段定时执行或停止记录，本段未提交；切换计划格式不会再次提交'
                        self.store.ensure_run(run)
                        self._skip_segments((run,), message)
                        return RunResult('skipped', message, [])
                    if run.kind == 'scheduled' and run.operation_id:
                        row = self.store.get_account(run.account_id)
                        plan = validate_plan(row['plan'], self.accounts.catalog)
                        release = date.fromisoformat(run.target_date) - timedelta(days=plan.day_offset)
                        runs = resolve_runs(run.account_id, run.revision, plan, release, row.get('date_exceptions'))
                        if not runs or runs[0] != run or not self._valid(run):
                            self._skip_siblings(run, '计划已更新或关闭，后续时段未提交')
                            return None
                        if self._cohort_handled(run):
                            self._skip_segments(runs, '该使用日期已有定时执行或停止记录，本段未提交；不会因修改计划再次提交')
                            return None
                        leader = self.store.get_run(run.key)
                        if leader and leader['status'] not in ('queued', 'preparing'):
                            self._skip_segments(runs[1:], '本轮执行已经结束或中断，本段未提交；不会自动重交')
                            return RunResult(leader['status'], leader.get('message', '本轮已处理'), [])
                        if not self._claim_cohort(run):
                            self._skip_segments(runs, '该使用日期的定时批次已认领，本段未提交；不会自动重交')
                            return None
                        for index, segment in enumerate(runs):
                            result = self.reservations.execute(segment)
                            if result.status != 'success' and not clear_busy_failure(self.store.get_run(segment.key)):
                                self._skip_segments(runs[index+1:], '前段结果待核对或执行已停止，本段未提交；不会自动重交')
                                return result
                        return result
                    return self.reservations.execute(run)
        except Exception:
            self.store.transition_run(run.key,('queued','preparing','submitting'),'uncertain',{'message':'执行中断，请先查询学校记录'},revision=run.revision)
            self._skip_siblings(run, '批次执行中断，本段未提交；不会自动重交')
        finally:self.accounts.busy.discard(run.account_id)

    def _skip_segments(self, runs, message, *, handled=True):
        for run in runs:
            row = self.store.get_run(run.key)
            if row and 'native_binding' in row:
                # The native authority owns classification and reconciliation.
                continue
            fields = {'message': message}
            if handled and run.operation_id:
                fields['result'] = {'status': 'skipped', 'message': message, 'records': [],
                    'verified_at': None, 'scheduled_cohort_handled': True}
            self.store.transition_run(run.key, ('queued', 'preparing'), 'skipped',
                                      fields, revision=run.revision)

    def _cohort_handled(self, run, *, segmented_history_only=False):
        return cohort_handled(self.store, run, segmented_history_only=segmented_history_only)

    def _claim_cohort(self, run):
        # Claim before any POST. The claim survives any later run/result state,
        # trigger edit, segment replacement or process interruption.
        try:
            result = self.store.db.campus_scheduled_cohorts.update_one(
                {'_id': run.account_id + ':' + run.target_date}, {'$setOnInsert': {
                    'account_id': run.account_id, 'target_date': run.target_date,
                    'revision': run.revision, 'first_run_id': run.key,
                    'due_at': run.due_at, 'claimed_at': self.wall_clock()}}, upsert=True)
            return result.upserted_id is not None
        except DuplicateKeyError:
            return False

    def _skip_siblings(self, run, message, *, handled=True):
        if run.kind != 'scheduled' or not run.operation_id: return
        siblings = self.store.runs.find({'account_id': run.account_id, 'target_date': run.target_date,
            'spec.revision': run.revision, 'spec.kind': 'scheduled', 'spec.operation_id': {'$ne': None},
            'status': {'$in': ['queued', 'preparing']}})
        self._skip_segments((RunSpec(**row['spec']) for row in siblings), message, handled=handled)

    def _warm(self, run, phase=None):
        phase = phase if phase is not None else {}
        waiting = self.monotonic()
        with self.accounts.account_lock(run.account_id):
            phase['account_lock_wait_ms'] = round((self.monotonic()-waiting)*1000, 3)
            if not self._valid(run) or not self.reservations.allow_live or not self.reservations.guard():
                return False
            try:
                phase['active_step'] = 'session'
                started = self.monotonic()
                try:client = self.accounts.client(run.account_id)
                finally:phase['session_ms'] = round((self.monotonic()-started)*1000, 3)
                prepare = getattr(client, 'prepare_submission', None)
                if prepare:
                    phase['active_step'] = 'payload'
                    started = self.monotonic()
                    try:
                        scheduled = getattr(client, 'prepare_scheduled_submission', None)
                        if scheduled:
                            budget = max(.05, run.due_at-self.wall_clock()-.2)
                            scheduled(run, run.seat_ids[0], self.monotonic()+budget)
                        else:prepare(run, run.seat_ids[0])
                        valid_until = getattr(client, 'prepared_until', None)
                        if valid_until:
                            expires = valid_until(run, run.seat_ids[0])
                            remaining = max(0, run.due_at-self.wall_clock())
                            phase['refresh_needed'] = expires is None or expires < self.monotonic()+remaining+.05
                    finally:
                        phase['payload_prepare_ms'] = round((self.monotonic()-started)*1000, 3)
                        phase['payload_prepare_total_ms'] = round(
                            phase.get('payload_prepare_total_ms', 0)+phase['payload_prepare_ms'], 3)
            except AppError as exc:
                record_error(phase, exc)
                # A failed read-only warmup falls back to the ordinary submit path.
                phase['refresh_needed'] = True
                return self._valid(run)
            return self._valid(run)

    def refresh(self):
        now = self.wall_clock(); today = datetime.fromtimestamp(now,ZONE).date()
        active, upcoming, errors = {}, [], set()
        for a in self.store.list_accounts():
            try:
                from .dual_node.fence import legacy_allowed
                if not legacy_allowed(self.accounts,a.id):continue
                if not a.plan or not a.plan.get('enabled') or not self.accounts.is_allowed(a.student_id): continue
                plan = validate_plan(a.plan,self.accounts.catalog)
                exceptions = validate_date_exceptions(a.date_exceptions, self.accounts.catalog)
                following = next_scheduled_run(a.id, a.revision, plan, datetime.fromtimestamp(now, ZONE), exceptions)
                if following:
                    upcoming.append(following.due_at)
                for delta in (-1,0,1):
                    runs = resolve_runs(a.id,a.revision,plan,today+timedelta(days=delta), date_exceptions=exceptions)
                    if not runs: continue
                    run = runs[0]
                    if run.due_at >= now: upcoming.append(run.due_at)
                    if not (self.reservations.allow_live and now-30 <= run.due_at <= now+180):continue
                    key = (run.key,run.revision)
                    account_lock = self.accounts.account_lock(a.id)
                    if not account_lock.acquire(blocking=False):
                        # Its own login/query may own this lock. Keep an already
                        # discovered task without making other accounts wait.
                        with self._lock:
                            if key in self.jobs:active[key] = run
                        continue
                    try:
                        if not self._valid(run): continue
                        rows = [self.store.ensure_run(segment) for segment in runs]
                        row = rows[0]
                        if not run.operation_id and self._cohort_handled(run, segmented_history_only=True):
                            self._skip_segments(runs, '该使用日期已有多时段定时执行或停止记录，本段未提交；切换计划格式不会再次提交')
                            continue
                        if run.operation_id:
                            if self._cohort_handled(run):
                                self._skip_segments(runs, '该使用日期已有定时执行或停止记录，本段未提交；不会因修改计划再次提交')
                                continue
                            # A newly started scheduler never replays a past
                            # segmented release, including unsubmitted siblings.
                            if run.due_at < self.anchor_wall:
                                self._skip_segments(runs, '放号时刻早于本次后台启动，本段未提交；不会自动补交')
                                continue
                            if row['status'] not in ('queued', 'preparing'):
                                self._skip_segments(runs[1:], '本轮执行已经结束或中断，本段未提交；不会自动重交')
                                continue
                    finally:account_lock.release()
                    if row['status'] in ('queued','preparing') and row['spec']['revision'] == run.revision:
                        active[key] = run
            except Exception as exc:
                errors.add(a.id)
                # A temporary read failure must not discard an existing task;
                # final execution still validates the current plan and access.
                if not isinstance(exc, AppError):
                    with self._lock:
                        for key, job in self.jobs.items():
                            if job['run'].account_id == a.id:active[key] = job['run']
        self.next_due = min(upcoming) if upcoming else None
        with self._lock:
            self._account_errors = errors
            for key,run in active.items():self.jobs.setdefault(key,{'run':run,'prep':None,'warm':None,'submit':None})
            stale = [(key,job) for key,job in self.jobs.items() if key not in active and not job['submit']]
        for key,job in stale:
            run=job['run']
            if run.operation_id:
                self._skip_segments((run,), '计划已关闭、更新或过期', handled=self.wall_clock() >= run.due_at)
            else:
                self.store.transition_run(run.key,('queued','preparing'),'skipped',{'message':'计划已关闭、更新或过期'},revision=run.revision)
            self._skip_siblings(run, '计划已关闭、更新或过期，本段未提交', handled=self.wall_clock() >= run.due_at)
            with self._lock:
                if self.jobs.get(key) is job and not job['submit']:
                    if all(not job.get(stage) or job[stage].done() for stage in ('prep','warm')):
                        self.accounts.busy.discard(run.account_id);del self.jobs[key]
        # Rechecks are read-only, including after restarting with the live gate closed.
        for row in self.store.runs.find({'status':'uncertain','attempted_seat_id':{'$ne':None}}).limit(100):
            count=row.get('reconcile_count',0);key=row['_id']
            future=self.rechecks.get(key)
            if count < 3 and (not future or future.done()) and now >= row.get('submitted_at',now)+[1,3,10][count]:
                self.store.runs.update_one({'_id':key,'status':'uncertain','reconcile_count':count},{'$inc':{'reconcile_count':1}})
                self.rechecks[key]=self.prep_pool.submit(self.reservations.reconcile,key)

    def tick(self):
        offset = (self.wall_clock()-self.anchor_wall)-(self.monotonic()-self.anchor_mono)
        if abs(offset) > 2:
            if self.clock_ok:
                LOGGER.error('Scheduler clock blocked at %s: wall/elapsed delta %.3fs (%s)',
                             datetime.now(ZONE).isoformat(), offset, CLOCK_SOURCE)
            self.clock_ok=False;self.last_error='本机时间发生跳变，请核对时间后重启后台'
        if not self.clock_ok or not self.reservations.allow_live: return
        with self._lock:
            for key,job in list(self.jobs.items()):
                run=job['run']
                if job['submit']:
                    if job['submit'].done():del self.jobs[key]
                    continue
                if job['prep'] is None:
                    if self.wall_clock() < job.get('prepare_retry_at', 0):continue
                    job['prep']=self._queue_stage(job,'prepare',self.prep_pool,self._prepare);continue
                if not job['prep'].done():continue
                try:prepared = job['prep'].result()
                except Exception:
                    LOGGER.warning('Preparation worker interrupted: %s', run.key)
                    job.setdefault('timings', {}).setdefault('prepare', {})['retryable'] = False
                    job['prep'] = self.prep_pool.submit(self._failed_worker, run)
                    continue
                if not prepared:
                    phase = job.get('timings', {}).get('prepare', {})
                    if phase.get('retryable'):
                        retry_at = prepare_retry_at(run, phase.get('attempt_count'), self.wall_clock(),
                                                    deadline=run.due_at + 30)
                        if retry_at is not None:
                            job['prepare_retry_at'] = retry_at
                            job['prep'] = None
                            continue
                        self.store.transition_run(run.key, ('queued','preparing'), 'skipped',
                            {'message':'提前认证重试已超过有效窗口，不补交'}, revision=run.revision)
                    self._skip_siblings(run, '首段准备未完成，本段未提交；不会自动重交')
                    self._record_stop(job,'prepare_failed')
                    self.accounts.busy.discard(run.account_id);del self.jobs[key];continue
                if job.get('warm') is None:
                    # Start slow reads early; refresh fast results close to release
                    # rather than extending the two-second payload lifetime.
                    if self.wall_clock() >= run.due_at - WARMUP_LEAD_SECONDS:
                        job['warm'] = self._queue_stage(job,'warm',self.warm_pool,self._warm)
                    continue
                if not job['warm'].done():continue
                try:warmed = job['warm'].result()
                except Exception:
                    LOGGER.warning('Warmup worker interrupted: %s', run.key)
                    job.setdefault('timings', {}).setdefault('prepare', {})['retryable'] = False
                    job['prep'] = self.prep_pool.submit(self._failed_worker, run)
                    job['warm'] = None
                    continue
                if not warmed:
                    self._skip_siblings(run, '执行校验已失效，本段未提交')
                    self.accounts.busy.discard(run.account_id);del self.jobs[key];continue
                phase = job.get('timings', {}).get('warm', {})
                if (phase.get('refresh_needed') and phase.get('attempt_count', 1) < 2
                        and self.wall_clock() < run.due_at):
                    if self.wall_clock() >= run.due_at-WARMUP_REFRESH_LEAD_SECONDS:
                        job['warm'] = self._queue_stage(job,'warm',self.warm_pool,self._warm)
                    continue
                if self.wall_clock() >= run.due_at:job['submit']=self._queue_stage(job,'submit',self.submit_pool,self._submit)

    def _deadline_interval(self):
        if self.clock_ok and self.reservations.allow_live:
            now = self.wall_clock()
            with self._lock:
                remaining = [job['run'].due_at-now for job in self.jobs.values() if not job['submit']]
            if any(-.05 <= value <= .05 for value in remaining):return .001
        return .01

    def start(self):
        if self.threads or self.closed:return
        with self._lock:
            self._last_success = {'refresh': self.monotonic(), 'tick': self.monotonic()}
        def loop(name,action,interval):
            while not self.stopping.is_set():
                delay = .01 if callable(interval) else interval
                try:
                    action()
                    delay = interval() if callable(interval) else interval
                except Exception:
                    with self._lock:
                        if name not in self._loop_errors:
                            LOGGER.warning('Scheduler loop interrupted; retrying: %s', threading.current_thread().name)
                        self._loop_errors.add(name)
                else:
                    with self._lock:
                        self._loop_errors.discard(name)
                        self._last_success[name] = self.monotonic()
                self.stopping.wait(delay)
        self.threads=[threading.Thread(target=loop,args=('refresh',self.refresh,.5),name='njfu-plan',daemon=True),
                      threading.Thread(target=loop,args=('tick',self.tick,self._deadline_interval),name='njfu-deadline',daemon=True)]
        for thread in self.threads:thread.start()

    def status(self):
        with self._lock:
            running = bool(self.threads) and all(t.is_alive() for t in self.threads) and not self.stopping.is_set()
            ages = {name: max(0, self.monotonic()-value) for name, value in self._last_success.items()}
            stale = running and any(value > LOOP_STALE_SECONDS for value in ages.values())
            warning = ('调度检查已超过5秒未完成，请检查本机数据库或后台阻塞' if stale else
                       '后台短暂异常，正在自动重试' if self._loop_errors else
                       '部分账号计划检查异常，其他账号继续运行，请管理员核对' if self._account_errors else None)
            ready = running and self.clock_ok and self.reservations.allow_live and not self.last_error and not warning
            message = ('自动预约已暂停' if not self.clock_ok or self.last_error else
                       '执行未开启（仅登录与查询）' if not self.reservations.allow_live else
                       '定时预约已开启' if running else '自动预约未运行')
            return {'scheduler_running':running,'clock_ok':self.clock_ok,'ready_to_submit':bool(ready),'clock_source':CLOCK_SOURCE,
                'allow_live':self.reservations.allow_live,'capacity':PARALLEL_ACCOUNT_LIMIT,'preparing':sum(bool(j['prep']) and not j['submit'] for j in self.jobs.values()),
                'queued':len(self.jobs),'next_due':self.next_due,'last_error':self.last_error,
                'scheduler_warning':warning,
                'loop_age_seconds': {name: round(value, 3) for name, value in ages.items()},
                'execution_message':message}

    def stop(self):
        if self.closed:return
        self.closed=True;self.stopping.set()
        for thread in self.threads:thread.join()
        self.warm_pool.shutdown(wait=True,cancel_futures=True)
        self.submit_pool.shutdown(wait=True,cancel_futures=True)
        self.prep_pool.shutdown(wait=True)
        self.diagnostic_pool.shutdown(wait=True)
        self.accounts.busy.clear()
