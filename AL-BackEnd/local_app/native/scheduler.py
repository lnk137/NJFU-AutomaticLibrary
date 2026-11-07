"""Native deadline sends after independent bounded canonical-payload warmup."""
from concurrent.futures import wait

from local_app.scheduler import LocalScheduler,WARMUP_LEAD_SECONDS,WARMUP_REFRESH_LEAD_SECONDS
from local_app.models import AppError,RunResult
from local_app.timing_diagnostics import record_error
from .discovery import NativeScheduler
from .request import prepared_session,remember_preparation


class FallbackReservations:
    """Only explicitly routed, never-native-registered work uses Python sends."""
    def __init__(self,parent):self.parent=parent;self.guard=lambda:True
    @property
    def allow_live(self):
        parent=self.parent
        return bool(parent.clock_ok and not parent.maintenance.is_set()
            and not parent.stopping.is_set() and parent.dispatch.service.allow_live)
    def execute(self,run):
        parent=self.parent
        from local_app.dual_node.fence import require_legacy
        require_legacy(parent.accounts,run)
        # Do not combine the bounded Python queue with outstanding native
        # first sends or continuations. Native ownership is never transferred.
        while True:
            if not self.allow_live or not self.guard() or parent.wall_clock()>run.due_at+30:
                return self._stopped(run)
            with parent.operation_lock:
                pending=any(not future.done() for future in parent.batches.values())
            if not pending:break
            if parent.stopping.wait(.01):return self._stopped(run)
        row=parent.store.get_run(run.key)
        if not row or row.get('native_binding'):
            raise AppError('native_ownership','原生资格已登记，禁止切换发送路径',409)
        return parent.dispatch.service.execute(run)
    def _stopped(self,run):
        row=self.parent.store.get_run(run.key)
        if row and row.get('native_binding'):
            raise AppError('native_ownership','原生资格已登记，禁止切换发送路径',409)
        message='执行已暂停或有效窗口已结束，本段未提交；不会自动重交'
        self.parent.store.transition_run(run.key,('queued','preparing'),'skipped',
            {'message':message},revision=run.revision)
        return RunResult('skipped',message,[])
    def reconcile(self,key):return self.parent.dispatch.service.reconcile(key)


class PythonFallback(LocalScheduler):
    """Reuse original bounded workers; discovery remains owned by the parent."""
    def __init__(self,parent):
        self.parent=parent
        super().__init__(parent.accounts,FallbackReservations(parent),parent.store,
            wall_clock=parent.wall_clock,monotonic=parent.monotonic)
    def refresh(self):pass
    def route(self,candidates,migrated):
        with self._lock:
            for key,run in candidates.items():
                if key in self.jobs:continue
                row=self.store.get_run(run.key) or self.store.ensure_run(run)
                if row.get('native_binding') or row['status'] not in {'queued','preparing'}:continue
                if row['spec']['revision']!=run.revision:continue
                self.jobs[key]=migrated.get(key) or {'run':run,'prep':None,'warm':None,'submit':None}
            for key,job in list(self.jobs.items()):
                if key in candidates or job['submit']:continue
                if any(job.get(stage) and not job[stage].done() for stage in ('prep','warm')):continue
                run=job['run'];handled=self.wall_clock()>=run.due_at
                self._skip_segments((run,),'计划已关闭、更新或过期',handled=handled)
                self._skip_siblings(run,'计划已关闭、更新或过期，本段未提交',handled=handled)
                self.accounts.busy.discard(run.account_id);self.jobs.pop(key,None)
    def pending(self):
        with self._lock:return bool(self.jobs)
    def pending_legacy(self):
        with self._lock:return any(not job['run'].operation_id for job in self.jobs.values())
    def futures(self):
        with self._lock:
            return [future for job in self.jobs.values() for stage in ('prep','warm','submit')
                    if (future:=job.get(stage)) is not None]


class CampusScheduler(NativeScheduler):
    MIN_ARM_LEAD=.05
    MAX_WARM_ATTEMPTS=4
    WARM_RETRY_GAP=.1
    WARM_RETRY_BUDGET=.25
    TRANSIENT_WARM_ERRORS=frozenset({'network_error','school_http','native_payload_expired','native_session_changed'})
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.dispatch.service.guard=lambda:(self.clock_ok and not self.stopping.is_set()
            and not self.maintenance.is_set() and self.dispatch.clock_ok() and not self.bridge_failed())
        self.fallback=PythonFallback(self)

    def route_candidates(self,candidates):
        from local_app.dual_node.fence import legacy_allowed
        candidates={key:run for key,run in candidates.items() if legacy_allowed(self.accounts,run)}
        capacity=len(self.dispatch.native.workers)
        use_python=sum(not run.operation_id for run in candidates.values())>capacity or self.fallback.pending_legacy()
        retained={};routed={};migrated={}
        with self._lock:
            for key,run in candidates.items():
                job=self.jobs.get(key)
                if job and job['submit']:
                    retained[key]=run;continue
                row=self.store.get_run(run.key)
                if row and row.get('native_binding'):
                    retained[key]=run;continue
                if not run.operation_id and not use_python:
                    retained[key]=run;continue
                routed[key]=run
                if job:migrated[key]=self.jobs.pop(key)
        self.fallback.route(routed,migrated)
        return retained

    def start(self):
        super().start()
        if not self.closed:self.fallback.start()

    def stop(self):
        if self.closed:return
        self.stopping.set();self.maintenance.set()
        self.fallback.stop()
        super().stop()

    def recover(self):
        with self.operation_lock:
            self.maintenance.set();self.fallback.clock_ok=False
        if wait(self.fallback.futures(),timeout=5).not_done:
            self.dispatch.bridge.outbox.failed=True
            raise AppError('python_results_busy','排队任务尚未结束，保持暂停',409)
        count=super().recover()
        with self.fallback._lock:
            self.fallback.jobs.clear()
            self.fallback.anchor_wall,self.fallback.anchor_mono=self.wall_clock(),self.monotonic()
            self.fallback.clock_ok=True;self.fallback.last_error=None
        return count

    def status(self):
        result=super().status();fallback=self.fallback.status()
        result['queued']+=fallback['queued'];result['preparing']+=fallback['preparing']
        result['python_fallback_queued']=fallback['queued']
        if fallback['queued']:
            result['ready_to_submit']=bool(result['ready_to_submit'] and fallback['ready_to_submit'])
            result['scheduler_warning']=result['scheduler_warning'] or fallback['scheduler_warning']
        result['executor']='cpp'
        result['executor_state']='ready' if result['ready_to_submit'] else 'recovering' if result.get('recovery_required') else 'paused'
        diagnostics=getattr(self,'runtime_diagnostics',None)
        if diagnostics:
            details=diagnostics()
            result.update(details)
            if details['executor_state'] in {'recovering','blocked'}:
                result['ready_to_submit']=False
                result['scheduler_warning']=details.get('executor_message') or '执行连接正在恢复，请稍后查看'
            elif details.get('operator_paused'):
                result['ready_to_submit']=False
                result['execution_message']='管理员已暂停自动预约'
        return result

    def refresh(self):
        super().refresh()
        with self._lock:active={(job['run'].key,job['run'].revision) for job in self.jobs.values()}
        self.dispatch.reap_warm(active)

    def bridge_failed(self):return self.dispatch.bridge.outbox.failed

    def _finish_batch(self,runs,future,results=None,error=None):
        # Collectors run after responses, away from deadline dispatch. Retain
        # the existing dashboard's measured phase schema.
        for run in runs:
            with self._lock:
                job=next((j for j in self.jobs.values() if j['run'].key==run.key),None)
                phases=dict(job.get('timings',{})) if job else {}
            if phases:self._save_timings(run,phases)
        super()._finish_batch(runs,future,results,error)

    def _warm(self,run,phase=None):
        phase=phase if phase is not None else {}
        with self.accounts.account_lock(run.account_id):
            phase['active_step']='session'
            if not self._valid(run):
                raise AppError('native_state_changed','本次预约计划或权限已变化，未继续预热',409)
            if not self.reservations.allow_live or not self.reservations.guard():
                raise AppError('live_disabled','执行校验失效，未继续预热',409)
            client=self.accounts.client(run.account_id)
            phase['active_step']='payload'
            before=prepared_session(client);client._native_prepared_session=None
            ready=super()._warm(run,phase)
            if not ready or phase.get('error_code'):return False
            remember_preparation(client,before)
            budget=min(3500,max(50,int((run.due_at-self.wall_clock()-.2)*1000)))
            phase['active_step']='transport';started=self.monotonic()
            try:self.dispatch.warm_transport(run,budget)
            finally:phase['transport_ms']=round((self.monotonic()-started)*1000,3)
            expires=client.prepared_until(run,run.seat_ids[0])
            remaining=max(0,run.due_at-self.wall_clock())
            phase['payload_remaining_ms']=round((expires-self.monotonic())*1000,3) if expires is not None else 0
            phase['refresh_needed']=expires is None or expires<self.monotonic()+remaining+.05
            return ready

    def payload_ready(self,job,now):
        run=job['run']
        if now<run.due_at-WARMUP_LEAD_SECONDS:return False
        if job['warm'] is None:
            job['warm']=self._queue_stage(job,'warm',self.warm_pool,self._warm)
            self.preflights.add(job['warm']);return False
        if not job['warm'].done():return False
        try:ready=job['warm'].result()
        except Exception as exc:
            ready=False
            record_error(job.setdefault('timings',{}).setdefault('warm',{}),exc)
        phase=job.get('timings',{}).get('warm',{})
        if ready and not phase.get('refresh_needed'):return True
        code=phase.get('error_code')
        attempt=phase.get('attempt_count',0)
        retryable=(code in self.TRANSIENT_WARM_ERRORS or
                   (ready and phase.get('refresh_needed') and not code))
        phase['retryable']=bool(retryable and attempt<self.MAX_WARM_ATTEMPTS
                               and now+self.WARM_RETRY_BUDGET<run.due_at)
        if not phase['retryable']:return False
        retry_at=phase.get('finished_at',now)+self.WARM_RETRY_GAP
        # Normal fast results wait for a fresh payload near release. Repeated
        # early faults also reserve the final attempt for that opportunity.
        if (ready and not code) or attempt>=self.MAX_WARM_ATTEMPTS-1:
            retry_at=max(retry_at,run.due_at-WARMUP_REFRESH_LEAD_SECONDS)
        phase['next_retry_at']=retry_at
        if now<retry_at:return False
        job['warm']=self._queue_stage(job,'warm',self.warm_pool,self._warm)
        self.preflights.add(job['warm'])
        return False
