import {isSegmentedPlan,scheduledSegmentsError,nextScheduledWindow} from './scheduled_segments.mjs';
const copy = value => JSON.parse(JSON.stringify(value));
const defaultPlan = () => ({seat_ids:[],day_offset:1,mode:'day',begin:'09:00',end:'12:00',week_times:{},trigger:'',enabled:false});
const accountPart = d => JSON.stringify([d.student,d.alias]);
const planPart = d => JSON.stringify([d.plan,d.week]);

export function planWindowError(plan,policy){
  if(!policy)return '正在读取学校开放规则';
  if(isSegmentedPlan(plan))return scheduledSegmentsError(plan,policy);
  if(plan.mode==='day'&&plan.time_mode==='full_day')return '';
  const minutes=value=>Number(value.slice(0,2))*60+Number(value.slice(3));
  const valid=value=>typeof value==='string'&&/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
  const check=(begin,end,friday)=>{
    if(!valid(begin)||!valid(end))return '请填写完整的 HH:MM 时段';
    if(begin<policy.open||end>policy.close)return '时段须在学校开放时间内';
    const finish=friday&&end>policy.friday_close?policy.friday_close:end;
    const duration=minutes(finish)-minutes(begin);
    if(duration<(policy.min_minutes||120)||duration>(policy.max_minutes||900))return friday?'周五最晚20:00结束，调整后至少保留2小时':'时段须为2–15小时';
    return '';
  };
  if(plan.mode!=='week')return check(plan.begin,plan.end,false)||check(plan.begin,plan.end,true);
  let count=0;
  for(let day=1;day<=7;day++){
    const row=plan.week_times?.[day];
    if(row!=null&&(!Array.isArray(row)||row.length!==2))return '请填写完整的 HH:MM 时段';
    const [begin,end]=row||['',''];
    if(!begin&&!end)continue;
    count++;const error=check(begin,end,day===5);if(error)return error;
  }
  return count?'':'请至少选择一个每周使用日';
}

export function withSchoolDefaults(plan,policy){
  return {...copy(plan),day_offset:policy.day_offset,trigger:policy.booking_open,
    time_mode:'full_day',mode:'day',begin:policy.open,end:policy.close,week_times:{}};
}
export function withWeeklyDefaults(plan,policy){
  return {...withSchoolDefaults(plan,policy),mode:'week',time_mode:'custom',
    week_times:Object.fromEntries(Array.from({length:7},(_,i)=>[String(i+1),[policy.open,i===4?policy.friday_close:policy.close]]))};
}
export function todayDefaults(policy,now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).map(p=>[p.type,p.value]));
  const date=`${parts.year}-${parts.month}-${parts.day}`;
  const weekday=new Date(`${date}T12:00:00+08:00`).getUTCDay();
  const minutes=value=>Number(value.slice(0,2))*60+Number(value.slice(3));
  const start=Math.max(minutes(policy.open),Math.floor((Number(parts.hour)*60+Number(parts.minute))/30)*30+30);
  const end=weekday===5?policy.friday_close:policy.close;
  return {date,begin:`${String(Math.floor(start/60)).padStart(2,'0')}:${String(start%60).padStart(2,'0')}`,end,
    available:minutes(end)-start>=(policy.min_minutes||120),seat_ids:[]};
}

export function withTimePreset(plan,policy,preset){
  const next=copy(plan);
  if(preset==='custom')return {...next,time_mode:'custom'};
  if(preset==='full_day')return {...next,time_mode:'full_day',mode:'day',begin:policy.open,end:policy.close,week_times:{}};
  const times={morning:[policy.open,'12:00'],afternoon:['14:00','18:00'],evening:['18:00',policy.close]}[preset];
  if(!times)throw new Error('Unknown time preset');
  const [begin,end]=times;
  return {...next,time_mode:'custom',begin,end,...(next.mode==='week'?{
    week_times:Object.fromEntries(Array.from({length:7},(_,i)=>[String(i+1),[begin,i===4&&end>policy.friday_close?policy.friday_close:end]]))}:{} )};
}

export function nextPlanWindow(plan,policy,now=new Date(),dateExceptions={}){
  if(!policy||!plan)return {available:false,message:'正在读取学校时间规则'};
  const validTime=value=>typeof value==='string'&&/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
  if(!/^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(plan.trigger||''))return {available:false,message:'请填写有效的放号时间'};
  if(isSegmentedPlan(plan))return nextScheduledWindow(plan,policy,now,dateExceptions);
  const addDays=(day,n)=>new Date(Date.parse(day+'T00:00:00Z')+n*86400000).toISOString().slice(0,10);
  const today=todayDefaults(policy,now).date;
  const firstRelease=addDays(today,new Date(`${today}T${plan.trigger}+08:00`)<=now?1:0);
  const exceptions=dateExceptions||{};
  for(let i=0;i<(Object.keys(exceptions).length+1)*7+1;i++){
    const release=addDays(firstRelease,i),date=addDays(release,1);
    const weekday=new Date(date+'T12:00:00+08:00').getUTCDay()||7;
    const rule=exceptions[date];
    if(rule?.action==='skip')continue;
    if(rule?.action==='override'&&rule.segments){
      const segments=copy(rule.segments).sort((a,b)=>a.begin.localeCompare(b.begin)).map(row=>({...row,end:weekday===5&&row.end>policy.friday_close?policy.friday_close:row.end}));
      const message=scheduledSegmentsError({mode:'week',week_segments:{[weekday]:segments}},policy);
      return {date,release_date:release,weekday,...segments[0],segments,available:!message,message,source:'exception'};
    }
    const interval=rule?.action==='override'?[rule.begin,rule.end]:plan.time_mode==='full_day'?[policy.open,weekday===5?policy.friday_close:policy.close]:
      plan.mode==='week'?plan.week_times?.[String(weekday)]:[plan.begin,plan.end];
    if(!interval||!interval[0]&&!interval[1])continue;
    const [begin,chosenEnd]=interval;
    if(!validTime(begin)||!validTime(chosenEnd))return {available:false,message:'请填写完整的开始和结束时间'};
    const end=weekday===5&&chosenEnd>policy.friday_close?policy.friday_close:chosenEnd;
    const minutes=value=>Number(value.slice(0,2))*60+Number(value.slice(3));
    const duration=minutes(end)-minutes(begin),available=begin>=policy.open&&end<=policy.close&&duration>=(policy.min_minutes||120)&&duration<=900;
    return {date,release_date:release,begin,end,weekday,available,adjusted:end!==chosenEnd,
      seat_ids:[...((rule?.action==='override'?rule.seat_ids:plan.seat_ids)||[])],source:rule?.action==='override'?'exception':'base',
      message:available?'':weekday===5?'周五最晚20:00结束，调整后须保留至少2小时':'使用时间须在开放时段内，且为2–15小时'};
  }
  return {available:false,message:'请至少选择一个每周使用日'};
}
export function createDraft(account,policy){
  const defaults=policy?withSchoolDefaults(defaultPlan(),policy):defaultPlan();
  const d={student:account?.student_id||'',alias:account?.alias||'',password:'',revision:account?.revision??null,plan:copy(account?.plan||defaults)};
  d.week=copy(d.plan.week_times||{});d.accountBaseline=accountPart(d);d.planBaseline=planPart(d);
  return d;
}
export const isDirty = d => accountPart(d)!==d.accountBaseline || planPart(d)!==d.planBaseline || !!d.password || (!!d.todayBaseline&&JSON.stringify(d.today)!==d.todayBaseline) || !!d.todayEditor?.dirty || !!d.editor?.dirty || !!d.dateEditor?.dirty || !!d.dateEditor?.libraryEditor?.dirty;
export const persistableDraft = d => ({...copy(d),password:''});

export function applySavedAccount(current,account,sent){
  const baseline=createDraft(account),next=copy(current);next.revision=account.revision;
  if(current.student===sent.student)next.student=baseline.student;
  if(current.alias===sent.alias)next.alias=baseline.alias;
  if(current.password===sent.password)next.password='';
  next.accountBaseline=baseline.accountBaseline;
  return next;
}
export function applySavedPlan(current,account,sent){
  const baseline=createDraft(account),next=copy(current);next.revision=account.revision;
  if(planPart(current)===planPart(sent)){next.plan=baseline.plan;next.week=baseline.week;}
  next.planBaseline=baseline.planBaseline;
  return next;
}
