"""One durable owner continues only observed seat-specific rejections."""
from dataclasses import asdict,dataclass
import re
import uuid

from local_app.models import RunResult,Submission
from local_app.recovery_policy import remaining_candidate_attempts


@dataclass(frozen=True)
class NativeFirstProof:
    job: str
    epoch: int
    session: int
    student_id: str
    credential_revision: int
    packet_sha256: str
    started_ns: int
    finished_ns: int


class NativeFirstContinuation:
    def __init__(self,reservations,native,*,ready=lambda:True):
        self.service,self.native,self.ready=reservations,native,ready
        self.accounts,self.store=reservations.accounts,reservations.store
        self.persist_failed=False

    @staticmethod
    def existing(row):
        if not row:return RunResult('uncertain','原生执行记录不存在，禁止重交',[])
        result=row.get('result') or {}
        return RunResult(row['status'],result.get('message',row.get('message','已处理，未重复提交')),
                         result.get('records',[]),result.get('verified_at'))

    def same_generation(self,proof):
        return (proof.epoch,proof.session)==(self.native.epoch,self.native.session)

    def trusted(self,run,proof,first):
        if (not isinstance(proof,NativeFirstProof) or not isinstance(first,Submission)
                or run.kind!='scheduled' or not run.seat_ids or not self.same_generation(proof)
                or not re.fullmatch('[0-9a-f]{64}',proof.packet_sha256)
                or not 0<=proof.started_ns<=proof.finished_ns or proof.finished_ns==0):return False
        try:fields=self.native.job_status(proof.job).split()
        except Exception:return False
        return fields==['JOB',proof.job,str(proof.epoch),str(proof.session),'1','done'] or fields==[
            'JOB',proof.job,str(proof.epoch),str(proof.session),'1','uncertain']

    def apply(self,run,proof,first):
        service=self.service
        if not self.trusted(run,proof,first):return RunResult('uncertain','原生领取身份未确认，禁止重交',[])
        with self.accounts.account_lock(run.account_id):
            owner=uuid.uuid4().hex
            query={'_id':run.key,'spec':asdict(run),'status':'uncertain',
                'native_binding.job':proof.job,'native_binding.epoch':proof.epoch,
                'native_binding.session':proof.session,'native_binding.student_id':proof.student_id,
                'native_binding.packet_sha256':proof.packet_sha256,
                'native_result.journal_ok':1,'native_result.dispatch_ns':proof.started_ns,
                'native_result.network_done_ns':proof.finished_ns,
                'native_continuation':{'$exists':False}}
            try:
                claimed=self.store.runs.update_one(query,{'$set':{'native_continuation':{
                    'owner':owner,'state':'processing','job':proof.job,'epoch':proof.epoch,'session':proof.session}}})
            except Exception:
                self.persist_failed=True
                return RunResult('uncertain','首发结果未取得持久处理状态，禁止重交',[])
            if claimed.matched_count!=1:return self.existing(self.store.get_run(run.key))
            result=None
            try:
                result=self._continue(run,proof,first)
                saved=self.store.get_run(run.key)
                if not saved or saved['status']!=result.status:
                    self.persist_failed=True
                    result=RunResult('uncertain','结果尚未确认落库，禁止重交',[])
            except Exception:
                self.persist_failed=True
                result=RunResult('uncertain','首发结果处理中断，禁止自动重交',[])
                try:service._finish(run.key,result,run.revision)
                except Exception:pass
            finally:
                try:
                    changed=self.store.runs.update_one({'_id':run.key,'native_continuation.owner':owner},
                        {'$set':{'native_continuation.state':'uncertain' if self.persist_failed else 'done'}})
                    if changed.matched_count!=1:self.persist_failed=True
                except Exception:self.persist_failed=True
            return result

    def _continue(self,run,proof,first):
        service=self.service
        attempts=[asdict(first)|{'seat_id':run.seat_ids[0]}]
        if not self.store.transition_run(run.key,('uncertain',),'submitting',{
            'attempts':attempts,'attempted_seat_id':run.seat_ids[0],
            'submitted_at':first.timings.get('request_started_at',run.due_at) if first.timings else run.due_at,
            'reconcile_count':0},revision=run.revision):
            self.persist_failed=True
            return RunResult('uncertain','首发回复未确认保存，禁止重交',[])
        if first.outcome=='accepted':return service.reconcile(run.key)
        if first.outcome=='uncertain':return service._finish(run.key,RunResult('uncertain','首发结果待学校记录确认，禁止重交',[]),run.revision)
        candidates=remaining_candidate_attempts(run.seat_ids,attempts) if first.retryable else ()
        if not candidates:
            return service._finish(run.key,RunResult('failed',first.message or '学校拒绝首选预约，未继续提交',[]),run.revision)
        deadline=min(run.due_at+30,run.expires_at) if run.expires_at is not None else run.due_at+30
        try:client=self.accounts.client(run.account_id)
        except Exception:return service._finish(run.key,RunResult('failed','首选已被学校拒绝；账号认证或权限已变化，未继续提交',[]),run.revision)

        def allowed():
            from local_app.dual_node.fence import legacy_allowed
            if not legacy_allowed(self.accounts,run):return False
            row=self.store.get_account(run.account_id)
            permitted=self.accounts.is_allowed(row['student_id'])
            live=service.allow_live
            ready=self.ready()
            guarded=service.guard()
            # Readiness can itself inspect a durable authority snapshot. No IO
            # may follow the final deadline sample before returning permission.
            now=service.wall_clock()
            return bool(live and guarded and ready and self.same_generation(proof)
                and run.due_at<=now<=deadline and row['revision']==run.revision
                and row['credential_revision']==proof.credential_revision and (row.get('plan') or {}).get('enabled')
                and permitted and row['student_id']==proof.student_id==client.identity.student_id)

        client.allow_live=True;client.submission_guard=allowed
        retry_at,check=service._busy_retry_window(first,1)
        # Processing can be delayed by another result. The actual network finish,
        # rather than collection time, anchors the existing retry interval floors.
        delay=max(0,service.monotonic()-proof.finished_ns/1e9)
        retry_at-=delay;check['native_collection_delay_ms']=round(delay*1000,3)
        attempts[-1]['retry_check']=check
        while candidates:
            seat=candidates[0]
            if not service._wait_retry(attempts,retry_at,allowed):
                self.store.transition_run(run.key,('submitting',),'submitting',{'attempts':attempts},revision=run.revision)
                return service._finish(run.key,RunResult('failed','首选已被学校拒绝；后续执行校验失效，未继续提交',[]),run.revision)
            checked=service._retry_readback(run,client,attempts)
            if checked is not None:return checked
            if not allowed():return service._finish(run.key,RunResult('failed','查询后执行校验变化，未继续提交',[]),run.revision)
            if not self.store.transition_run(run.key,('submitting',),'submitting',{'attempted_seat_id':seat},revision=run.revision):
                self.persist_failed=True;return RunResult('uncertain','备选尝试状态未确认，禁止重交',[])
            response=client.submit(run,seat)
            attempt=asdict(response)|{'seat_id':seat}
            following=remaining_candidate_attempts(run.seat_ids,attempts+[attempt]) if response.retryable else ()
            retry=bool(following)
            if retry:retry_at,check=service._busy_retry_window(response,len(attempts)+1)
            attempts.append(attempt)
            if retry:attempts[-1]['retry_check']=check
            if not self.store.transition_run(run.key,('submitting',),'submitting',{'attempts':attempts},revision=run.revision):
                self.persist_failed=True;return RunResult('uncertain','备选回复尚未确认保存，禁止重交',[])
            if response.outcome=='accepted':return service.reconcile(run.key)
            if response.outcome=='uncertain':return service._finish(run.key,RunResult('uncertain','备选提交结果待确认，已停止后续尝试',[]),run.revision)
            if not retry:
                return service._finish(run.key,RunResult('failed',response.message or '有限座位尝试已结束',[]),run.revision)
            candidates=following
        return service._finish(run.key,RunResult('failed','有限座位尝试已结束，未继续提交',[]),run.revision)
