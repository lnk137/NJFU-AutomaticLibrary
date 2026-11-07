import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as feature from '../../AL-FrontEnd/services/scheduled_segments.mjs';
import {planWindowError,nextPlanWindow,createDraft,isDirty,persistableDraft} from '../../AL-FrontEnd/services/drafts.mjs';
const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900,day_offset:1,booking_open:'07:00:00'};
const scalar={mode:'day',time_mode:'custom',begin:'09:00',end:'12:00',seat_ids:['1','2'],trigger:'07:00:00',enabled:true,day_offset:1,week_times:{}};
const intervals=()=>[{id:'morning',begin:'09:00',end:'12:00',seat_ids:['1','2']},{id:'afternoon',begin:'12:00',end:'15:00',seat_ids:['2']}];
const multi=()=>({...scalar,segments:intervals(),week_segments:{}});
test('explicit conversion copies legacy plan and preserves ordered seats',()=>{
 const next=feature.segmentedPlan(scalar);assert.equal(scalar.segments,undefined);assert.deepEqual(next.segments[0].seat_ids,['1','2']);
 next.segments[0].seat_ids.reverse();assert.deepEqual(scalar.seat_ids,['1','2']);
});
test('nonoverlap, adjacency, weekly windows and Friday validation share save guard',()=>{
 assert.equal(planWindowError(multi(),policy),'');const p=multi();p.segments[1].begin='11:00';assert.match(planWindowError(p,policy),/重叠/);
 p.segments=[{id:'late',begin:'19:00',end:'22:00',seat_ids:['1']}];assert.match(planWindowError(p,policy),/周五/);
 p.mode='week';p.week_segments={'1':p.segments};assert.equal(planWindowError(p,policy),'');p.week_segments={'5':p.segments};assert.match(planWindowError(p,policy),/周五/);
});
test('preview uses all actual target-day intervals with skip and replacement override',()=>{
 const now=new Date('2026-10-24T06:00:00+08:00');let preview=nextPlanWindow(multi(),policy,now,{});
 assert.equal(preview.date,'2026-10-25');assert.equal(preview.segments.length,2);assert.deepEqual(preview.segments[1].seat_ids,['2']);
 preview=nextPlanWindow(multi(),policy,now,{'2026-10-25':{action:'skip'}});assert.equal(preview.date,'2026-10-26');
 preview=nextPlanWindow(multi(),policy,now,{'2026-10-25':{action:'override',begin:'14:00',end:'17:00',seat_ids:['2']}});assert.equal(preview.segments.length,1);assert.equal(preview.begin,'14:00');
});
test('multi plan drafts survive page persistence and keep dirty status',()=>{
 const draft=createDraft({student_id:'one',revision:1,plan:multi()},policy);assert.equal(isDirty(draft),false);
 draft.plan.segments[1].seat_ids=['1'];const persisted=persistableDraft(draft);assert.equal(isDirty(persisted),true);assert.deepEqual(persisted.plan.segments[1].seat_ids,['1']);
});
function component(){
 const props={account:{id:'one'},sessionKey:1,modelValue:{plan:multi(),day:'1',seat_index:null,search:'',library_editor:null},seats:[{id:'1',name:'A001'},{id:'2',name:'A002'}],policy,disabled:false};
 const events=[];let confirm=true,pendingConfirm=null;
 const source=readFileSync(new URL('../../AL-FrontEnd/components/ScheduledSegments.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
 const context={...feature,uni:{showModal:options=>{if(confirm===null)pendingConfirm=options;else options.success({confirm});}},defineProps:()=>props,defineEmits:()=>((...args)=>{events.push(args);if(args[0]==='update:modelValue')props.modelValue=args[1];}),computed:fn=>({get value(){return fn();}}),ref:value=>({value})};
 vm.runInNewContext(source+'\nglobalThis.subject={edit,add,remove,pick,move,toggleDay,changeDay,toggleSeats,error};',context);return {...context.subject,props,events,setConfirm:value=>confirm=value,resolveConfirm:value=>pendingConfirm.success({confirm:value})};
}
test('each segment seat selection and priority edits remain independent',async()=>{
 const c=component();c.pick(1,'1');assert.deepEqual([...c.props.modelValue.plan.segments[1].seat_ids],['2','1']);
 c.move(1,1,-1);assert.deepEqual([...c.props.modelValue.plan.segments[1].seat_ids],['1','2']);
 assert.deepEqual([...c.props.modelValue.plan.segments[0].seat_ids],['1','2']);await c.remove(1);assert.equal(c.props.modelValue.plan.segments.length,1);
});
test('disabled editors cannot mutate plan or seat drafts',()=>{
 const c=component();c.props.disabled=true;const before=JSON.stringify(c.props.modelValue);c.edit(0,'begin','10:00');c.add();c.remove(0);c.pick(0,'2');assert.equal(JSON.stringify(c.props.modelValue),before);
});

test('persisted weekly plan with inactive daily array can switch back to an editable day',()=>{
 const plan={...scalar,mode:'week',segments:[],week_segments:{'1':intervals()}};const day=feature.segmentedPlan(plan,'day');assert.equal(day.segments.length,2);assert.deepEqual(day.segments[1].seat_ids,['2']);
});
test('malformed stored segment lists produce visible errors instead of crashing preview',()=>{
 for(const values of [[null],[{id:'broken',begin:null}]]){const plan={...multi(),segments:values};assert.ok(planWindowError(plan,policy));assert.equal(nextPlanWindow(plan,policy,new Date('2026-10-24T06:00:00+08:00')).available,false);}
});

test('dirty library edits require explicit discard before changing segment, closing it, deleting or switching weekday',async()=>{
 for(const action of [c=>c.toggleSeats(1),c=>c.toggleSeats(0),c=>c.remove(1),c=>c.changeDay('2'),c=>c.toggleDay()]){
  const c=component();c.props.modelValue.seat_index=0;c.props.modelValue.library_editor={kind:'tags',text:'保留我的输入',dirty:true};
  const before=JSON.stringify(c.props.modelValue);c.setConfirm(false);assert.equal(await action(c),false);assert.equal(JSON.stringify(c.props.modelValue),before);
  c.setConfirm(true);assert.equal(await action(c),true);assert.equal(c.props.modelValue.library_editor,null);
 }
});
test('late discard confirmations cannot clear another account, session or changed editor',async()=>{
 for(const change of [c=>c.props.account.id='two',c=>c.props.sessionKey++,c=>c.props.modelValue.library_editor.text='新的内容']){
  const c=component();c.props.modelValue.seat_index=0;c.props.modelValue.library_editor={text:'原始内容',dirty:true};c.setConfirm(null);
  const pending=c.changeDay('2');change(c);const latest=JSON.stringify(c.props.modelValue);c.resolveConfirm(true);assert.equal(await pending,false);assert.equal(JSON.stringify(c.props.modelValue),latest);
 }
});
