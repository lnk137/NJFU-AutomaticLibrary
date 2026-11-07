"""School-compatible native first send; business confirmation stays in Python."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading

from .protocol import clock_ns
from .request import capture_request,decode_result
from .continuation import NativeFirstContinuation,NativeFirstProof
from local_app.reservations import ReservationService
from .dispatch_base import NativeDispatch
from .state import prepare_state
from local_app.models import AppError
from local_app.limits import PARALLEL_ACCOUNT_LIMIT
import time
import uuid



class WarmAccounts:
    def __init__(self,raw):self.raw=raw
    def __getattr__(self,name):return getattr(self.raw,name)
    def client(self,account_id,**kwargs):
        row=self.raw.store.get_account(account_id)
        cached=self.raw._clients.get(account_id)
        if (not cached or cached[0]!=row['credential_revision'] or not 0<=time.monotonic()-cached[1]<=900
                or cached[2].identity is None or cached[2].identity.student_id!=row['student_id']):
            raise AppError('native_session_not_prepared','认证会话未就绪，不在临近放号时隐式重新登录',409)
        return cached[2]
    def ready(self,account_id):
        try:self.client(account_id);return True
        except AppError:return False


class GovernedReservations(ReservationService):
    def __init__(self,accounts,store,active,**kwargs):
        self._active=active;super().__init__(accounts,store,True,**kwargs)
    @property
    def allow_live(self):return self._configured and self._active()
    @allow_live.setter
    def allow_live(self,value):self._configured=value


class CampusDispatch(NativeDispatch):
    def __init__(self,bridge,accounts,store,**kwargs):
        self._lease_deadline_ns=0;self._outcomes={};self._outcome_lock=threading.Lock()
        self._result_registration=threading.RLock()
        self._result_pool=ThreadPoolExecutor(max_workers=PARALLEL_ACCOUNT_LIMIT,thread_name_prefix='campus-result')
        self.raw_accounts=accounts;self.recovery_classifier=None;self.warmed={};self._initial_streams=True
        super().__init__(bridge,WarmAccounts(accounts),store,**kwargs)
        self.service=GovernedReservations(accounts,store,self.global_allowed,wall_clock=self.wall_clock)
        self.receipts=self.service

    def _reset_streams(self):
        if self._initial_streams:
            # NativeProcess already completed a fresh control and all worker
            # handshakes. A failed initial readback is kept gated for recovery.
            self._initial_streams=False;return
        if getattr(self,'_native_restarted',False):
            self._native_restarted=False;return
        super()._reset_streams()

    def recover(self):
        self.warmed.clear()
        # Recovery runs only after revocation and owned-future drainage. Reopen
        # the same durable store even for a live channel failure so a poisoned
        # native process cannot be mistaken for a healthy new control session.
        with self.bridge.outbox.lock:
            initialize,identity=prepare_state(self.native.runtime.parent,self.bridge.raw)
            if initialize or identity!=self.native.identity:
                raise AppError('native_state_invalid','执行记录来源变化，保持暂停；未重建账本',503)
            self.native.close();self.native.restart();self._native_restarted=True
        return super().recover()

    def recover_runs(self):
        # Reuse NativeDispatch's paused transport/lifecycle recovery, but classify
        # every scheduled row, including cross-ledger writes interrupted before
        # Mongo acquired its binding. Never ARM or read a result stream here.
        with self.lifecycle_lock,self.bridge.outbox.lock:
            state=self.bridge.outbox.applied('global')
            if not state or state['enabled'] or state['plan'] or self.recovery_classifier is None:
                raise AppError('native_recovery_gate','恢复须先确认全局暂停',409)
            try:return self.recovery_classifier()
            except Exception:
                self.bridge.outbox.failed=True;self.native.close_control();raise
    def global_allowed(self):
        # Publication is single-owner and ACKed. These immutable snapshots and
        # operation flags are read without SQL/locks at the final HTTP boundary.
        state=self.native.applied_states.get('global') or {}
        return bool(not self.closed and not self.closing and not self.stop.is_set()
            and not self.bridge.outbox.failed and not self.bridge.outbox.active_operations
            and not self.clock_fault.is_set()
            and state.get('enabled') and state.get('plan'))

    def send_ready(self):
        return self.global_allowed() and self.clock_ok() and clock_ns()<self._lease_deadline_ns

    def _lease_ack(self,deadline_ns):self._lease_deadline_ns=deadline_ns

    def warm_transport(self,run,budget_ms):
        with self.lifecycle_lock:
            if self.closed or self.closing or self.stop.is_set():raise AppError('native_paused','执行器正在关闭',409)
            old=self.warmed.get(run.key)
            generation=(self.native.epoch,self.native.session)
            if (old and old['revision']==run.revision and old['generation']==generation
                    and 0<=clock_ns()-old.get('warmed_ns',0)<=5_000_000_000):return old
            if old and old.get('warming'):raise AppError('native_warm_busy','此账号连接正在预热',409)
            occupied=set(self.reservations)|{value['worker'] for key,value in self.warmed.items() if key!=run.key}
            idle=[index for index in range(len(self.native.workers)) if index not in occupied]
            if not idle:raise AppError('native_capacity','连接通道正在使用，等待本轮释放',409)
            entry={'worker':idle[0],'revision':run.revision,'generation':generation,'warming':True,'warmed_ns':0}
            self.warmed[run.key]=entry
        try:result=self.native.workers[entry['worker']].warm(budget_ms)
        except Exception:
            with self.lifecycle_lock:
                if self.warmed.get(run.key) is entry:self.warmed.pop(run.key,None)
            raise AppError('network_error','该账号执行连接暂未预热完成',503) from None
        with self.lifecycle_lock:
            if (self.warmed.get(run.key) is not entry or generation!=(self.native.epoch,self.native.session)
                    or self.closed or self.closing):raise AppError('native_state_changed','执行会话已变化',409)
            entry.update(warming=False,warmed_ns=result['warmed_ns'])
            return entry

    def reap_warm(self,active):
        with self.lifecycle_lock:
            for key,entry in list(self.warmed.items()):
                if (key,entry['revision']) not in active and not entry['warming'] and entry['worker'] not in self.reservations:
                    self.warmed.pop(key,None)

    def prepare(self,runs,*,allow_rebind=False):
        runs=list(runs);started=clock_ns()
        with self.lifecycle_lock:
            if self.closed or self.closing or self.stop.is_set():raise AppError('native_paused','执行器已停止',409)
            entries=[self.warmed.get(run.key) for run in runs]
            if any(not entry or entry['warming'] or entry['revision']!=run.revision
                   or entry['generation']!=(self.native.epoch,self.native.session)
                   or not 0<=clock_ns()-entry['warmed_ns']<=5_000_000_000
                   for run,entry in zip(runs,entries,strict=True)):
                raise AppError('native_transport_not_ready','执行连接未就绪，未登记预约',409)
            slots=[entry['worker'] for entry in entries]
            if len(set(slots))!=len(slots) or any(index in self.reservations for index in slots):
                raise AppError('native_workers_busy','执行连接仍被其他任务持有',409)
            token=uuid.uuid4().hex
            for index in slots:self.reservations[index]={'token':token,'job':None,'ready':False,'collecting':False}
            try:prepared=self._prepare(runs,slots,token,allow_rebind=allow_rebind)
            except Exception as exc:
                if not isinstance(exc,AppError) or exc.code!='native_arm_uncertain':
                    for index in slots:self.reservations.pop(index,None)
                raise
            acked=clock_ns()
            for item,entry in zip(prepared,entries,strict=True):
                item['schedule_trace']={key:item['job'][key] for key in ('due_ns','created_ns','expires_ns','account_version','global_version','credential')}
                item['schedule_trace'].update(arm_ack_ns=acked,prepare_started_ns=started,batch_size=len(prepared),
                    transport_warmed_ns=entry['warmed_ns'],body_sha256=hashlib.sha256(item['job']['body']).hexdigest())
                if item.get('pilot_clock_pair'):
                    item['schedule_trace'].update(item['pilot_clock_pair'].diagnostics())
                if item.get('pilot_mode'):
                    item['schedule_trace']['pilot_mode']=item['pilot_mode']
            return prepared

    def payload_fields(self,run,key,created,due):
        client=self.accounts.client(run.account_id)
        packet=capture_request(client,run,run.seat_ids[0])
        return {'body':packet.encode(),'created_ns':packet.created_ns,
                'expires_ns':min(packet.expires_ns,due+1_000_000_000),'student_id':client.identity.student_id}

    def binding_fields(self,run,job):
        return {'student_id':job['student_id'],
                'credential_revision':job['credential'],'packet_sha256':hashlib.sha256(job['body']).hexdigest()}

    def transport_outcome(self,item,result):
        job,run=item['job'],item['run']
        try:fields=self.native.job_status(job['id']).split()
        except Exception:fields=[]  # retain the received transport evidence
        same_job=(len(fields)==6 and fields[:4]==['JOB',job['id'],str(self.native.epoch),str(self.native.session)])
        proven=same_job and fields[4]=='1' and fields[5] in {'done','uncertain','denied'}
        denied=(same_job and fields[4] in {'0','1'} and fields[5]=='denied'
                and result['journal_ok']==1 and result['dispatch_ns']==0)
        start=result.get('metrics',{}).get('curl_start_ns') or result['dispatch_ns']
        pair=item.get('pilot_clock_pair')
        wall_start=(pair.wall_at(start) if pair else self.anchor_wall+(start-self.anchor_ns)/1e9) if start else self.wall_clock()
        wall_end=pair.wall_at(result['network_done_ns']) if pair else self.anchor_wall+(result['network_done_ns']-self.anchor_ns)/1e9
        timings={'timing_version':3,'request_started_at':wall_start,
            'request_finished_at':wall_end,
            'request_ms':max(0,result['network_done_ns']-start)/1e6 if start else 0,
            'guard_ms':result.get('metrics',{}).get('guard_ns',0)/1e6}
        first=decode_result(result['http_status'],result['body'].encode(),timings,
            sent=False if denied else True if result['dispatch_ns'] else None,
            journal_ok=(proven or denied) and bool(result['journal_ok']),transport_ok=result['curl_code']==0)
        proof=NativeFirstProof(job['id'],self.native.epoch,self.native.session,
            job['student_id'],job['credential'],
            hashlib.sha256(job['body']).hexdigest(),result['dispatch_ns'],result['network_done_ns'])
        with self._outcome_lock:self._outcomes[job['id']]=(proof,first)
        return ('skipped','原生最终校验阻断，未发送') if denied else ('uncertain','首发请求已结束，等待有效预约记录核实')

    def _collect_one(self,item):
        result=NativeDispatch.collect(self,[item])[0]
        run=item['run'];job=item['job']
        with self.lifecycle_lock:self.warmed.pop(run.key,None)
        with self._outcome_lock:outcome=self._outcomes.pop(job['id'],None)
        trace=item.get('schedule_trace',{})
        try:
            values={'native_schedule':trace,'execution_source':'native_campus_first'}
            if result['status']=='uncertain':values.update(
                attempted_seat_id=run.seat_ids[0],submitted_at=run.due_at,reconcile_count=0)
            self.store.runs.update_one({'_id':run.key,'spec.revision':run.revision},{'$set':values})
            if outcome and result['result_persisted'] and result['status']=='uncertain':
                proof,first=outcome
                continuation=NativeFirstContinuation(self.service,self.native,ready=self.send_ready)
                verified=continuation.apply(run,proof,first);result['status']=verified.status
                if continuation.persist_failed:self.bridge.outbox.failed=True
        except Exception:
            self.bridge.outbox.failed=True;result['status']='uncertain'
        result['schedule_trace']=trace
        return result

    def collect(self,prepared):
        with self._result_registration:
            # Closing fences sends first. A collector already registered by ARM
            # may arrive after pool shutdown; it must still drain/persist once.
            if self.closed:return [self._collect_one(item) for item in prepared]
            futures=[self._result_pool.submit(self._collect_one,item) for item in prepared]
        return [future.result() for future in futures]

    def close(self):
        super().close()
        with self._result_registration:self._result_pool.shutdown(wait=True,cancel_futures=False)
