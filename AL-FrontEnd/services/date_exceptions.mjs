import {beijingDate,parseCalendarDate,shiftCalendarDate} from './booking_calendar.mjs';
import {isSegmentedPlan,scheduledSegmentsError} from './scheduled_segments.mjs';

export const exceptionSegments=rule=>rule?.segments?copy(rule.segments):[{id:'single',begin:rule?.begin,end:rule?.end,seat_ids:[...(rule?.seat_ids||[])]}];
export function dateEditorError(editor,policy){
  try{const weekday=new Date(parseCalendarDate(editor.date)).getUTCDay()||7;return scheduledSegmentsError({mode:'week',week_segments:{[weekday]:editor.segments}},policy);}
  catch{return '请选择有效的实际使用日期';}
}
const copy=value=>JSON.parse(JSON.stringify(value));
const following=day=>new Date(Date.parse(day+'T00:00:00Z')+86400000).toISOString().slice(0,10);

export function dateExceptionRows(exceptions={}){
  const rows=[];
  for(const day of Object.keys(exceptions||{}).sort()){
    const rule=exceptions[day],previous=rows.at(-1);
    if(rule.action==='skip'&&previous?.action==='skip'&&following(previous.end_date)===day){previous.end_date=day;continue;}
    rows.push({...copy(rule),...(rule.action==='override'?{segments:exceptionSegments(rule)}:{}),start_date:day,end_date:day});
  }
  return rows;
}

export function newDateEditor(action,account,policy,next){
  const plan=account.plan||{};
  const day=next.date||'';
  const first={begin:next.begin||plan.begin||policy.open,end:next.end||plan.end||policy.close,seat_ids:[...(next.seat_ids||plan.seat_ids||[])]};
  const segments=next.segments?.length?copy(next.segments):exceptionSegments(first);
  return {action,start_date:day,end_date:day,date:day,...segments[0],segments,segment_payload:next.segment_payload??!!next.segments,account_id:account.id,base_revision:account.revision,seat_index:0,search:'',dirty:false};
}

export function calendarDateEditor(date,account,policy,now=new Date()){
  const time=parseCalendarDate(date),plan=account?.plan;
  if(!plan)throw new Error('请先保存基本预约计划');
  const trigger=plan.trigger||policy.booking_open;
  const release=new Date(`${shiftCalendarDate(date,-1)}T${trigger}+08:00`);
  if(date<=beijingDate(now)||!Number.isFinite(release.getTime())||release<=now)throw new Error('所选日期已经放号，请调整尚未放号的日期；已有预约保留');
  const weekday=new Date(time).getUTCDay()||7,rule=account.date_exceptions?.[date];
  const rows=plan.mode==='week'?plan.week_segments?.[weekday]:plan.segments;
  const segments=rows?.length?copy(rows).sort((a,b)=>a.begin.localeCompare(b.begin)):null;
  const interval=plan.mode==='week'?plan.week_times?.[weekday]:null;
  const next=rule?.action==='override'?{segments:exceptionSegments(rule)}:segments?{segments}:{
    begin:plan.time_mode==='full_day'?policy.open:interval?.[0]||plan.begin||policy.open,
    end:plan.time_mode==='full_day'?policy.close:interval?.[1]||plan.end||policy.close,
    seat_ids:plan.seat_ids||[]
  };
  const selected=(next.segments||exceptionSegments(next)).map(row=>({...row,end:weekday===5&&row.end>policy.friday_close?policy.friday_close:row.end}));
  const segment_payload=rule?.action==='override'?Array.isArray(rule.segments):isSegmentedPlan(plan);
  return newDateEditor('override',account,policy,{date,segments:selected,segment_payload});
}
