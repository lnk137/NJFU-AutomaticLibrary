const DAY=86400000;
const pad=value=>String(value).padStart(2,'0');

export function beijingDate(now=new Date()){
  return new Date(now.getTime()+8*3600000).toISOString().slice(0,10);
}
export function parseCalendarDate(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))throw new Error('日历日期无效');
  const time=Date.parse(value+'T00:00:00Z');
  if(!Number.isFinite(time)||new Date(time).toISOString().slice(0,10)!==value)throw new Error('日历日期无效');
  return time;
}
export function shiftCalendarDate(value,amount){return new Date(parseCalendarDate(value)+amount*DAY).toISOString().slice(0,10);}
export function calendarRange(anchor,mode='month'){
  const time=parseCalendarDate(anchor),day=new Date(time),weekday=(day.getUTCDay()+6)%7;
  if(mode==='week'){const start=shiftCalendarDate(anchor,-weekday);return {start,end:shiftCalendarDate(start,6)};}
  if(mode!=='month')throw new Error('日历视图无效');
  const first=`${day.getUTCFullYear()}-${pad(day.getUTCMonth()+1)}-01`;
  const offset=(new Date(parseCalendarDate(first)).getUTCDay()+6)%7;
  const start=shiftCalendarDate(first,-offset);
  return {start,end:shiftCalendarDate(start,41)};
}
export function moveCalendarAnchor(anchor,mode,direction){
  if(mode==='week')return shiftCalendarDate(anchor,direction*7);
  const date=new Date(parseCalendarDate(anchor));date.setUTCDate(1);date.setUTCMonth(date.getUTCMonth()+direction);
  return date.toISOString().slice(0,10);
}
export const calendarStates={planned:{label:'计划',tone:'planned'},confirmed:{label:'学校回执',tone:'confirmed'},failed:{label:'失败',tone:'failed'},uncertain:{label:'待核对',tone:'uncertain'},skipped:{label:'跳过',tone:'skipped'},cancelled:{label:'已取消',tone:'cancelled'}};
export function calendarState(entry){return calendarStates[entry?.state]||calendarStates.uncertain;}
export function calendarDays(payload,range){
  if(!payload||payload.start!==range.start||payload.end!==range.end||!Array.isArray(payload.days))throw new Error('日历数据不完整，请重试');
  const expected=(parseCalendarDate(range.end)-parseCalendarDate(range.start))/DAY+1;
  if(expected<1||expected>42||payload.days.length!==expected)throw new Error('日历数据不完整，请重试');
  return payload.days.map((day,index)=>{
    if(day?.date!==shiftCalendarDate(range.start,index)||!Array.isArray(day.entries))throw new Error('日历日期或内容不完整，请重试');
    return {...day,states:[...new Set(day.entries.map(entry=>calendarState(entry).tone))]};
  });
}

// Requests belong to an account, session and sequence; hiding invalidates them.
export function createCalendarLoader({read,context,apply,pending=()=>{},failure=()=>{}}){
  let sequence=0;
  const invalidate=()=>{sequence++;pending(false);};
  async function load(range){
    const owner=context(),ticket=++sequence;
    if(!owner.accountId){apply(null,[]);pending(false);return;}
    const current=()=>{const next=context();return ticket===sequence&&owner.accountId===next.accountId&&owner.sessionKey===next.sessionKey;};
    pending(true);failure('');
    try{
      const payload=await read(owner.accountId,range.start,range.end);
      if(!current())return;
      if(payload?.account_id!==owner.accountId)throw new Error('日历账号不匹配，请重新读取');
      const days=calendarDays(payload,range);
      apply(payload,days);
    }catch(error){if(current()){apply(null,[]);failure(error?.message||'日历读取失败，请重试');}}
    finally{if(current())pending(false);}
  }
  return {load,invalidate};
}
