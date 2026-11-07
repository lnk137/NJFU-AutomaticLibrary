import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as feature from '../../AL-FrontEnd/services/today_segments.mjs';
const time=Date.parse('2026-11-02T08:10:00+08:00');
function restoredPicker(status='completed',segmentStatus=status==='running'?'pending':'success',date='2026-11-02'){
  const draft={date,segments:[{begin:'16:00',end:'19:00',seat_ids:['old-seat']}]};
  const props={account:{id:'one',revision:1,plan:{seat_ids:['base-seat']}},revision:1,policy:{open:'07:30',close:'22:00',friday_close:'20:00'},seats:[{id:'old-seat',name:'Old'},{id:'new-seat',name:'New'}],sessionKey:1,allowLive:true,
    editor:{account_id:'one',...draft,baseline:JSON.stringify(draft),dirty:true,seat_index:0,pending_seats:['new-seat'],seat_baseline:JSON.stringify(['old-seat']),library_editor:null}};
  const batch={batch_id:'older',date:draft.date,state:status,segments:[{...draft.segments[0],run_id:`one:${draft.date}:manual:${'1'.repeat(32)}`,status:segmentStatus}]};
  const api={latestTodayBatch:async()=>({batch}),reconcileRun:async()=>{},getTodayBatch:async()=>({...batch,segments:batch.segments.map(s=>({...s,status:'success'}))})};
  const source=readFileSync(new URL('../../AL-FrontEnd/components/TodaySegments.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const events=[];
  const FixtureDate=class extends Date{constructor(...args){super(...(args.length?args:[time]));}static now(){return time;}};
  const context={...feature,Date:FixtureDate,createTodayBatchController:options=>feature.createTodayBatchController({...options,now:()=>time}),api,defineProps:()=>props,defineEmits:()=>((...args)=>events.push(args)),ref:value=>({value}),reactive:value=>value,computed:fn=>({get value(){return fn();}}),watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},setInterval,clearInterval};
  vm.runInNewContext(source+'\nglobalThis.subject={state,seatIndex,pendingSeats,seatError,controller,applySeats};',context);
  return {...context.subject,events};
}
test('applying an owned seat draft after old terminal result preserves its original interval',async()=>{
  for(const status of ['completed','stopped']){
    const p=restoredPicker(status);await p.controller.restore();p.applySeats();
    assert.deepEqual([...p.state.draft.segments[0].seat_ids],['new-seat']);
    assert.equal(p.state.draft.segments[0].begin,'16:00');assert.equal(p.state.draft.segments[0].end,'19:00');
    assert.equal(p.seatIndex.value,null);assert.deepEqual([...p.pendingSeats.value],[]);
    assert.equal(p.controller.editor().dirty,true);assert.equal(p.controller.editor().pending,null);
    const last=p.events.filter(e=>e[0]==='update:editor').at(-1)[1];
    assert.equal(last.seat_index,null);assert.equal(last.dirty,true);
  }
});
test('an active batch rejects seat application without clearing the saved picker',async()=>{
  const p=restoredPicker('running');await p.controller.restore();p.applySeats();
  assert.deepEqual([...p.pendingSeats.value],['new-seat']);assert.equal(p.seatIndex.value,0);
  assert.deepEqual([...p.state.draft.segments[0].seat_ids],['old-seat']);assert.match(p.seatError.value,/批次|执行|核对/);
});
test('an uncertain batch retains an unapplied picker through explicit verification',async()=>{
  const p=restoredPicker('stopped','uncertain');await p.controller.restore();p.applySeats();
  assert.equal(p.seatIndex.value,0);assert.deepEqual([...p.pendingSeats.value],['new-seat']);
  assert.deepEqual([...p.state.draft.segments[0].seat_ids],['old-seat']);
  await p.controller.reconcile();
  assert.equal(p.seatIndex.value,0);assert.deepEqual([...p.pendingSeats.value],['new-seat']);
  assert.deepEqual([...p.state.draft.segments[0].seat_ids],['old-seat']);
  p.applySeats();assert.equal(p.seatIndex.value,null);
  assert.deepEqual([...p.state.draft.segments[0].seat_ids],['new-seat']);
  assert.equal(p.state.draft.segments[0].begin,'16:00');assert.equal(p.state.draft.segments[0].end,'19:00');
});
test('an older date cannot restore a picker against a new days interval',()=>{
  const p=restoredPicker('completed','success','2026-11-01');
  assert.equal(p.seatIndex.value,null);assert.deepEqual([...p.pendingSeats.value],[]);
  assert.equal(p.state.draft.date,'2026-11-02');
});
test('an orphaned picker cannot apply to a removed interval or throw',()=>{
  const p=restoredPicker();p.state.draft.segments=[];
  assert.doesNotThrow(()=>p.applySeats());assert.match(p.seatError.value,/时段|草稿/);
  assert.deepEqual([...p.pendingSeats.value],['new-seat']);
});
