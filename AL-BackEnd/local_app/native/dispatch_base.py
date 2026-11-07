"""Explicit scheduled-run adapter; no Python HTTP submit or automatic replay."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import math
import threading
import time
import uuid

from local_app.date_exceptions import validate_date_exceptions
from local_app.models import AppError, ZONE, resolve_run, validate_plan
from local_app.scheduler import cohort_handled
from .protocol import clock_ns, Worker
from .outbox import SyncPending
from .clock_pair import MAX_PAIR_WIDTH_NS, sample_clock_pair, wall_deadline_ns
from .trusted_clock import validate_evidence


class NativeDispatch:
    def __init__(self, bridge, accounts, store, *, wall_clock=time.time):
        self.bridge, self.accounts, self.store = bridge, accounts, store
        self.native = bridge.native
        self.lifecycle_lock = threading.RLock()
        self.heartbeat_lock = threading.RLock()
        self.reservations = {}
        self.closed = False
        self.closing = False
        self.transfer_lock = self.native.__dict__.setdefault('dispatch_transfer_lock',threading.RLock())
        self.wall_clock = wall_clock
        self.anchor_wall, self.anchor_ns = wall_clock(), clock_ns()
        self.stop = threading.Event()
        # Latch a transient clock fault until the recovery path has verified
        # time and restored the durable global gate. A single bad observation
        # can disappear before the separate recovery monitor samples it.
        self.clock_fault = threading.Event()
        with self.transfer_lock:
            previous=getattr(self.native,'dispatch_owner',None)
            if previous:
                with previous.lifecycle_lock:
                    if not previous.closed or any(slot['collecting'] for slot in previous.reservations.values()):
                        raise AppError('native_workers_busy','原执行器仍持有任务或结果通道',409)
                    previous.reservations.clear()
            self._reset_streams()
            self.native.dispatch_owner=self
        self.heartbeat = threading.Thread(target=self._renew, name='isolated-native-lease', daemon=True)
        self.heartbeat.start()

    def _reset_streams(self):
        """New control session fences old jobs before replacing result streams."""
        with self.heartbeat_lock, self.native.lock:
            self.native.reconnect()
            old=self.native.workers
            self.native.workers=[]
            for worker in old:worker.close()
            with ThreadPoolExecutor(max_workers=len(old)) as pool:
                self.native.workers=list(pool.map(lambda index:Worker(self.native.path,index),range(len(old))))

    def clock_ok(self):
        return abs((self.wall_clock()-self.anchor_wall)-(clock_ns()-self.anchor_ns)/1e9) < .25

    def pilot_selected(self, run):
        matcher = getattr(self, 'pilot_matcher', None)
        return bool(matcher and matcher(run))

    def _renew(self):
        while not self.stop.is_set():
            try:
                if not self.bridge.outbox.failed:
                    if self.clock_ok():
                        with self.heartbeat_lock:
                            # Failure may be reported while this heartbeat waits for the lock.
                            if not self.bridge.outbox.failed and not self.stop.is_set() and not self.clock_fault.is_set():
                                before=clock_ns();self.native.lease(500);self._lease_ack(before+500_000_000)
                    else:
                        with self.bridge.outbox.lock:
                            if self.stop.is_set() or self.bridge.outbox.failed:continue
                            self.clock_fault.set()
                            state=self.bridge.outbox.applied('global')
                            if state and (state['enabled'] or state['plan']):
                                self.bridge.set_control(live=False,clock_ok=False)
            except Exception:
                self.bridge.outbox.failed = True
                self.native.close_control()
            self.stop.wait(.05)

    @staticmethod
    def payload(run, job):
        # Synthetic fake-school body only. No campus encryption/session contract.
        return json.dumps({'job':job,'account':run.account_id,'day':run.target_date,
                           'begin':run.begin,'end':run.end,'seat':run.seat_ids[0],
                           'revision':run.revision},sort_keys=True).encode()

    def payload_fields(self,run,key,created,due):
        return {'body':self.payload(run,key),'created_ns':created,'expires_ns':due+1_000_000_000}

    def binding_fields(self,run,job):return {}
    def _lease_ack(self,deadline_ns):pass

    def transport_outcome(self,item,result):
        state='uncertain' if result['dispatch_ns'] or not result['journal_ok'] else 'skipped'
        return state,('隔离假服务请求已结束；不代表学校预约成功' if state=='uncertain' else '本地最终检查阻断，未自动重交')

    def prepare(self, runs, *, allow_rebind=False):
        runs = list(runs)
        with self.lifecycle_lock:
            if self.closed or self.closing or self.stop.is_set() or self.native.dispatch_owner is not self:
                raise AppError('native_paused','执行器已停止',409)
            idle=[index for index in range(len(self.native.workers)) if index not in self.reservations]
            if len(runs)>len(idle):
                raise AppError('native_workers_busy','执行器通道仍被任务或未读取结果占用',409)
            slots=idle[:len(runs)];token=uuid.uuid4().hex
            for index in slots:self.reservations[index]={'token':token,'job':None,'ready':False,'collecting':False}
            try:
                return self._prepare(runs,slots,token,allow_rebind=allow_rebind)
            except Exception as exc:
                if not isinstance(exc,AppError) or exc.code!='native_arm_uncertain':
                    for index in slots:self.reservations.pop(index,None)
                raise

    def _prepare(self, runs, slots, token, *, allow_rebind):
        from local_app.dual_node.fence import require_legacy
        for run in runs:require_legacy(self.accounts,run)
        if any(run.operation_id for run in runs):
            raise AppError('native_segmented_unsupported','多时段计划须由受控队列执行，禁止登记原生单段任务',409)
        if not runs or len(runs)>len(self.native.workers) or len({run.account_id for run in runs}) != len(runs):
            raise AppError('invalid_native_batch','每批每账号仅一个任务，且不能超过执行器容量')
        if len({run.due_at for run in runs}) != 1:
            raise AppError('invalid_native_batch','一批任务须具有同一放号时刻')
        pilot_modes = {self.pilot_selected(run) for run in runs}
        if len(pilot_modes) != 1:
            raise AppError('invalid_native_batch','试点任务必须单独登记',409)
        pilot = pilot_modes.pop()
        if pilot and not getattr(self.native, 'pilot_wall_supported', False):
            raise AppError('pilot_capability_missing','执行器不支持试点墙钟保护，未登记预约',503)
        with ExitStack() as stack:
            for account in sorted(run.account_id for run in runs):
                stack.enter_context(self.accounts.account_lock(account))
            stack.enter_context(self.bridge.outbox.lock)
            self.bridge.outbox.require_ready()
            master = self.bridge.outbox.applied('global')
            if self.stop.is_set() or not self.clock_ok() or not master or not master['enabled'] or not master['plan']:
                raise AppError('native_paused','执行总开关或时钟未通过，请先检查',409)
            bindings=[]
            for run in runs:
                if cohort_handled(self.store,run,segmented_history_only=True):
                    raise AppError('native_cohort_handled','该使用日期已有多时段执行或停止记录，禁止重新登记',409)
                row = self.store.get_account(run.account_id)
                state = self.bridge.outbox.applied(run.account_id)
                desired, projection = self.bridge.project(run.account_id)
                if pilot:
                    guard = getattr(self, 'pilot_guard', None)
                    try:
                        bound = guard is not None and guard(run, row, state) is True
                    except Exception:
                        bound = False
                    if not bound:
                        raise AppError('pilot_identity_changed',
                                       '试点账号或计划版本不匹配，未登记预约', 409)
                if projection != self.bridge.outbox.projection(run.account_id) or not state or not state['enabled'] or not state['plan']:
                    raise AppError('native_state_changed','权限状态尚未同步或已变化',409)
                plan = validate_plan(row.get('plan'), self.accounts.catalog)
                exceptions = validate_date_exceptions(row.get('date_exceptions',{}), self.accounts.catalog)
                expected = resolve_run(run.account_id,row['revision'],plan,datetime.fromtimestamp(run.due_at,ZONE).date(),date_exceptions=exceptions)
                if run != expected or run.kind != 'scheduled' or not run.seat_ids or not desired['enabled']:
                    raise AppError('native_run_changed','任务与当前有效日期、时间或座位配置不一致',409)
                identity = self.accounts.client(run.account_id,force=True).identity
                if identity is None or identity.student_id != row['student_id']:
                    raise AppError('identity_mismatch','学校身份不一致',502)
                key = 'r'+hashlib.sha256(run.key.encode()).hexdigest()
                journal = self.native.job_status(key)
                if journal != 'NOTFOUND':
                    fields = journal.split()
                    if not allow_rebind or len(fields)!=6 or fields[4]!='0' or (int(fields[2]),int(fields[3]))==(self.native.epoch,self.native.session):
                        raise AppError('native_already_staged','任务已登记或结果不确定，禁止自动重交',409)
                existing = self.store.ensure_run(run)
                if existing['status'] not in {'queued','preparing'}:
                    raise AppError('native_already_handled','任务已处理或待核实，禁止重交',409)
                bindings.append((run,state,key))
            pair = None
            if pilot:
                sampler = getattr(self, 'pilot_sampler', sample_clock_pair)
                pair = sampler(self.wall_clock, clock_ns, deadline_wall=runs[0].due_at)
                if pair.width_ns > MAX_PAIR_WIDTH_NS:
                    raise AppError('pilot_clock_sample_invalid','试点时间采样超限，未登记预约',503)
                pair.require_consistent(self.anchor_wall, self.anchor_ns)
                observed_wall, created = pair.wall, pair.after_ns
            else:
                observed_wall = self.wall_clock()
                created = clock_ns()
            remaining = runs[0].due_at-observed_wall
            if not .05 <= remaining <= 1.8:
                raise AppError('native_prepare_window','请在放号前 0.05–1.8 秒内显式准备',409)
            # Sampling the continuous clock after wall time makes this mapping
            # conservative: call latency cannot advance a wall-clock deadline.
            due = pair.deadline_ns(runs[0].due_at) if pair else created+math.ceil(remaining*1e9)
            pilot_mode = None
            if pilot:
                evidence = getattr(self, 'pilot_evidence', None)
                try:
                    valid = (getattr(self, 'pilot_evidence_generation', None) ==
                             (self.native.identity, self.native.epoch, self.native.session)
                             and validate_evidence(evidence, pair,
                             boot_clock=getattr(self, 'pilot_boot_clock', None))
                             and type(evidence.get('expires_ns')) is int
                             and evidence['expires_ns'] > due + 1_000_000_000)
                except Exception:
                    valid = False
                if not valid:
                    # User-selected fallback: before any ARM or payload capture,
                    # use the unchanged native timing path if school proof is
                    # unavailable. Existing clock, authorization and revision
                    # gates still apply and no prior send can be replayed here.
                    if not self.clock_ok():
                        raise AppError('native_clock_changed','本机时钟变化，未登记预约',503)
                    observed_wall = self.wall_clock()
                    created = clock_ns()
                    remaining = runs[0].due_at-observed_wall
                    if not .05 <= remaining <= 1.8:
                        raise AppError('native_prepare_window','请在放号前 0.05–1.8 秒内显式准备',409)
                    due = created+math.ceil(remaining*1e9)
                    pair = None
                    pilot_mode = 'original_no_clock_proof'
                else:
                    pilot_mode = 'paired_wall_guard'
            prepared=[]
            for index,(run,state,key) in enumerate(bindings):
                worker=slots[index]
                job={'id':key,'worker':worker,'account':run.account_id,'day':run.target_date,
                     'kind':'S','operation':'-','account_version':state['version'],'global_version':master['version'],
                     'credential':state['credential'],'due_ns':due,'expires_ns':due+1_000_000_000,
                     'created_ns':created}
                if pilot and pair:job['pilot_wall_due_ns']=wall_deadline_ns(run.due_at)
                job.update(self.payload_fields(run,key,created,due))
                binding={'job':key,'epoch':self.native.epoch,'session':self.native.session,'worker':worker,'rebind_required':False}
                binding.update(self.binding_fields(run,job))
                updated=self.store.runs.update_one({'_id':run.key,'spec.revision':run.revision,'status':{'$in':['queued','preparing']}},
                    {'$set':{'native_binding':binding,'status':'preparing','message':'隔离原生执行器正在准备'}})
                if updated.matched_count!=1:
                    raise AppError('native_run_changed','任务状态已变化',409)
                self.reservations[worker]['job']=key
                item={'run':run,'job':job,'reservation':token}
                if pair:item['pilot_clock_pair']=pair
                if pilot_mode:item['pilot_mode']=pilot_mode
                prepared.append(item)
            if clock_ns()+20_000_000 > due:
                raise AppError('native_prepare_window','准备时间不足，尚未登记发送任务',409)
            try:
                for run in runs:require_legacy(self.accounts,run)
                before=clock_ns();self.native.lease(500);self._lease_ack(before+500_000_000)
                self.native.arm([item['job'] for item in prepared], 'D')
                for item in prepared:
                    self.native.workers[item['job']['worker']].arm_wait(item['job'])
            except Exception as exc:
                self.bridge.outbox.failed=True
                for item in prepared:
                    self.store.transition_run(item['run'].key,('preparing',),'uncertain',
                        {'message':'原生登记确认中断，禁止自动重交'},revision=item['run'].revision)
                raise AppError('native_arm_uncertain','原生登记状态待核实，未回退到 Python 提交',503) from exc
            for worker in slots:self.reservations[worker]['ready']=True
            return prepared

    def collect(self, prepared):
        prepared=list(prepared)
        with self.lifecycle_lock:
            if len({item['job']['worker'] for item in prepared})!=len(prepared):
                raise AppError('native_result_owner','结果通道重复',409)
            for item in prepared:
                slot=self.reservations.get(item['job']['worker'])
                if not slot or slot['token']!=item.get('reservation') or slot['job']!=item['job']['id'] or not slot['ready'] or slot['collecting']:
                    raise AppError('native_result_owner','结果已被读取、归属变化或正在读取',409)
            for item in prepared:self.reservations[item['job']['worker']]['collecting']=True
        results=[]
        for item in prepared:
            run,job=item['run'],item['job']
            stream_complete=False
            try:
                result=self.native.workers[job['worker']].result()
                stream_complete=True
                status,message=self.transport_outcome(item,result)
                result.pop('body',None)
                if not result['journal_ok']:self.bridge.outbox.failed=True
            except Exception:
                result={};status='uncertain';message='原生执行中断，禁止自动重交，请核对持久账本'
                self.bridge.outbox.failed=True
                self.native.close_control()
            persisted=False
            try:
                self.store.runs.update_one({'_id':run.key,'spec.revision':run.revision}, {'$set':{'native_result':result}})
                persisted=self.store.transition_run(run.key,('queued','preparing','submitting'),'uncertain' if status=='uncertain' else 'skipped',
                                                    {'message':message},revision=run.revision)
                if not persisted:self.bridge.outbox.failed=True
            except Exception:
                self.bridge.outbox.failed=True
                status='uncertain'
            finally:
                with self.lifecycle_lock:
                    if stream_complete:self.reservations.pop(job['worker'],None)
                    else:self.reservations[job['worker']].update(collecting=False,ready=False)
            results.append({'run_key':run.key,'status':status,'native':result,'result_persisted':persisted})
        return results

    def recover(self):
        with self.lifecycle_lock, self.bridge.outbox.lock:
            if self.closed or self.closing or self.native.dispatch_owner is not self:
                raise AppError('native_paused','旧控制器不能恢复新执行会话',409)
            if any(slot['collecting'] for slot in self.reservations.values()):
                raise AppError('native_results_busy','结果正在读取，请完成后恢复',409)
            self.bridge.outbox.failed=True
            try:
                self._reset_streams()
                self.bridge.recover()
                self.reservations.clear()
                return self.recover_runs()
            except Exception as exc:
                self.bridge.outbox.failed=True
                self.native.close_control()
                if isinstance(exc,AppError) and exc.code=='native_state_invalid':raise
                raise SyncPending() from exc

    def recover_runs(self):
        """Read native claims into Mongo. Never call ARM or WAIT here."""
        with self.lifecycle_lock, self.bridge.outbox.lock:
            master=self.bridge.outbox.applied('global')
            if not master or master['enabled'] or master['plan']:
                raise AppError('native_recovery_gate','恢复须先确认全局暂停',409)
            try:
                rows=list(self.store.runs.find({'native_binding':{'$exists':True},'status':{'$in':['queued','preparing','submitting']}}))
                for row in rows:
                    binding=row['native_binding'];fields=self.native.job_status(binding['job']).split()
                    recorded=(binding['epoch'],binding['session'])
                    current=(self.native.epoch,self.native.session)
                    unclaimed=len(fields)==6 and fields[4]=='0' and (int(fields[2]),int(fields[3]))==recorded and recorded!=current
                    self.store.runs.update_one({'_id':row['_id'],'status':row['status'],'native_binding':binding},
                        {'$set':{'status':'queued' if unclaimed else 'uncertain',
                                 'native_binding.rebind_required':unclaimed,
                                 'message':'尚未领取，须显式重新绑定' if unclaimed else '跨账本恢复待核实，禁止自动重交'}})
                return len(rows)
            except Exception as exc:
                self.bridge.outbox.failed=True
                self.native.close_control()
                raise SyncPending() from exc

    def close(self):
        with self.transfer_lock:
            with self.lifecycle_lock:
                if self.closed:return
                self.closing=True
            self.stop.set()
            if threading.current_thread() is not self.heartbeat:
                self.heartbeat.join(timeout=2)
                if self.heartbeat.is_alive():
                    self.native.close_control()
                    self.heartbeat.join(timeout=2)
                    if self.heartbeat.is_alive():raise RuntimeError('lease owner did not stop; handoff refused')
            if self.native.dispatch_owner is self and self.bridge.outbox.db is not None:
                try:
                    self.bridge.set_control(live=False,clock_ok=False)
                except Exception:
                    # An unavailable ledger cannot authorize a clean shutdown ACK.
                    self.native.close_control()
            with self.lifecycle_lock:
                self.closed=True
                self.closing=False
