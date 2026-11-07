"""Bounded, read-only reporting over persisted evidence, never school sessions."""
from datetime import date, datetime, timedelta
import math
from pymongo.errors import ExecutionTimeout, PyMongoError
from .models import AppError, ZONE, validate_plan, resolve_run
from .date_exceptions import next_scheduled_run, next_scheduled_runs, validate_date_exceptions
from .seat_conflicts import detect_seat_conflicts

LIMIT = 5000
STATES = ('success','failed','skipped','uncertain','pending')
PHASE_FIELDS = {'queued_at','started_at','finished_at','queue_ms','elapsed_ms',
    'account_lock_wait_ms','school_login_ms','booking_preflight_ms','session_ms',
    'payload_prepare_ms','payload_prepare_total_ms','execution_started_at','attempt_count'}


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def distribution(values):
    values = sorted(v for v in values if number(v) and v >= 0)
    return {'count':len(values), 'p50':values[math.ceil(len(values)*.5)-1] if values else None,
            'p95':values[math.ceil(len(values)*.95)-1] if values else None}


def bounded(collection, query, projection):
    rows = list(collection.find(query,projection).limit(LIMIT+1).max_time_ms(1000))
    if len(rows)>LIMIT:raise AppError('dashboard_limit','统计范围超过5000条，请缩小日期范围；未返回不完整统计',400)
    return rows


def totals(rows):
    counts = dict.fromkeys(STATES,0)
    for row in rows:
        status = row.get('status')
        counts[status if status in STATES else 'pending'] += 1
    closed = counts['success']+counts['failed']+counts['skipped']
    return {'total':len(rows),**counts,'closed':closed,
            'success_rate':round(counts['success']/closed*100,1) if closed else None}


def safe_phases(row):
    phases = row.get('scheduler_timings') or {}
    return {stage:{key:value for key,value in values.items() if key in PHASE_FIELDS and number(value)}
            for stage,values in phases.items() if stage in ('prepare','warm','submit') and isinstance(values,dict)}


def safe_attempts(row):
    result=[]
    for index,attempt in enumerate(row.get('attempts') or []):
        timing=attempt.get('timings') or {}
        result.append({'index':index+1,'outcome':str(attempt.get('outcome',''))[:40],
            'message':str(attempt.get('message',''))[:300], 'sent':attempt.get('sent') is True,
            'timings':{k:v for k,v in timing.items() if k in ('request_ms','prepare_ms','request_started_at',
                'request_finished_at','guard_ms','timing_version') and number(v)}})
    return result


def _build(store,catalog,params,now,runtime):
    day_text=params.get('date') or now.date().isoformat()
    try:
        selected=date.fromisoformat(day_text)
        if selected.isoformat()!=day_text or not 2000<=selected.year<=2100:raise ValueError()
        days=int(params.get('days','14'))
        if str(days)!=str(params.get('days',14)) or days not in (7,14,30):raise ValueError()
    except (TypeError,ValueError):raise AppError('invalid_dashboard','请选择有效日期和7、14或30天范围') from None
    kind=params.get('kind','scheduled')
    if kind not in ('all','scheduled','manual'):raise AppError('invalid_dashboard','任务类型无效')
    start=selected-timedelta(days=days-1)
    lower=datetime.combine(start,datetime.min.time(),ZONE).timestamp()
    upper=datetime.combine(selected+timedelta(days=1),datetime.min.time(),ZONE).timestamp()
    query={'spec.due_at':{'$gte':lower,'$lt':upper}}
    if kind=='manual':query['spec.kind']='manual'
    elif kind=='scheduled':query['$or']=[{'spec.kind':'scheduled'},{'spec.kind':{'$exists':False}},{'spec.kind':None}]
    rows=bounded(store.runs,query,{'_id':1,'account_id':1,'target_date':1,'spec':1,
        'status':1,'message':1,'attempts':1,'scheduler_timings':1})
    saved=bounded(store.accounts,{}, {'student_id':1,'alias':1,'revision':1,'verified_at':1,'plan':1,'date_exceptions':1})
    access=bounded(store.db.campus_access,{}, {'enabled':1,'note':1,'last_login':1})
    by_student={a['student_id']:a for a in saved};access_map={a['_id']:a for a in access}
    accounts=[];alerts=[];conflict_plans=[]
    population={'authorized':sum(a.get('enabled') is True for a in access),'saved':len(saved),
                'enabled':0,'checked':0,'attention':0,'next_due':None}
    for student in sorted(set(by_student)|set(access_map)):
        a=by_student.get(student);permission=access_map.get(student,{})
        allowed=permission.get('enabled') is True;raw_plan=(a or {}).get('plan')
        malformed=(raw_plan is not None and not isinstance(raw_plan,dict) or
                   isinstance(raw_plan,dict) and 'enabled' in raw_plan and type(raw_plan['enabled']) is not bool)
        plan=raw_plan if isinstance(raw_plan,dict) else {}
        enabled=plan.get('enabled') is True
        item={'student_id':student,'account_id':a['_id'] if a else None,
            'name':str(permission.get('note') or (a or {}).get('alias') or student)[:120],
            'authorized':allowed,'saved':bool(a),'plan_enabled':enabled,
            'last_login':permission.get('last_login'),'verified_at':(a or {}).get('verified_at'),
            'next_run':None,'check':'not_saved' if not a else 'disabled',
            'message':'待同学登录填写' if not a else '未开启定时计划'}
        if enabled or malformed:
            population['enabled']+=int(enabled)
            try:
                if malformed:raise AppError('invalid_plan','原有计划格式异常')
                if not allowed:raise AppError('access_denied','该学号未获使用权限')
                checked=validate_plan(plan,catalog)
                exceptions=validate_date_exceptions(a.get('date_exceptions',{}),catalog)
                run=next_scheduled_run(a['_id'],a['revision'],checked,now,exceptions)
                if run:
                    item['next_run']={'due_at':run.due_at,
                        'release_at':datetime.fromtimestamp(run.due_at,ZONE).isoformat(),
                        'target_date':run.target_date,'begin':run.begin,'end':run.end,
                        'seats':[{'id':s,'name':catalog[s].name} for s in run.seat_ids]}
                next_runs = next_scheduled_runs(a['_id'], a['revision'], checked, now, exceptions)
                item['next_runs'] = [{'due_at': r.due_at, 'target_date': r.target_date,
                    'begin': r.begin, 'end': r.end,
                    'seats': [{'id': s, 'name': catalog[s].name} for s in r.seat_ids]} for r in next_runs]
                if item['next_run']:
                    item['next_run']['segments'] = item['next_runs']
                if not item['next_run']:raise AppError('invalid_plan','未来一周没有可执行时段')
                item.update(check='ready',message='本地计划检查通过，学校登录待临近放号验证')
                conflict_plans.append({'account_id':item['account_id'],'student_id':student,'name':item['name'],
                    'revision':a['revision'],'plan':checked,'date_exceptions':exceptions})
                population['checked']+=1
            except (AppError,ValueError,TypeError,KeyError):
                item.update(check='attention',message='计划或准入配置需要核对，请查看账号设置')
                population['attention']+=1
                alerts.append({'type':'plan', 'account_id':item['account_id'],'name':item['name'],
                    'student_id':student,'message':item['message']})
        accounts.append(item)
    dues=[a['next_run']['due_at'] for a in accounts if a['next_run']]
    population['next_due']=min(dues) if dues else None
    seat_conflicts=detect_seat_conflicts(conflict_plans,catalog,now)
    seat_conflicts['unchecked_accounts']=population['attention']
    labels={a['account_id']:a for a in accounts if a['account_id']}
    groups={(start+timedelta(days=i)).isoformat():[] for i in range(days)}
    for row in rows:
        groups[datetime.fromtimestamp(row['spec']['due_at'],ZONE).date().isoformat()].append(row)
    trend=[]
    for day,group in groups.items():
        values=[(attempt.get('timings') or {}).get('request_ms') for row in group
                for attempt in (row.get('attempts') or []) if attempt.get('sent') is not False]
        trend.append({'date':day,**totals(group),'request':distribution(values)})
    today=sorted(groups[selected.isoformat()],key=lambda r:(r['spec']['due_at'],str(r['_id'])),reverse=True)
    delays=[];missing=0;details=[]
    for row in today:
        who=labels.get(row.get('account_id'),{})
        base={'id':str(row['_id']),'account_id':row.get('account_id'),'name':who.get('name','原账号'),
              'student_id':who.get('student_id','')}
        attempts=row.get('attempts') or [];first=attempts[0] if attempts else {}
        first_start=(first.get('timings') or {}).get('request_started_at')
        scheduled=(row['spec'].get('kind') or 'scheduled')=='scheduled'
        delay=(first_start-row['spec']['due_at'])*1000 if number(first_start) else None
        if scheduled and number(delay) and first.get('sent') is not False:
            delays.append({**base,'value_ms':round(delay,3)})
        elif scheduled:missing+=1
        if row.get('status') in ('failed','skipped','uncertain'):
            alerts.append({**base,'type':row['status'],'message':str(row.get('message',''))[:500]})
        if len(details)<200:
            details.append({**base,'status':row.get('status'),'message':str(row.get('message',''))[:500],
                'target_date':row.get('target_date') or row['spec'].get('target_date'),
                'kind':row['spec'].get('kind') or 'scheduled','begin':row['spec'].get('begin'),
                'end':row['spec'].get('end'),'attempts':safe_attempts(row),'phases':safe_phases(row)})
    status={key:runtime[key] for key in ('allow_live','clock_ok','scheduler_running','ready_to_submit','capacity',
        'last_error','scheduler_warning','execution_message','executor','executor_state',
        'executor_message','last_recovery_at','last_wake_at','execution_scope','dual_node_status') if key in runtime}
    # Root-only runtime input; render only fixed alert codes and anonymous refs.
    from .dual_node.monitor import MESSAGES as dual_messages
    saved = (runtime.get('dual_node_publisher') or {}).get('monitor_alerts') or {}
    saved_rows = saved.get('alerts')
    monitor_alerts=[]
    if type(saved_rows) is list:
        saved_rows=sorted(saved_rows[:128],key=lambda r:(r.get('active') is True,
            r.get('last_seen') if number(r.get('last_seen')) else 0) if type(r) is dict else (False,0),reverse=True)
        for row in saved_rows:
            if (type(row) is dict and row.get('code') in dual_messages
                    and type(row.get('task_ref')) is str
                    and len(row['task_ref']) == 16 and all(c in '0123456789abcdef' for c in row['task_ref'])):
                monitor_alerts.append({'type':'dual_node','message':dual_messages[row['code']],
                    'task_ref':row['task_ref'],'active':row.get('active') is True,
                    'first_seen':row.get('first_seen') if number(row.get('first_seen')) else None})
    alerts=monitor_alerts+alerts
    if saved.get('persistence') == 'unavailable':
        alerts.insert(0, {'type':'dual_node','message':'双机告警记录暂不可用，请检查本机诊断目录'})
    if status.get('last_error') or status.get('scheduler_warning'):
        alerts.insert(0,{'type':'runtime','message':status.get('last_error') or status.get('scheduler_warning')})
    return {'generated_at':now.isoformat(),'date':selected.isoformat(),'start_date':start.isoformat(),
        'days':days,'kind':kind,'summary':totals(today),'population':population,'trend':trend,
        'delays':delays[:200],'delay_count':len(delays),'delay_missing':missing,
        'accounts':accounts,'alerts':alerts[:50],'alert_total':len(alerts),'seat_conflicts':seat_conflicts,'runs':details,
        'runs_total':len(today),'runtime':status}


def build_dashboard(store,catalog,params,*,now=None,runtime=None):
    try:
        return _build(store,catalog,params,now or datetime.now(ZONE),runtime or {})
    except ExecutionTimeout:
        raise AppError('dashboard_timeout','统计读取超时，请稍后刷新；保留上次结果',503) from None
    except PyMongoError:
        raise AppError('dashboard_unavailable','统计数据暂不可用，请稍后刷新',503) from None
