"""Reuse discovery/preflight/lifecycle, ARM batches before due, collect off-thread."""
from concurrent.futures import Future,ThreadPoolExecutor,wait
from dataclasses import asdict
from datetime import datetime,timedelta
import hashlib
import threading
import time
import uuid
from local_app.models import AppError,RunSpec,ZONE,resolve_run,resolve_runs,validate_plan
from local_app.date_exceptions import next_scheduled_run,validate_date_exceptions
from .protocol import clock_ns
from local_app.scheduler import LocalScheduler,prepare_retry_at
from local_app.elapsed_clock import elapsed_time


class NativeReservations:
    def __init__(self,dispatch):self.dispatch=dispatch;self.guard=lambda:True
    @property
    def allow_live(self):
        return self.dispatch.service.allow_live
    def reconcile(self,key):return self.dispatch.receipts.reconcile(key)
    def execute(self,*args):raise AssertionError('deadline Python submission is forbidden')


class NativeScheduler(LocalScheduler):
    MIN_ARM_LEAD=.25
    def __init__(self,accounts,dispatch,store,*,wall_clock=time.time,monotonic=None):
        self.dispatch=dispatch
        self.operation_lock=threading.RLock();self.maintenance=threading.Event()
        self.discovery_lock=threading.RLock();self.recovery_lock=threading.Lock()
        self.arm_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='native-arm')
        self.collect_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='native-collect')
        self.receipt_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='native-receipt')
        self.batches={};self.capacity_error=None;self.recovery_error=None
        self.preflights=set()
        self.admitted={};self.completed_batches=0;self._plan_signature=None
        super().__init__(accounts,NativeReservations(dispatch),store,wall_clock=wall_clock,monotonic=monotonic or elapsed_time)

    def refresh(self):
        with self.discovery_lock:
            if self.stopping.is_set() or self.maintenance.is_set():return
            self._followup()
            if not self.reservations.allow_live:return
            now=self.wall_clock();today=datetime.fromtimestamp(now,ZONE).date()
            candidates={};upcoming=[];errors=set();signature=[]
            for account in self.store.list_accounts():
                signature.append((account.id,account.revision))
                try:
                    from local_app.dual_node.fence import legacy_allowed
                    if not legacy_allowed(self.accounts,account.id):continue
                    if not account.plan or not account.plan.get('enabled') or not self.accounts.is_allowed(account.student_id):continue
                    plan=validate_plan(account.plan,self.accounts.catalog)
                    exceptions=validate_date_exceptions(account.date_exceptions,self.accounts.catalog)
                    following=next_scheduled_run(account.id,account.revision,plan,datetime.fromtimestamp(now,ZONE),exceptions)
                    if following:upcoming.append(following.due_at)
                    for delta in (-1,0,1):
                        runs=resolve_runs(account.id,account.revision,plan,today+timedelta(days=delta),date_exceptions=exceptions)
                        if not runs or not now-30<=runs[0].due_at<=now+180:continue
                        run=runs[0];key=(run.key,run.revision)
                        lock=self.accounts.account_lock(account.id)
                        if not lock.acquire(blocking=False):
                            # Preparation and complete segmented execution own
                            # this lock. Keep their existing route while busy.
                            fallback=getattr(self,'fallback',None)
                            with self._lock:known=key in self.jobs
                            if fallback:
                                with fallback._lock:known=known or key in fallback.jobs
                            if known:candidates[key]=run
                            continue
                        try:
                            if not self._valid(run):continue
                            rows=[self.store.ensure_run(segment) for segment in runs]
                            if self._cohort_handled(run,segmented_history_only=not bool(run.operation_id)):
                                self._skip_segments(runs,'该使用日期已有执行或停止记录，本段未提交；不会因修改计划再次提交')
                                continue
                            if run.operation_id:
                                if run.due_at<self.anchor_wall:
                                    self._skip_segments(runs,'放号时刻早于本次后台启动，本段未提交；不会自动补交')
                                    continue
                                if rows[0]['status'] not in {'queued','preparing'}:
                                    self._skip_segments(runs[1:],'本轮执行已经结束或中断，本段未提交；不会自动重交')
                                    continue
                            candidates[key]=run
                        finally:lock.release()
                except Exception:errors.add(account.id)
            with self.operation_lock:
                if self.maintenance.is_set() or self.stopping.is_set() or not self.reservations.allow_live:return
                if tuple(signature)!=self._plan_signature:self.capacity_error=None;self._plan_signature=tuple(signature)
                self.next_due=min(upcoming) if upcoming else None;self._account_errors=errors
                candidates=self.route_candidates(candidates)
                for key,run in candidates.items():
                    if key in self.jobs:continue
                    existing=self.store.get_run(run.key)
                    # A recovered unclaimed task needs authentication again;
                    # preflight is safe, while ARM remains an explicit action.
                    if existing and existing.get('native_binding') and not existing['native_binding'].get('rebind_required'):continue
                    row=self.store.ensure_run(run)
                    if row['status'] in {'queued','preparing'} and row['spec']['revision']==run.revision:
                        with self._lock:self.jobs.setdefault(key,{'run':run,'prep':None,'warm':None,'submit':None})
                for key,job in list(self.jobs.items()):
                    if key in candidates or job['submit'] or (job['prep'] and not job['prep'].done()):continue
                    self.store.transition_run(job['run'].key,('queued','preparing'),'skipped',{'message':'计划已关闭、更新或过期'},revision=job['run'].revision)
                    self.accounts.busy.discard(job['run'].account_id)
                    with self._lock:self.jobs.pop(key,None)

    def route_candidates(self,candidates):return candidates
    def payload_ready(self,job,now):return True

    def _followup(self):
        # Pausing sends does not pause independent read-only receipt checks.
        self.rechecks={key:future for key,future in self.rechecks.items() if not future.done()}
        if self.dispatch.bridge.outbox.failed or len(self.rechecks)>=4:return
        now=self.wall_clock()
        eligible={'status':'uncertain','attempted_seat_id':{'$exists':True,'$ne':None},
                  '$or':[{'reconcile_count':{'$lt':3}},{'reconcile_count':{'$exists':False}}]}
        for row in self.store.runs.find(eligible).sort('submitted_at',1).limit(100):
            count=row.get('reconcile_count',0)
            if row['_id'] in self.rechecks or not 0<=count<3 or now<row.get('submitted_at',now)+[1,3,10][count]:continue
            condition={'_id':row['_id'],'status':'uncertain','reconcile_count':count}
            if 'reconcile_count' not in row:condition['reconcile_count']={'$exists':False}
            changed=self.store.runs.update_one(condition,{'$inc':{'reconcile_count':1}})
            if changed.matched_count:self.rechecks[row['_id']]=self.receipt_pool.submit(self.reservations.reconcile,row['_id'])
            if len(self.rechecks)>=4:break

    def _finish_batch(self,runs,future,results=None,error=None):
        try:
            for run in runs:
                try:
                    if error:
                        uncertain=isinstance(error,AppError) and error.code in {'native_arm_uncertain','native_sync_pending','native_already_staged'}
                        self.store.transition_run(run.key,('queued','preparing'),'uncertain' if uncertain else 'skipped',
                            {'message':'原生准备未确认，禁止自动重交' if uncertain else '准备窗口或配置已失效，未补交'},revision=run.revision)
                except Exception:
                    self.dispatch.bridge.outbox.failed=True
                    self.recovery_error='任务状态未能保存，请恢复后核对'
                finally:self.accounts.busy.discard(run.account_id)
        finally:
            if not future.done():future.set_result(results or [])

    def _collect(self,runs,prepared,completion):
        try:results=self.dispatch.collect(prepared)
        except Exception as exc:
            self.dispatch.bridge.outbox.failed=True
            self._finish_batch(runs,completion,error=exc)
        else:self._finish_batch(runs,completion,results=results)

    def _arm(self,runs,completion,*,rebind=False):
        prepared=None
        try:
            with self.operation_lock:
                if self.maintenance.is_set() or self.stopping.is_set():raise AppError('native_paused','调度已暂停',409)
                prepared=self.dispatch.prepare(runs,allow_rebind=rebind)
                self.collect_pool.submit(self._collect,runs,prepared,completion)
        except Exception as exc:
            if prepared is not None:
                self.dispatch.bridge.outbox.failed=True;self.dispatch.native.close_control()
                exc=AppError('native_arm_uncertain','结果收集未取得确认，禁止重交',503)
            self._finish_batch(runs,completion,error=exc)

    def _failed_preflight(self,run):
        # An interrupted preparation Future must not leave an invisible
        # preparing row that can never pass the queued-only preparation claim.
        try:
            changed=self.store.transition_run(run.key,('queued','preparing'),'failed',
                {'message':'本账号提前认证中断，请检查连接后恢复'},revision=run.revision)
            row=self.store.get_run(run.key) if not changed else None
            if row and row['status'] in {'queued','preparing'} and row['spec']['revision']==run.revision:
                raise RuntimeError('preflight failure was not persisted')
        except Exception:
            self.dispatch.bridge.outbox.failed=True
            self.recovery_error='提前认证状态未能保存，请恢复后核对'
        return False

    def tick(self):
        offset=(self.wall_clock()-self.anchor_wall)-(self.monotonic()-self.anchor_mono)
        if abs(offset)>.25 or not self.dispatch.clock_ok():
            self.clock_ok=False;self.last_error='时钟跳变，执行已暂停，请检查后恢复'
        with self.operation_lock:
            self.preflights={future for future in self.preflights if not future.done()}
            for token,future in list(self.batches.items()):
                if future.done():self.batches.pop(token,None);self.completed_batches+=1
            now=self.wall_clock()
            for due in list(self.admitted):
                if due<now-30:self.admitted.pop(due,None)
            with self._lock:
                for key,job in list(self.jobs.items()):
                    if job['submit'] and job['submit'].done():self.jobs.pop(key,None)
            if (self.maintenance.is_set() or self.stopping.is_set() or not self.clock_ok
                    or not self.reservations.allow_live):return
            groups={}
            with self._lock:items=list(self.jobs.items())
            cohorts={}
            for key,job in items:cohorts.setdefault(job['run'].due_at,[]).append((key,job))
            rejected=set()
            for due,cohort in cohorts.items():
                if len({job['run'].account_id for _,job in cohort})>len(self.dispatch.native.workers):
                    self.capacity_error='同一时刻账户超出原生容量；未登记账户不发送'
                    for key,job in cohort:
                        if job['submit']:continue
                        self.store.transition_run(job['run'].key,('queued','preparing'),'skipped',{'message':self.capacity_error},revision=job['run'].revision)
                        self.accounts.busy.discard(job['run'].account_id);rejected.add(key)
                        with self._lock:self.jobs.pop(key,None)
            for key,job in items:
                if key in rejected:continue
                if job['submit']:continue
                run=job['run']
                persisted=self.store.get_run(run.key)
                needs_rebind=(persisted or {}).get('native_binding',{}).get('rebind_required')
                if job['prep'] is None:
                    if now<job.get('prepare_retry_at',0):continue
                    job['prep']=self._queue_stage(job,'prepare',self.prep_pool,self._prepare)
                    self.preflights.add(job['prep']);continue
                if not job['prep'].done():continue
                try:ready=job['prep'].result()
                except Exception:
                    job.setdefault('timings',{}).setdefault('prepare',{})['retryable']=False
                    job['prep']=self.prep_pool.submit(self._failed_preflight,run)
                    self.preflights.add(job['prep'])
                    continue
                if not ready:
                    phase=job.get('timings',{}).get('prepare',{})
                    attempt=phase.get('attempt_count',0)
                    if phase.get('retryable'):
                        retry_at=prepare_retry_at(run,attempt,now,deadline=run.due_at-.25)
                        if retry_at is not None:
                            job['prepare_retry_at']=retry_at;job['prep']=None
                            continue
                        try:
                            changed=self.store.transition_run(run.key,('queued','preparing'),'skipped',
                                {'message':'提前认证重试已无法赶上准备窗口，不补交'},revision=run.revision)
                            if not changed:self._failed_preflight(run)
                        except Exception:
                            self.dispatch.bridge.outbox.failed=True
                            self.recovery_error='提前认证状态未能保存，请恢复后核对'
                    self._record_stop(job,'prepare_failed')
                    self.accounts.busy.discard(run.account_id)
                    with self._lock:self.jobs.pop(key,None)
                    continue
                remaining=run.due_at-now
                if remaining<self.MIN_ARM_LEAD:
                    self.store.transition_run(run.key,('queued','preparing'),'skipped',{'message':'准备窗口已错过，不补交'},revision=run.revision)
                    self._record_stop(job,'prepare_window_missed')
                    self.accounts.busy.discard(run.account_id)
                    with self._lock:self.jobs.pop(key,None)
                elif self.payload_ready(job,now) and remaining<=1.45:
                    groups.setdefault((run.due_at,bool(needs_rebind),self.dispatch.pilot_selected(run)),[]).append((key,job))
            for (due,rebind,_pilot),group in sorted(groups.items()):
                admitted=self.admitted.setdefault(due,set())
                members={job['run'].account_id for _,job in group}
                if len(admitted|members)>len(self.dispatch.native.workers):
                    self.capacity_error='新增账户超出原生容量，未为超额账户登记请求'
                    for key,job in group:
                        self.store.transition_run(job['run'].key,('queued','preparing'),'skipped',{'message':self.capacity_error},revision=job['run'].revision)
                        self.accounts.busy.discard(job['run'].account_id)
                        with self._lock:self.jobs.pop(key,None)
                    continue
                completion=Future();runs=[job['run'] for _,job in group]
                admitted.update(members)
                for _,job in group:job['submit']=completion
                self.batches[uuid.uuid4().hex]=completion
                try:self.arm_pool.submit(self._arm,runs,completion,rebind=rebind)
                except Exception as exc:self._finish_batch(runs,completion,error=exc)

    def _reconcile_unregistered(self):
        count=0
        rows=list(self.store.runs.find({'spec.kind':'scheduled','status':{'$in':['queued','preparing','submitting','uncertain','skipped']}}))
        for row in rows:
            spec=dict(row['spec']);spec['seat_ids']=tuple(spec['seat_ids']);run=RunSpec(**spec)
            from local_app.dual_node.fence import legacy_allowed
            if not legacy_allowed(self.accounts,run):
                if row['status'] in {'queued','preparing','submitting'}:
                    self.store.runs.update_one({'_id':row['_id'],'spec':row['spec'],'status':row['status']},
                        {'$set':{'status':'uncertain','message':'双机管理账号保持原路径停用；恢复不重新排队'}})
                continue
            if run.operation_id and not row.get('native_binding'):
                # Python owns segmented sends. A crash after its durable claim
                # requires read-only confirmation, never native re-queuing.
                if row['status']=='submitting':
                    self.store.transition_run(run.key,('submitting',),'uncertain',
                        {'message':'上次多时段提交中断，请查询学校记录核对；未重复提交'},revision=run.revision)
                    count+=1
                continue
            account=self.store.get_account(run.account_id)
            try:
                expected=resolve_run(run.account_id,account['revision'],validate_plan(account.get('plan'),self.accounts.catalog),
                    datetime.fromtimestamp(run.due_at,ZONE).date(),date_exceptions=validate_date_exceptions(account.get('date_exceptions',{}),self.accounts.catalog))
                valid=(not run.operation_id and expected==run and self.accounts.is_allowed(account['student_id'])
                    and run.due_at>self.wall_clock()+.25)
            except (AppError,KeyError,ValueError,TypeError):valid=False
            key='r'+hashlib.sha256(row['_id'].encode()).hexdigest()
            fields=self.dispatch.native.job_status(key).split()
            if fields==['NOTFOUND']:
                if row['status'] not in {'queued','preparing','submitting'}:continue
                state='queued' if valid else 'skipped'
                values={'status':state,'message':'已证明未登记，等待显式启用' if state=='queued' else '准备窗口已错过，不补交'}
                self.store.runs.update_one({'_id':row['_id'],'spec':row['spec'],'status':row['status']},{'$set':values,'$unset':{'native_binding':''}})
                count+=1
                continue
            if (len(fields)!=6 or fields[:2]!=['JOB',key] or fields[4] not in {'0','1'}
                    or not all(value.isdecimal() and int(value)>0 for value in fields[2:4])):
                raise AppError('invalid_native_readback','账本身份未取得有效确认，保持暂停',503)
            binding={'job':key,'epoch':int(fields[2]),'session':int(fields[3]),'worker':None,'rebind_required':False}
            condition={'_id':row['_id'],'spec':row['spec'],'status':row['status']}
            if fields[4]=='0' and (binding['epoch'],binding['session'])!=(self.dispatch.native.epoch,self.dispatch.native.session):
                if valid:
                    binding['rebind_required']=True
                    self.store.runs.update_one(condition,{'$set':{'status':'queued','native_binding':binding,'message':'账本证明未领取，须明确重新绑定'}})
                    count+=1
                else:
                    self.store.runs.update_one(condition,{'$set':{'status':'skipped','native_binding':binding,'message':'任务已过期或配置变化，本次不重交'}})
            elif fields[4]=='1' and row['status'] in {'queued','preparing','submitting','uncertain'}:
                self.store.runs.update_one(condition,{'$set':{'status':'uncertain','native_binding':binding,
                    'attempted_seat_id':run.seat_ids[0],'submitted_at':run.due_at,
                    'message':'持久资格已领取，禁止重交；请核对回执'}})
                count+=1
            else:self.store.runs.update_one(condition,{'$set':{'native_binding':binding}})
        return count

    def recover(self):
        if not self.recovery_lock.acquire(blocking=False):raise AppError('recovery_busy','已有恢复正在执行',409)
        try:return self._recover()
        finally:self.recovery_lock.release()

    def _recover(self):
        with self.operation_lock:
            if self.stopping.is_set() or self.closed:raise AppError('native_paused','后台已停止',409)
            self.maintenance.set()
            try:self.dispatch.bridge.set_control(live=False,clock_ok=False)
            except Exception:self.dispatch.native.close_control()
            futures=list(self.batches.values())+list(self.preflights)+list(self.rechecks.values())
        try:
            if futures and wait(futures,timeout=5).not_done:raise AppError('native_results_busy','结果尚未收齐，请稍后恢复',409)
            if not self.discovery_lock.acquire(timeout=5):raise AppError('discovery_busy','计划读取尚未结束，保持暂停',409)
            try:
                # A refresh already in flight can enqueue read-only work after
                # the first snapshot; the discovery lock closes that race.
                if self.rechecks and wait(list(self.rechecks.values()),timeout=5).not_done:
                    raise AppError('receipt_checks_busy','回执核对尚未结束，保持暂停',409)
                with self.operation_lock:
                    if self.stopping.is_set() or self.closed:raise AppError('native_paused','后台已停止',409)
                    count=self.dispatch.recover()
                    if self.stopping.is_set() or self.closed:raise AppError('native_paused','后台正在停止，保持暂停',409)
                    with self._lock:self.jobs.clear()
                    self.batches.clear();self.preflights.clear();self.admitted.clear();self.rechecks.clear();self.accounts.busy.clear()
                    self.recovery_error=None;self.last_error=None
                    self.anchor_wall,self.anchor_mono=self.wall_clock(),self.monotonic()
                    self.dispatch.anchor_wall,self.dispatch.anchor_ns=self.wall_clock(),clock_ns()
                    self.clock_ok=True;self.maintenance.clear()
                    return count
            finally:self.discovery_lock.release()
        except Exception:
            self.recovery_error='恢复未完成，保持暂停'
            self.dispatch.bridge.outbox.failed=True
            raise

    def status(self):
        result=super().status()
        result.update(execution_scope='native_school',capacity=len(self.dispatch.native.workers),
                      active_batches=sum(not future.done() for future in self.batches.values()),
                      recovery_required=self.maintenance.is_set() or bool(self.recovery_error) or self.dispatch.bridge.outbox.failed,native_variant='wal-raw-t',completed_batches=self.completed_batches)
        warning=self.capacity_error or self.recovery_error or result['scheduler_warning']
        result['scheduler_warning']=warning
        result['ready_to_submit']=bool(result['ready_to_submit'] and not warning and not self.maintenance.is_set()
                                       and not self.dispatch.bridge.outbox.failed)
        return result

    def rebind(self,data):
        required={'run_key','expected_revision','epoch','session','expected_global_version'}
        if set(data)!=required or not isinstance(data['run_key'],str):raise AppError('invalid_rebind','请重新读取任务后确认')
        with self.discovery_lock,self.operation_lock:
            if self.maintenance.is_set() or self.stopping.is_set() or not self.reservations.allow_live:raise AppError('native_paused','请先显式启用执行',409)
            native=self.dispatch.native;state=self.dispatch.bridge.outbox.applied('global')
            if any(type(data[key]) is not int for key in required-{'run_key'}) or (data['epoch'],data['session'],data['expected_global_version'])!=(native.epoch,native.session,state['version']):
                raise AppError('conflict','执行会话已变化，请刷新后确认',409)
            row=self.store.get_run(data['run_key'])
            if not row or not (row.get('native_binding') or {}).get('rebind_required') or row['spec']['revision']!=data['expected_revision']:
                raise AppError('conflict','任务不再等待重新绑定',409)
            spec=dict(row['spec']);spec['seat_ids']=tuple(spec['seat_ids']);run=RunSpec(**spec)
            prepared=self.dispatch.prepare([run],allow_rebind=True)
            completion=Future();ready=Future();ready.set_result(True)
            with self._lock:self.jobs[(run.key,run.revision)]={'run':run,'prep':ready,'warm':None,'submit':completion}
            self.accounts.busy.add(run.account_id);self.batches[uuid.uuid4().hex]=completion
            try:self.collect_pool.submit(self._collect,[run],prepared,completion)
            except Exception as exc:
                self.dispatch.bridge.outbox.failed=True;self.dispatch.native.close_control()
                self._finish_batch([run],completion,error=AppError('native_arm_uncertain','结果收集未确认',503));raise
            return {'accepted':True,'run_key':run.key}

    def stop(self):
        if self.closed:return
        self.stopping.set();self.maintenance.set()
        self.dispatch.close()
        super().stop()
        self.arm_pool.shutdown(wait=True,cancel_futures=False)
        self.collect_pool.shutdown(wait=True,cancel_futures=False)
        self.receipt_pool.shutdown(wait=True,cancel_futures=False)
        self.preflights.clear();self.rechecks.clear()


class ControlHandle:
    def __init__(self,scheduler):self.scheduler=scheduler
    @property
    def stop(self):return self.scheduler.dispatch.stop
    def clock_ok(self):
        return (self.scheduler.dispatch.clock_ok() and not self.scheduler.maintenance.is_set()
                and not self.scheduler.stopping.is_set() and not self.scheduler.closed
                and not self.scheduler.dispatch.closed and not self.scheduler.dispatch.closing)
    def recover(self):return self.scheduler.recover()
