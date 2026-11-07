import {todayDefaults} from './drafts.mjs';

const copy=value=>JSON.parse(JSON.stringify(value));
const minutes=value=>Number(value.slice(0,2))*60+Number(value.slice(3));
const timeText=value=>`${String(Math.floor(value/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`;
const validTime=value=>typeof value==='string'&&/^(?:[01]\d|2[0-3]):(?:00|30)$/.test(value);
const closing=(date,policy)=>new Date(`${date}T12:00:00+08:00`).getUTCDay()===5?policy.friday_close:policy.close;
const canonical=draft=>({date:draft.date,segments:draft.segments.map(s=>({begin:s.begin,end:s.end,seat_ids:[...s.seat_ids]}))});

export function freshTodaySegments(policy,seatIds=[],now=Date.now()){
  const defaults=todayDefaults(policy,new Date(now));
  if(!defaults.available)return {date:defaults.date,segments:[]};
  const end=timeText(Math.min(minutes(defaults.end),minutes(defaults.begin)+(policy.min_minutes||120)));
  return {date:defaults.date,segments:[{begin:defaults.begin,end,seat_ids:[...seatIds]}]};
}

export function validateTodaySegments(draft,policy,now=Date.now()){
  if(!policy)throw new Error('正在读取学校开放时间');
  if(draft?.date!==todayDefaults(policy,new Date(now)).date)throw new Error('使用日期须为北京时间今天，请重新打开当天预约');
  if(!Array.isArray(draft.segments)||draft.segments.length<1||draft.segments.length>7)throw new Error('请填写 1–7 个时段');
  const endOfDay=closing(draft.date,policy);
  const segments=draft.segments.map((row,index)=>{
    const fail=message=>{throw new Error(`第 ${index+1} 段：${message}`);};
    if(!validTime(row?.begin)||!validTime(row?.end))fail('请选择整点或半点时间');
    const begin=row.begin,end=row.end>endOfDay?endOfDay:row.end;
    if(begin<policy.open||end>policy.close)fail('时段须在学校开放时间内');
    // Friday has an explicit earlier closing cap; other days reject out-of-hours ends.
    if(endOfDay===policy.close&&row.end>policy.close)fail('时段须在学校开放时间内');
    const duration=minutes(end)-minutes(begin);
    if(duration<(policy.min_minutes||120)||duration>(policy.max_minutes||900))fail('实际使用时段须为 2–15 小时');
    if(Date.parse(`${draft.date}T${begin}:00+08:00`)<=now)fail('开始时间已过，请选择尚未开始的时段');
    if(!Array.isArray(row.seat_ids)||!row.seat_ids.length||row.seat_ids.length>5||row.seat_ids.some(s=>typeof s!=='string'||!s)||new Set(row.seat_ids).size!==row.seat_ids.length)fail('请选择 1–5 个不同的首选、备选座位');
    return {begin,end,seat_ids:[...row.seat_ids]};
  }).sort((a,b)=>a.begin.localeCompare(b.begin));
  for(let index=1;index<segments.length;index++){
    if(segments[index].begin<segments[index-1].end)throw new Error('时段之间有重叠，请调整后再检查');
  }
  return {date:draft.date,segments};
}

export function appendTodaySegment(draft,policy){
  if(draft.segments.length>=7)throw new Error('最多添加 7 个时段');
  if(!draft.segments.length||draft.segments.some(s=>!validTime(s.begin)||!validTime(s.end)))throw new Error('请先填写完整的使用时段');
  const last=[...draft.segments].sort((a,b)=>b.end.localeCompare(a.end))[0];
  const begin=last.end,end=timeText(minutes(begin)+(policy.min_minutes||120));
  if(end>closing(draft.date,policy))throw new Error('闭馆前已没有可添加的两小时时段，请调整已有时段');
  return {...canonical(draft),segments:[...canonical(draft).segments,{begin,end,seat_ids:[...last.seat_ids]}]};
}

/** Account-owned draft plus one authorization; status recovery only performs GETs. */
export function createTodayBatchController({state,context,policy,seatIds,api,now=()=>Date.now(),initialEditor=null,onDraft=()=>{},onResult=()=>{}}){
  let generation=0,alive=true,baseline='',previewContext='',polling=false,notified='';
  const identity=()=>JSON.stringify([context().accountId,context().sessionKey,context().revision]);
  const ticket=()=>({generation,identity:identity()});
  const current=token=>alive&&token.generation===generation&&token.identity===identity();
  const active=()=>state.confirmationUnknown||state.batch?.state==='running';
  const needsReconcile=()=>!!state.batch?.segments?.some(segment=>segment.status==='uncertain');
  const locked=()=>state.loading||state.submitting||state.restoring||state.reconciling||!!state.confirmedId;
  const availability=(at=now())=>{
    const window=todayDefaults(policy(),new Date(at));
    return state.draft.date!==window.date?{available:false,message:'日期已变化，请关闭后重新打开当天预约；现有草稿已保留。'}:
      {available:window.available,message:window.available?'':'今天已无可预约时段（需尚未开始的整点或半点，且至少两小时）。'};
  };
  const editor=()=>({account_id:context().accountId,...copy(state.draft),baseline,
    dirty:JSON.stringify(state.draft)!==baseline,
    pending:state.confirmedId?{batch_id:state.confirmedId,expires_at:state.expiresAt||''}:null});
  const emitDraft=()=>{if(alive)onDraft(editor());};
  function reset(saved=null){
    generation++;polling=false;previewContext='';notified='';
    const fresh=freshTodaySegments(policy(),seatIds(),now());
    const own=saved?.account_id===context().accountId&&saved.date===fresh.date&&Array.isArray(saved.segments);
    Object.assign(state,{draft:own?canonical(saved):fresh,preview:null,batch:null,error:'',loading:false,submitting:false,restoring:false,reconciling:false,
      confirmedId:own?saved.pending?.batch_id||'':'',expiresAt:own?saved.pending?.expires_at||'':'',confirmationUnknown:!!(own&&saved.pending)});
    baseline=own&&typeof saved.baseline==='string'?saved.baseline:JSON.stringify(state.draft);
    emitDraft();
  }
  function setDraft(draft){
    if(locked())return false;
    state.draft=copy(draft);state.preview=null;state.error='';previewContext='';emitDraft();return true;
  }
  function available(interactive=false){
    if(context().offline){state.error='请重新实时登录后使用当天预约';return false;}
    if(interactive&&context().disabled){state.error='当前操作尚未完成，请稍后再试';return false;}
    return !!context().accountId;
  }
  function acceptBatch(batch,expected,notifyUpdate=false){
    if(!batch||batch.batch_id!==expected||batch.date!==state.draft.date||!['running','completed','stopped'].includes(batch.state)||!Array.isArray(batch.segments))throw new Error('预约进度响应异常，请刷新进度核对');
    state.batch=batch;state.confirmationUnknown=false;state.confirmedId=expected;state.error='';
    if(batch.expires_at)state.expiresAt=batch.expires_at;
    emitDraft();
    if(batch.state!=='running'&&(notified!==expected||notifyUpdate)){notified=expected;onResult(batch);}
  }
  async function prepare(){
    if(locked()||!available(true))return;
    if(!availability().available){state.error=availability().message;state.preview=null;return;}
    let data;
    try{data=validateTodaySegments(state.draft,policy(),now());}catch(error){state.error=error.message;state.preview=null;return;}
    const token=ticket(),sent=JSON.stringify(state.draft),id=context().accountId;
    state.loading=true;state.error='';state.preview=null;
    try{
      const result=await api.previewTodayBatch(id,{...data,expected_revision:context().revision});
      if(!current(token)||JSON.stringify(state.draft)!==sent)return;
      if(!result?.batch_id||!result.preview_token||result.date!==data.date||!Array.isArray(result.segments)||!Number.isFinite(Date.parse(result.expires_at)))throw new Error('预约预检响应异常，请重新检查');
      state.preview=result;previewContext=token.identity;
    }catch(error){if(current(token))state.error=error.message;}
    finally{if(current(token))state.loading=false;}
  }
  async function confirm(){
    if(state.submitting||state.confirmedId||!state.preview||!available(true))return;
    if(!availability().available){state.error=availability().message;state.preview=null;return;}
    if(previewContext!==identity()){state.error='账号配置已变化，请重新检查';state.preview=null;return;}
    if(!context().allowLive){state.error='真实预约未开启，可检查但暂不能确认';return;}
    if(Date.parse(state.preview.expires_at)<=now()){state.error='预检已过期，请重新检查';state.preview=null;return;}
    const token=ticket(),id=context().accountId,preview=state.preview,priorBaseline=baseline;
    state.submitting=true;state.confirmedId=preview.batch_id;state.expiresAt=preview.expires_at;
    state.error='';baseline=JSON.stringify(state.draft);emitDraft();
    try{
      const result=await api.submitTodayBatch(id,preview.preview_token);
      if(!current(token))return;
      acceptBatch(result,preview.batch_id);state.preview=null;
    }catch(error){
      if(!current(token))return;
      state.preview=null;
      if(!error.status||error.status>=500){
        state.confirmationUnknown=true;
        state.error='确认请求的结果尚未收到，正在核对原批次，请勿重复提交。';
      }else{
        state.confirmedId='';state.expiresAt='';baseline=priorBaseline;state.error=error.message;
      }
      emitDraft();
    }finally{if(current(token))state.submitting=false;}
  }
  async function poll(){
    if(polling||state.reconciling||!state.confirmedId||!available())return;
    polling=true;const token=ticket(),id=context().accountId,key=state.confirmedId;
    try{
      const result=await api.getTodayBatch(id,key);
      if(current(token)&&key===state.confirmedId)acceptBatch(result,key);
    }catch(error){
      if(!current(token))return;
      if(error.status===404&&state.confirmationUnknown){
        if(Number.isFinite(Date.parse(state.expiresAt))&&Date.parse(state.expiresAt)<=now()){
          state.confirmationUnknown=false;state.confirmedId='';state.expiresAt='';
          state.error='确认未被接收，本批未提交。可以重新检查预约。';emitDraft();
        }else state.error='暂未查询到原批次，继续核对中，请勿重复提交。';
      }else state.error=error.message;
    }finally{if(current(token))polling=false;}
  }
  async function reconcile(){
    if(state.reconciling||state.loading||state.submitting||state.restoring||active()||!needsReconcile()||!state.confirmedId||!available(true))return;
    const id=context().accountId,key=state.confirmedId,prefix=`${id}:${state.batch.date}:manual:`;
    const runIds=state.batch.segments.filter(segment=>segment.status==='uncertain').map(segment=>segment.run_id);
    if(runIds.length>7||new Set(runIds).size!==runIds.length||runIds.some(run=>typeof run!=='string'||!run.startsWith(prefix)||!/^[a-f0-9]{32}$/.test(run.slice(prefix.length)))){
      state.error='待核实时段缺少有效的运行记录，请在学校记录中核对。';return;
    }
    // A prior local GET must not overwrite this explicit school's newer readback.
    generation++;polling=false;
    const token=ticket();
    state.reconciling=true;state.error='';
    try{
      // The bound run preserves its original date even after Beijing midnight.
      for(const runId of runIds){
        await api.reconcileRun(runId);
        if(!current(token)||key!==state.confirmedId)return;
      }
      const result=await api.getTodayBatch(id,key);
      if(!current(token)||key!==state.confirmedId)return;
      acceptBatch(result,key,true);
      if(needsReconcile())state.error='已查询学校记录，仍有时段待核实；请稍后再次核对，不会自动重交。';
    }catch(error){if(current(token))state.error=error.message;}
    finally{if(current(token))state.reconciling=false;}
  }
  async function restore(){
    if(state.restoring||!available())return;
    if(state.confirmedId)return poll();
    state.restoring=true;const token=ticket(),id=context().accountId;
    try{
      const response=await api.latestTodayBatch(id);
      if(current(token)&&response.batch?.date===state.draft.date)acceptBatch(response.batch,response.batch.batch_id);
    }catch(error){if(current(token))state.error=error.message;}
    finally{if(current(token))state.restoring=false;}
  }
  function startNew({preserveDraft=false}={}){
    if(active()||needsReconcile()||state.submitting||state.loading||state.restoring||state.reconciling||context().disabled)return false;
    const saved=editor();
    reset(saved.dirty||preserveDraft?{...saved,pending:null}:null);return true;
  }
  function dispose(){alive=false;generation++;}
  reset(initialEditor);
  return {reset,setDraft,prepare,confirm,poll,reconcile,restore,startNew,dispose,editor,locked,active,needsReconcile,availability};
}
