"""Account-scoped execution evidence, independent of plan synchronization."""
import math
import time

MESSAGES={'waiting':'双机计划已同步，等待探测窗口','probing':'双机正在探测网络和执行资格',
          'eligible':'双机探测通过，等待选机','selected':'已选定执行节点，正在准备',
          'ready':'双机执行节点已准备就绪','authorized':'双机任务已授权，等待执行结果',
          'idle':'当前没有待执行的双机任务','paused':'双机任务已暂停',
          'missed':'双机任务到期未授权，请联系管理员核查','uncertain':'双机结果待核实，请联系管理员',
          'blocked':'双机执行资格尚未通过，请联系管理员检查','unavailable':'双机执行状态尚未确认，请联系管理员检查'}


def fresh(value,now,maximum=90):
    return type(value) in (int,float) and math.isfinite(value) and -1<=now-value<=maximum


def execution_health(runtime,*,managed=False,account_id=None,now=None):
    result=dict(runtime)
    if not managed:return result
    now=time.time() if now is None else now
    publisher=result.get('dual_node_publisher') or {}
    at=publisher.get('last_synced_at');pending=publisher.get('pending_changes',0)
    synced=publisher.get('status')=='synchronized' and fresh(at,now) and type(pending) is int and pending==0
    snapshot=publisher.get('execution_health') or {}
    evidence=snapshot.get('summary') if account_id is None else (snapshot.get('accounts') or {}).get(account_id)
    good=synced and fresh(snapshot.get('checked_at'),now,30) and type(evidence) is dict
    state=evidence.get('state') if good else ('blocked' if publisher.get('status')=='blocked' else 'unavailable')
    if state not in MESSAGES:state='blocked'
    message=MESSAGES[state]
    result['execution_scope']='dual_node'
    result['dual_node_status']={'state':state,'message':message,'last_synced_at':at if fresh(at,now) else None,
                               'pending_changes':pending if type(pending) is int and pending>=0 else None,
                               'degraded':bool(good and evidence.get('degraded') is True)}
    if good and type(evidence.get('release_at')) in (int,float):result['dual_node_status']['release_at']=evidence['release_at']
    local_ready=(result.get('ready_to_submit') is True and result.get('allow_live') is not False
                 and result.get('clock_ok') is not False and result.get('operator_paused') is not True)
    result['ready_to_submit']=bool(local_ready and good and state=='ready' and evidence.get('ready') is True)
    warning=result.get('scheduler_warning')
    if not warning and not local_ready:warning='本机执行安全门禁尚未通过，请检查原调度状态'
    if not warning and state in ('blocked','unavailable','missed','uncertain','paused'):warning=message
    result['scheduler_warning']=warning
    result['execution_message']=warning or message
    return result
