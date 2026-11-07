import test from 'node:test';
import assert from 'node:assert/strict';
let feature;
try{feature=await import('../../AL-FrontEnd/services/today_segments.mjs');}catch{}
const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900};
const now=Date.parse('2026-11-02T08:10:00+08:00');
const segment=(begin,end,seat='1')=>({begin,end,seat_ids:[seat]});
const draft={date:'2026-11-02',segments:[segment('09:00','12:00'),segment('14:00','18:00','2')]};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function setup(overrides={},initialEditor=null){
  assert.ok(feature?.createTodayBatchController,'same-day batch controller is missing');
  const state={},context={accountId:'old',revision:1,sessionKey:1,allowLive:true,offline:false};
  const calls=[],drafts=[],results=[];let time=now;
  const batch={batch_id:'batch-one',date:draft.date,state:'running',message:'依次预约中',segments:draft.segments.map((s,index)=>({...s,index,status:'pending'}))};
  const preview={batch_id:'batch-one',date:draft.date,expires_at:new Date(now+120000).toISOString(),preview_token:'signed',segments:draft.segments.map((s,index)=>({...s,index,seats:s.seat_ids.map(id=>({id,name:'Seat '+id}))}))};
  const api={
    previewTodayBatch:async(id,data)=>{calls.push(['preview',id,data]);return preview;},
    submitTodayBatch:async(id,token)=>{calls.push(['submit',id,token]);return batch;},
    getTodayBatch:async(id,key)=>{calls.push(['get',id,key]);return batch;},
    latestTodayBatch:async id=>{calls.push(['latest',id]);return {batch:null};},...overrides
  };
  const controller=feature.createTodayBatchController({state,context:()=>context,policy:()=>policy,seatIds:()=>['1'],api,now:()=>time,initialEditor,onDraft:d=>drafts.push(d),onResult:r=>results.push(r)});
  return {state,context,calls,drafts,results,batch,preview,controller,setNow:value=>time=value};
}

test('effective segment validation sorts copies, permits adjacency and caps Friday',()=>{
  assert.ok(feature?.validateTodaySegments,'same-day interval validation is missing');
  const input={date:draft.date,segments:[segment('14:00','18:00','2'),segment('09:00','14:00')]};
  assert.deepEqual(feature.validateTodaySegments(input,policy,now).segments.map(s=>s.begin),['09:00','14:00']);
  assert.equal(input.segments[0].begin,'14:00');
  const friday=feature.validateTodaySegments({date:'2026-11-06',segments:[segment('18:00','22:00')]},policy,Date.parse('2026-11-06T08:00:00+08:00'));
  assert.equal(friday.segments[0].end,'20:00');
});
test('invalid, overlapping, expired, duplicate-seat and excessive intervals fail before HTTP',async()=>{
  const p=setup();
  for(const input of [
    {...draft,segments:[segment('09:00','12:00'),segment('11:30','14:00','2')]},
    {...draft,segments:[segment('09:10','12:00')]},
    {...draft,segments:[segment('07:30','10:00')]},
    {...draft,segments:[segment('09:00','10:00')]},
    {...draft,date:'2026-11-03'},
    {...draft,segments:[{...segment('09:00','12:00'),seat_ids:['1','1']}]},
    {...draft,segments:Array.from({length:8},()=>segment('09:00','12:00'))}
  ]){p.controller.setDraft(input);await p.controller.prepare();assert.ok(p.state.error);}
  assert.deepEqual(p.calls,[]);
});
test('new intervals have separate seat snapshots and respect the closing boundary',()=>{
  assert.ok(feature?.appendTodaySegment,'adding a same-day interval is missing');
  const first={date:draft.date,segments:[segment('09:00','12:00')]};
  const next=feature.appendTodaySegment(first,policy);
  assert.deepEqual(next.segments.map(s=>[s.begin,s.end]),[['09:00','12:00'],['12:00','14:00']]);
  next.segments[1].seat_ids.push('2');assert.deepEqual(first.segments[0].seat_ids,['1']);
  assert.throws(()=>feature.appendTodaySegment({...first,segments:[segment('19:00','22:00')]},policy),/时段|闭馆|开放/);
});
test('one confirmation submits the batch once and progress reads never submit',async()=>{
  const p=setup();p.controller.setDraft(draft);await p.controller.prepare();
  await Promise.all([p.controller.confirm(),p.controller.confirm()]);
  await p.controller.poll();await p.controller.poll();
  assert.equal(p.calls.filter(c=>c[0]==='submit').length,1);
  assert.equal(p.calls.filter(c=>c[0]==='get').length,2);
  assert.equal(p.state.batch.state,'running');
  assert.equal(p.controller.editor().dirty,false);
  assert.doesNotMatch(JSON.stringify(p.drafts),/signed|preview_token/);
});
test('editing a preview invalidates its authorization and submitted values stay locked',async()=>{
  const p=setup();p.controller.setDraft(draft);await p.controller.prepare();
  p.controller.setDraft({...draft,segments:[segment('10:00','12:00')]});
  assert.equal(p.state.preview,null);await p.controller.confirm();
  assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
  await p.controller.prepare();await p.controller.confirm();
  const before=JSON.stringify(p.state.draft);assert.equal(p.controller.setDraft(draft),false);
  assert.equal(JSON.stringify(p.state.draft),before);
});
test('late preview, submit and polling responses cannot cross an account/session boundary',async()=>{
  for(const operation of ['preview','submit','get']){
    const wait=deferred();const method={preview:'previewTodayBatch',submit:'submitTodayBatch',get:'getTodayBatch'}[operation];
    const p=setup({[method]:()=>wait.promise});p.controller.setDraft(draft);
    if(operation!=='preview')await p.controller.prepare();
    if(operation==='get')await p.controller.confirm();
    const previous=JSON.stringify(p.state.batch||null);
    const task=operation==='preview'?p.controller.prepare():operation==='submit'?p.controller.confirm():p.controller.poll();
    p.context.accountId='other';p.context.sessionKey++;
    wait.resolve(operation==='preview'?p.preview:p.batch);await task;
    assert.equal(JSON.stringify(p.state.batch||null),previous);assert.equal(p.state.preview,operation==='submit'?p.preview:null);
    assert.equal(p.results.length,0);
  }
});
test('revision changes and expired previews prevent a confirmation POST',async()=>{
  for(const change of ['revision','expiry']){
    const p=setup();p.controller.setDraft(draft);await p.controller.prepare();
    if(change==='revision')p.context.revision++;else p.setNow(now+120001);
    await p.controller.confirm();assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
    assert.match(p.state.error,/变化|过期/);
  }
});
test('an ambiguous confirmation keeps its ID and only polls for its actual result',async()=>{
  const p=setup({submitTodayBatch:async()=>{throw new Error('network timeout');}});
  p.controller.setDraft(draft);await p.controller.prepare();await p.controller.confirm();
  assert.equal(p.controller.editor().pending.batch_id,'batch-one');assert.equal(p.state.confirmationUnknown,true);
  await p.controller.confirm();assert.equal(p.state.confirmationUnknown,true);
  await p.controller.poll();assert.equal(p.state.confirmationUnknown,false);assert.equal(p.state.batch.batch_id,'batch-one');
});
test('a missing batch before expiry remains uncertain instead of allowing resubmission',async()=>{
  const p=setup({submitTodayBatch:async()=>{throw new Error('network timeout');},getTodayBatch:async()=>{throw Object.assign(new Error('not found'),{status:404});}});
  p.controller.setDraft(draft);await p.controller.prepare();await p.controller.confirm();await p.controller.poll();
  assert.equal(p.state.confirmationUnknown,true);assert.equal(p.controller.startNew(),false);
  p.setNow(now+120001);await p.controller.poll();
  assert.equal(p.state.confirmationUnknown,false);assert.match(p.state.error,/未提交|未被接收/);
});
test('progress failure preserves the previous result and a stopped batch shows untouched intervals',async()=>{
  const p=setup({getTodayBatch:async()=>{throw new Error('read failed');}});
  p.controller.setDraft(draft);await p.controller.prepare();await p.controller.confirm();
  const before=JSON.stringify(p.state.batch);await p.controller.poll();assert.equal(JSON.stringify(p.state.batch),before);
  assert.match(p.state.error,/read failed/);
});
test('restoring an account-owned pending batch performs a GET and never replays confirmation',async()=>{
  const pending={account_id:'old',...draft,baseline:JSON.stringify(draft),pending:{batch_id:'batch-one',expires_at:new Date(now+120000).toISOString()}};
  const p=setup({},pending);await p.controller.restore();
  assert.deepEqual(p.calls.map(c=>c[0]),['get']);assert.equal(p.state.batch.batch_id,'batch-one');
  const foreign=setup({}, {...pending,account_id:'other'});assert.equal(foreign.state.draft.segments.length,1);
  assert.equal(foreign.controller.editor().pending,null);
});
test('restoring the latest result neither creates requests nor overwrites a saved draft',async()=>{
  const p=setup();p.controller.setDraft(draft);
  p.batch.state='stopped';p.batch.segments[0].status='uncertain';p.batch.segments[1].status='not_submitted';
  const saved=JSON.stringify(p.state.draft);p.controller.dispose();
  const q=setup({latestTodayBatch:async()=>({batch:p.batch})},{account_id:'old',...draft,baseline:'initial',dirty:true});
  await q.controller.restore();assert.equal(JSON.stringify(q.state.draft),saved);
  assert.equal(q.state.batch.segments[1].status,'not_submitted');assert.equal(q.controller.editor().dirty,true);
});
test('continuing a saved draft after restoring an old result preserves its times and seats',async()=>{
  for(const status of ['completed','stopped']){
    const saved={account_id:'old',date:draft.date,segments:[segment('16:00','19:00','new-seat')],baseline:'original clean draft',dirty:true};
    const batch={batch_id:'older-batch',date:draft.date,state:status,message:'earlier result',segments:[{...segment('09:00','12:00'),status:'success'}]};
    const p=setup({latestTodayBatch:async()=>({batch})},saved);await p.controller.restore();
    assert.equal(p.controller.startNew(),true);
    assert.deepEqual(p.state.draft,{date:saved.date,segments:saved.segments});
    assert.equal(p.controller.editor().pending,null);assert.equal(p.controller.editor().dirty,true);
    assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
  }
});
test('offline/live-disabled sessions cannot confirm and disposed controllers ignore private errors',async()=>{
  for(const mode of ['offline','allowLive']){
    const p=setup();p.controller.setDraft(draft);await p.controller.prepare();
    p.context[mode]=mode==='offline';await p.controller.confirm();assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
  }
  const wait=deferred(),p=setup({previewTodayBatch:()=>wait.promise});p.controller.setDraft(draft);
  const request=p.controller.prepare();p.controller.dispose();wait.reject(new Error('private old account failure'));await request;
  assert.equal(p.state.error,'');
});

function stoppedBatch(){return {batch_id:'stopped-one',date:draft.date,state:'stopped',message:'请核对学校记录',
  segments:draft.segments.map((s,index)=>({...s,index,run_id:`old:${draft.date}:manual:${String(index+1).repeat(32)}`,status:index?'not_submitted':'uncertain'}))};}
test('uncertain results require one explicit school query and keep the owned draft without replaying',async()=>{
  const batch=stoppedBatch(),wait=deferred(),calls=[];
  const saved={account_id:'old',...draft,baseline:'older draft',dirty:true};
  const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async id=>{calls.push(['reconcile',id]);await wait.promise;batch.segments[0].status='success';},
    getTodayBatch:async(id,key)=>{calls.push(['get',id,key]);return batch;}},saved);
  await p.controller.restore();const before=JSON.stringify(p.state.draft);
  assert.equal(p.controller.needsReconcile(),true);assert.equal(p.controller.active(),false);
  assert.equal(p.controller.startNew(),false);
  const task=p.controller.reconcile();await p.controller.reconcile();await p.controller.poll();
  assert.equal(p.state.reconciling,true);assert.deepEqual(calls,[['reconcile',batch.segments[0].run_id]]);
  wait.resolve();await task;
  assert.deepEqual(calls,[['reconcile',batch.segments[0].run_id],['get','old','stopped-one']]);
  assert.equal(p.state.reconciling,false);assert.equal(p.controller.needsReconcile(),false);
  assert.equal(p.state.batch.state,'stopped');assert.equal(p.state.batch.segments[1].status,'not_submitted');
  assert.equal(JSON.stringify(p.state.draft),before);assert.equal(p.controller.editor().dirty,true);
  assert.equal(p.results.length,2);assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
  assert.equal(p.controller.startNew(),true);assert.equal(JSON.stringify(p.state.draft),before);
});
test('failed explicit school query or readback retains uncertainty and permits a safe retry',async()=>{
  for(const phase of ['query','get']){
    const batch=stoppedBatch();let queries=0;
    const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async()=>{queries++;if(phase==='query')throw new Error('query unavailable');},
      getTodayBatch:async()=>{throw new Error('readback unavailable');}});
    await p.controller.restore();const before=JSON.stringify(p.state.batch);
    await p.controller.reconcile();assert.match(p.state.error,/unavailable/);assert.equal(p.state.reconciling,false);
    assert.equal(JSON.stringify(p.state.batch),before);assert.equal(p.controller.startNew(),false);
    await p.controller.reconcile();assert.equal(queries,2);assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
  }
});
test('explicit reconciliation ignores late account/session responses and never queries offline',async()=>{
  for(const phase of ['query','get']){
    const batch=stoppedBatch(),wait=deferred();let gets=0;
    const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:()=>phase==='query'?wait.promise:Promise.resolve(),
      getTodayBatch:()=>{gets++;return wait.promise;}});
    await p.controller.restore();const before=JSON.stringify(p.state.batch);
    const task=p.controller.reconcile();await Promise.resolve();
    p.context.accountId='new';p.context.sessionKey++;wait.resolve({...batch,segments:[]});await task;
    assert.equal(JSON.stringify(p.state.batch),before);if(phase==='query')assert.equal(gets,0);
  }
  let queried=0;const batch=stoppedBatch(),p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async()=>{queried++;}});
  await p.controller.restore();p.context.offline=true;await p.controller.reconcile();
  assert.equal(queried,0);assert.match(p.state.error,/实时登录/);
});
test('late hours produce no invalid default interval and block preview without HTTP',async()=>{
  for(const text of ['2026-11-06T18:00:00+08:00','2026-11-06T20:05:00+08:00','2026-11-07T23:40:00+08:00']){
    const at=Date.parse(text),fresh=feature.freshTodaySegments(policy,['1'],at);
    assert.deepEqual(fresh.segments,[]);
    const p=setup();p.setNow(at);p.controller.reset();
    assert.equal(p.controller.availability().available,false);await p.controller.prepare();
    assert.match(p.state.error,/今天已无可预约时段/);assert.deepEqual(p.calls,[]);
  }
  for(const text of ['2026-11-06T17:59:59+08:00','2026-11-07T19:59:59+08:00']){
    const at=Date.parse(text),fresh=feature.freshTodaySegments(policy,['1'],at);
    assert.equal(fresh.segments.length,1);assert.doesNotThrow(()=>feature.validateTodaySegments(fresh,policy,at));
  }
});
test('a draft survives closing time and midnight while its prior batch remains readable',async()=>{
  const batch=stoppedBatch(),p=setup({latestTodayBatch:async()=>({batch}),getTodayBatch:async()=>batch});
  p.controller.setDraft(draft);await p.controller.restore();const before=JSON.stringify(p.state.draft);
  p.setNow(Date.parse('2026-11-02T23:40:00+08:00'));
  assert.equal(p.controller.availability().available,false);await p.controller.poll();
  p.setNow(Date.parse('2026-11-03T00:01:00+08:00'));
  assert.equal(p.controller.availability().available,false);assert.match(p.controller.availability().message,/日期已变化/);
  await p.controller.poll();assert.equal(JSON.stringify(p.state.draft),before);assert.equal(p.state.batch.batch_id,batch.batch_id);
});
test('an older local poll cannot replace the explicit verification result',async()=>{
  const batch=stoppedBatch(),old=deferred();let gets=0;
  const verified={...batch,segments:batch.segments.map((s,index)=>({...s,status:index?'not_submitted':'success'}))};
  const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async()=>{},
    getTodayBatch:async()=>++gets===1?old.promise:verified});
  await p.controller.restore();const prior=p.controller.poll();
  await p.controller.reconcile();assert.equal(p.state.batch.segments[0].status,'success');
  old.resolve(batch);await prior;
  assert.equal(p.state.batch.segments[0].status,'success');assert.equal(p.state.batch.segments[1].status,'not_submitted');
});
test('explicit verification observes the external disabled guard and explains an unmatched receipt',async()=>{
  let queries=0;const batch=stoppedBatch(),p=setup({latestTodayBatch:async()=>({batch}),
    reconcileRun:async()=>{queries++;return {result:{status:'uncertain'}};},getTodayBatch:async()=>batch});
  await p.controller.restore();p.context.disabled=true;await p.controller.reconcile();
  assert.equal(queries,0);assert.equal(p.state.reconciling,false);
  p.context.disabled=false;await p.controller.reconcile();assert.equal(queries,1);
  assert.match(p.state.error,/仍有时段待核实/);assert.equal(p.controller.startNew(),false);
});
test('verification after midnight targets the original manual run without a today-wide query',async()=>{
  const batch=stoppedBatch(),calls=[];
  const verified={...batch,segments:batch.segments.map((s,index)=>({...s,status:index?'not_submitted':'success'}))};
  const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async key=>{calls.push(key);return {result:{status:'success'}};},
    queryRecords:async()=>{throw new Error('A broad current-day query cannot verify yesterday');},getTodayBatch:async()=>verified});
  await p.controller.restore();const before=JSON.stringify(p.state.draft);
  p.setNow(Date.parse('2026-11-03T00:01:00+08:00'));await p.controller.reconcile();
  assert.deepEqual(calls,[batch.segments[0].run_id]);assert.equal(p.state.batch.segments[0].status,'success');
  assert.equal(p.state.batch.segments[1].status,'not_submitted');assert.equal(JSON.stringify(p.state.draft),before);
  assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
});
test('every segment verification checks the account session and revision before the next read',async()=>{
  for(const field of ['accountId','sessionKey','revision'])for(const phase of [0,1]){
    const batch=stoppedBatch(),wait=deferred(),calls=[];batch.segments[1].status='uncertain';
    const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async key=>{calls.push(key);if(calls.length===phase+1)await wait.promise;},
      getTodayBatch:async()=>{calls.push('get');return batch;}});
    await p.controller.restore();const original=JSON.stringify(p.state.batch),task=p.controller.reconcile();
    await Promise.resolve();
    if(field==='accountId')p.context[field]='another';else p.context[field]++;
    wait.resolve({result:{status:'uncertain'}});await task;
    assert.deepEqual(calls,batch.segments.slice(0,phase+1).map(s=>s.run_id));assert.equal(JSON.stringify(p.state.batch),original);
  }
});
test('multiple uncertain runs are verified in order and followed by one local batch read',async()=>{
  const batch=stoppedBatch(),calls=[];batch.segments[1].status='uncertain';
  const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async key=>{calls.push(key);return {result:{status:'uncertain'}};},
    getTodayBatch:async()=>{calls.push('get');return batch;}});
  await p.controller.restore();await p.controller.reconcile();
  assert.deepEqual(calls,[...batch.segments.map(s=>s.run_id),'get']);
  assert.match(p.state.error,/仍有时段待核实/);assert.equal(p.controller.startNew(),false);
  assert.equal(p.calls.filter(c=>c[0]==='submit').length,0);
});
test('invalid or foreign manual-run bindings fail before any school read',async()=>{
  for(const invalid of [undefined,'','foreign:2026-11-02:manual:'+'1'.repeat(32),'old:2026-11-03:manual:'+'1'.repeat(32),'old:2026-11-02:scheduled:'+'1'.repeat(32)]){
    let reads=0;const batch=stoppedBatch();batch.segments[0].run_id=invalid;
    const p=setup({latestTodayBatch:async()=>({batch}),reconcileRun:async()=>{reads++;},queryRecords:async()=>{reads++;},getTodayBatch:async()=>{reads++;return batch;}});
    await p.controller.restore();const before=JSON.stringify(p.state.batch);await p.controller.reconcile();
    assert.equal(reads,0);assert.match(p.state.error,/运行记录/);assert.equal(JSON.stringify(p.state.batch),before);
    assert.equal(p.state.reconciling,false);assert.equal(p.controller.startNew(),false);
  }
});
