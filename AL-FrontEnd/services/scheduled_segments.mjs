const copy=value=>JSON.parse(JSON.stringify(value));
export const isSegmentedPlan=plan=>!!plan&&('segments' in plan||'week_segments' in plan);
const segment=(begin,end,seat_ids,id)=>({id:id||`${begin.replace(':','')}-${end.replace(':','')}`,begin,end,seat_ids:[...(seat_ids||[])]});
export function segmentedPlan(plan,mode=plan.mode){
  const next={...copy(plan),mode,time_mode:'custom'};
  next.segments=copy(plan.segments?.length?plan.segments:Object.values(plan.week_segments||{}).find(rows=>rows?.length)||[segment(plan.begin,plan.end,plan.seat_ids)]);
  next.week_segments=copy(plan.week_segments||Object.fromEntries(Object.entries(plan.week_times||{}).filter(([,v])=>v?.[0]&&v?.[1]).map(([day,v])=>[day,[segment(...v,plan.seat_ids)]])));
  if(mode==='week'&&!Object.keys(next.week_segments).length)next.week_segments=Object.fromEntries(Array.from({length:7},(_,i)=>[String(i+1),copy(next.segments)]));
  return next;
}
// Recovery is an unsaved, paused draft. The caller confirms the discarded rows
// and retains the original draft until the user explicitly saves the result.
export function ordinaryPlanRecovery(plan){
  const groups=plan.mode==='week'?Object.entries(plan.week_segments||{}).sort(([a],[b])=>Number(a)-Number(b)):[['daily',plan.segments||[]]];
  const active=groups.filter(([,rows])=>Array.isArray(rows)&&rows.length).map(([day,rows])=>[day,[...rows].sort((a,b)=>a.begin.localeCompare(b.begin))]);
  if(!active.length)throw new Error('没有可恢复的时段，请联系管理员核对原配置');
  const first=active[0][1][0],next={...copy(plan),enabled:false,time_mode:'custom',begin:first.begin,end:first.end,seat_ids:[...first.seat_ids],week_times:{}};
  delete next.segments;delete next.week_segments;
  if(next.mode==='week')next.week_times=Object.fromEntries(active.map(([day,rows])=>[day,[rows[0].begin,rows[0].end]]));
  return {plan:next,removed:active.reduce((sum,[,rows])=>sum+rows.length-1,0),
    seatsChanged:active.some(([,rows])=>JSON.stringify(rows[0].seat_ids)!==JSON.stringify(first.seat_ids)),
    intervals:active.map(([day,rows])=>({day,begin:rows[0].begin,end:rows[0].end}))};
}
export function scheduledSegmentsError(plan,policy){
  if(!policy)return '正在读取学校开放规则';
  const minutes=t=>Number(t.slice(0,2))*60+Number(t.slice(3));
  const valid=t=>typeof t==='string'&&/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(t);
  const groups=plan.mode==='week'?Object.entries(plan.week_segments||{}):[['daily',plan.segments]];
  if(!groups.length)return '请至少选择一个每周使用日';
  for(const [day,values] of groups){
    if(!Array.isArray(values)||values.length<1||values.length>7)return '每天请选择1–7个时段';
    if(values.some(row=>!row||typeof row!=='object'))return '请填写完整的时段与座位';
    const rows=[...values].sort((a,b)=>(a.begin||'').localeCompare(b.begin||''));
    const ids=new Set();
    for(let i=0;i<rows.length;i++){
      const row=rows[i];
      if(!valid(row.begin)||!valid(row.end))return '请填写完整的 HH:MM 时段';
      if(row.begin<policy.open||row.end>policy.close)return '时段须在学校开放时间内';
      const end=(day==='daily'||day==='5')&&row.end>policy.friday_close?policy.friday_close:row.end;
      if((minutes(row.end)-minutes(row.begin))<(policy.min_minutes||120)||(minutes(row.end)-minutes(row.begin))>(policy.max_minutes||900))return '每段须为2–15小时';
      if(minutes(end)-minutes(row.begin)<(policy.min_minutes||120))return '周五最晚20:00结束，调整后每段至少2小时';
      if(!Array.isArray(row.seat_ids)||!row.seat_ids.length||row.seat_ids.length>5||new Set(row.seat_ids).size!==row.seat_ids.length)return '每段请选择1–5个不同座位';
      if(!/^[A-Za-z0-9_-]{1,64}$/.test(row.id||'')||ids.has(row.id)||row.id.startsWith('override-'))return '时段标识无效或重复';
      ids.add(row.id);
      if(i&&rows[i-1].end>row.begin)return '同一天的时段不能重叠，可首尾相接';
    }
  }
  return '';
}
export function nextScheduledWindow(plan,policy,now,dateExceptions={}){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now).map(p=>[p.type,p.value]));
  const today=`${parts.year}-${parts.month}-${parts.day}`;
  const add=(day,n)=>new Date(Date.parse(day+'T00:00:00Z')+n*86400000).toISOString().slice(0,10);
  const first=add(today,new Date(`${today}T${plan.trigger}+08:00`)<=now?1:0);
  for(let i=0;i<(Object.keys(dateExceptions||{}).length+1)*7+1;i++){
    const release=add(first,i),date=add(release,1),weekday=new Date(date+'T12:00:00+08:00').getUTCDay()||7,rule=dateExceptions?.[date];
    if(rule?.action==='skip')continue;
    const values=rule?.action==='override'?(rule.segments||[{...rule,id:'single'}]):plan.mode==='week'?plan.week_segments?.[String(weekday)]:plan.segments;
    if(!values?.length)continue;
    if(!Array.isArray(values)||values.some(row=>!row||typeof row.begin!=='string'||typeof row.end!=='string'))return {available:false,message:'请填写完整的时段与座位',segments:[]};
    const segments=copy(values).sort((a,b)=>a.begin.localeCompare(b.begin)).map(row=>({...row,end:weekday===5&&row.end>policy.friday_close?policy.friday_close:row.end}));
    const error=scheduledSegmentsError({...plan,mode:'week',week_segments:{[weekday]:segments}},policy);
    return {date,release_date:release,weekday,...segments[0],segments,available:!error,message:error,source:rule?'exception':'base'};
  }
  return {available:false,message:'请至少选择一个每周使用日',segments:[]};
}
