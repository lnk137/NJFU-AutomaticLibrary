"""Confirmed form-only manual execution; school I/O never owns booking locks."""
from dataclasses import asdict
from datetime import date, timedelta
import uuid

from .models import AppError, RunSpec, RunResult, stamp
from .recovery_policy import candidate_attempts
from .reservations import records_match
from .dual_node.fence import require_legacy


def execute_import_today(forms, key, item, expires_at):
    accounts,store,today=forms.accounts,forms.store,forms.today
    reservations=today.reservations
    account_id=item['account_id'];claimed=False;run=None;attempted=False
    require_legacy(accounts,account_id)

    def check(revision, *, own=False):
        require_legacy(accounts,account_id)
        row=store.get_account(account_id)
        if row['revision']!=revision:raise AppError('conflict','账号或计划已更新，本次未继续预约',409)
        if not accounts.is_allowed(row['student_id']):raise AppError('access_denied','账号使用权限已停用')
        if account_id in accounts.busy and not own:raise AppError('account_busy','账号正在执行预约，请稍后处理')
        forms._quiet()
        if forms.clock()>=expires_at:raise AppError('import_expired','整份审阅已过期，本次未继续预约')
        if run is not None and forms.clock()>=run.expires_at:raise AppError('preview_expired','当天预约准备或尝试已超时，请核对执行记录')
        if not reservations.allow_live:raise AppError('live_disabled','真实预约未开启，当天预约未执行')
        if not reservations.guard():raise AppError('execution_paused','执行时钟或后台状态异常，未继续预约')
        return row

    old=check(item['expected_revision'])
    _,day,plan,start=today._validate(account_id,item['today']|{'expected_revision':old['revision']})
    pending=store.runs.find_one({'account_id':account_id,'target_date':day,'status':{'$in':['preparing','submitting','uncertain']}})
    if pending:raise AppError('booking_uncertain','当天有待确认任务，请先核对学校记录')
    password=accounts.vault.unseal(account_id,item['credential'])
    changed=password!=accounts.vault.unseal(account_id,old['secret'])
    client=accounts.client_factory(item['student_id'],password,allow_live=False)
    try:
        identity=client.login();check(old['revision'])
        if identity.student_id!=item['student_id']:raise AppError('identity_mismatch','学校身份不一致')
        prepare=getattr(client,'prepare_booking',None)
        if prepare:prepare();check(old['revision'])
        records=client.query(date.fromisoformat(day),date.fromisoformat(day)+timedelta(days=3))
        check(old['revision'])
        begin,end=f'{day} {plan.begin}:00',f'{day} {plan.end}:00'
        for record in records:
            if record.student_id!=item['student_id']:raise AppError('identity_mismatch','学校查询身份不一致')
            if record.begin<end and record.end>begin:raise AppError('booking_overlap','学校记录已有重叠预约，未提交')
        run=RunSpec(account_id,old['revision']+int(changed),day,plan.begin,plan.end,tuple(plan.seat_ids),
            forms.clock(),kind='manual',operation_id=uuid.uuid5(uuid.NAMESPACE_URL,key+':'+str(item['line'])).hex,
            expires_at=min(expires_at,start-1,forms.clock()+30))
        prepare_payload=getattr(client,'prepare_submission',None)
        if prepare_payload:prepare_payload(run,run.seat_ids[0]);check(old['revision'])
        with forms._lock('student:'+item['student_id']),forms._lock(account_id):
            current=check(old['revision'])
            today._validate(account_id,item['today']|{'expected_revision':current['revision']})
            current=forms._commit(key,item,current)
            if current['revision']!=run.revision:raise AppError('conflict','账号版本已变化，未提交预约')
            existing=store.ensure_run(run)
            if existing['status'] not in ('queued','preparing'):return today._existing(existing)
            check(current['revision'])
            if not store.transition_run(run.key,('queued','preparing'),'submitting',
                {'attempted_seat_id':run.seat_ids[0],'submitted_at':forms.clock(),'reconcile_count':0},revision=run.revision):
                raise AppError('booking_uncertain','预约任务已有执行记录，请先核对')
            accounts.busy.add(account_id);claimed=True
            accounts.discard(account_id)
        accounts._observe_auth(account_id)

        def final_guard():
            try:
                current=store.get_run(run.key)
                check(run.revision,own=True)
                return current['status']=='submitting' and current['spec']['revision']==run.revision and forms.clock()<run.expires_at
            except Exception:return False

        client.allow_live=True;client.submission_guard=final_guard
        attempts=[];result=None;candidates=candidate_attempts(run.seat_ids)
        for index,seat in enumerate(candidates):
            check(run.revision,own=True)
            if index:
                if not reservations._wait_retry(attempts,retry_not_before,final_guard):
                    store.transition_run(run.key,('submitting',),'submitting',
                        {'attempts':attempts},revision=run.revision)
                    raise AppError('execution_paused','重试等待时执行校验失效，未继续预约')
                result=reservations._retry_readback(run,client,attempts)
                if result is not None:break
                check(run.revision,own=True)
                if not store.transition_run(run.key,('submitting',),'submitting',{'attempted_seat_id':seat},revision=run.revision):
                    raise AppError('booking_uncertain','尝试记录已变化，未继续提交')
            if not final_guard():raise AppError('execution_paused','提交前检查未通过，未继续预约')
            attempted=True;response=client.submit(run,seat)
            retry_pending=response.retryable and index+1<len(candidates)
            if retry_pending:
                retry_not_before,retry_check=reservations._busy_retry_window(response,len(attempts)+1)
            attempts.append(asdict(response)|{'seat_id':seat})
            if retry_pending:attempts[-1]['retry_check']=retry_check
            if not store.transition_run(run.key,('submitting',),'submitting',{'attempts':attempts},revision=run.revision):
                raise AppError('booking_uncertain','学校回复未能保存，请先核对记录')
            if response.outcome=='accepted':
                records=client.query(date.fromisoformat(day),date.fromisoformat(day))
                reservations._save_target_snapshot(run,records)
                matched=records_match(run,seat,records,client.identity,active_statuses=getattr(client,'active_statuses',frozenset()))
                result=reservations._finish(run.key,RunResult('success' if matched else 'uncertain',
                    '学校有效预约记录已核实' if matched else '提交结果待核对，请查询学校记录',records,stamp() if matched else None),run.revision)
                break
            if response.outcome=='uncertain':
                result=reservations._finish(run.key,RunResult('uncertain','提交结果待确认，未继续尝试',[]),run.revision);break
            if not response.retryable:
                result=reservations._finish(run.key,RunResult('failed','学校未接受预约，请核对使用规则或座位',[]),run.revision);break
        if result is None:result=reservations._finish(run.key,RunResult('failed','有限座位尝试已结束，未继续提交',[]),run.revision)
        return {'run_id':run.key,'result':asdict(result)}
    except Exception:
        if claimed:
            reservations._finish(run.key,RunResult('uncertain' if attempted else 'skipped',
                '执行中断，请核对学校记录；不会自动重交' if attempted else '提交前校验未通过，未提交预约',[]),run.revision)
        raise
    finally:
        if claimed:accounts.busy.discard(account_id)
        client.close()
