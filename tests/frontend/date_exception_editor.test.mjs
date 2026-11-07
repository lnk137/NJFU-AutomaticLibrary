import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
import * as helpers from '../../AL-FrontEnd/services/date_exceptions.mjs';
import {transformViteAssets} from './sfc_test_helpers.mjs';

const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900};
const account={id:'a',revision:2,plan:{seat_ids:['1'],mode:'day',time_mode:'custom',day_offset:1,begin:'09:00',end:'12:00',trigger:'07:00:00',enabled:true},date_exceptions:{}};
const seats=[{id:'1',name:'A001',location:'2F'},{id:'2',name:'A002',location:'2F'}];
const deferred=()=>{let resolve;const promise=new Promise(a=>resolve=a);return {promise,resolve};};
function editor(api={},changes={},clock=Date){
  const props=Vue.reactive({account:structuredClone(account),policy,seats,sessionKey:1,disabled:false,modelValue:null,...changes});
  const events=[],hooks={};let discard=true;
  const emit=(name,value)=>{events.push([name,value]);if(name==='update:modelValue')props.modelValue=value;if(name==='refreshed')props.account=value;};
  const source=readFileSync(new URL('components/DateExceptions.vue',frontend),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const context={...Vue,...drafts,...helpers,Date:clock,api,defineProps:()=>props,defineEmits:()=>emit,
    onBeforeUnmount:f=>hooks.unmount=f,uni:{showModal:o=>o.success({confirm:discard})}};
  const scope=Vue.effectScope();scope.run(()=>vm.runInNewContext(source+'\nglobalThis.subject={openEditor,change,saveEditor,closeEditor,skipNext,restore,pickSeat,moveSeat,removeSeat,error,message,busy,addSegment,editSegment,selectSegment,removeSegment,staleDraft,validationError,reloadEditor};',context));
  return {p:context.subject,props,events,scope,hooks,setDiscard:value=>discard=value};
}
for(const action of ['override','skip_range'])test('reopening '+action+' after release uses the current next date without retiming an open draft',async()=>{
  let now=Date.parse('2026-11-07T06:59:00+08:00');
  class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const e=editor({}, {},Clock),key=action==='override'?'date':'start_date';
  try{
    e.p.openEditor(action);assert.equal(e.props.modelValue[key],'2026-11-08');
    now=Date.parse('2026-11-07T07:01:00+08:00');await Vue.nextTick();
    assert.equal(e.props.modelValue[key],'2026-11-08');
    await e.p.closeEditor();e.p.openEditor(action);
    assert.equal(e.props.modelValue[key],'2026-11-09');
  }finally{e.scope.stop();}
});
test('saving one-day settings submits ordered seats and preserves the base plan',async()=>{
  let sent;const e=editor({updateDateExceptions:async(id,data)=>{sent={id,data};return {account:{...account,revision:3,date_exceptions:{'2099-01-01':{action:'skip'}}}};}});
  e.p.openEditor('override');e.p.change('date','2099-01-01');e.p.change('begin','14:00');e.p.change('end','18:00');e.p.pickSeat('2');e.p.moveSeat(1,-1);
  await e.p.saveEditor();assert.equal(sent.id,'a');assert.equal(sent.data.expected_revision,2);
  assert.equal(sent.data.date,'2099-01-01');assert.deepEqual([...sent.data.seat_ids],['2','1']);
  assert.equal(e.props.account.plan.begin,'09:00');assert.equal(e.props.modelValue,null);
  assert.equal(e.events.filter(([name])=>name==='saved').length,1);e.scope.stop();
});
test('an old save response is ignored after account or session changes',async()=>{
  for(const kind of ['account','session']){
    const wait=deferred(),e=editor({updateDateExceptions:()=>wait.promise});const operation=e.p.skipNext();
    if(kind==='account')e.props.account={...account,id:'b'};else e.props.sessionKey++;
    await Vue.nextTick();wait.resolve({account:{...account,revision:3}});await operation;
    assert.equal(e.events.filter(([name])=>name==='saved').length,0);assert.equal(e.p.error.value,'');e.scope.stop();
  }
});
test('an idle editor changing account does not clear another operation busy state',async()=>{
  const e=editor();e.props.account={...account,id:'b'};await Vue.nextTick();
  assert.equal(e.events.filter(([name])=>name==='busy').length,0);e.scope.stop();
});
test('discarding an edited exception is explicit and can be declined',async()=>{
  const e=editor();e.p.openEditor('skip_range');e.p.change('end_date','2099-01-01');e.setDiscard(false);
  await e.p.closeEditor();assert.ok(e.props.modelValue);e.setDiscard(true);await e.p.closeEditor();
  assert.equal(e.props.modelValue,null);e.scope.stop();
});
test('nested personal-library editing is protected without pretending the date changed',async()=>{
  let calls=0;const e=editor({updateDateExceptions:async()=>{calls++;}});e.p.openEditor('override');
  e.props.modelValue.libraryEditor={account_id:'a',kind:'tags',text:'保留输入',dirty:true};
  e.setDiscard(false);await e.p.closeEditor();assert.ok(e.props.modelValue);
  await e.p.saveEditor();assert.equal(calls,0);assert.equal(e.props.modelValue.dirty,false);e.scope.stop();
});
test('disabled or already busy editor cannot submit another request',async()=>{
  let requests=0;const wait=deferred(),e=editor({updateDateExceptions:()=>{requests++;return wait.promise;}},{disabled:true});
  await e.p.skipNext();assert.equal(requests,0);e.props.disabled=false;
  const operation=e.p.skipNext();await e.p.skipNext();assert.equal(requests,1);
  wait.resolve({account:{...account,revision:3}});await operation;e.scope.stop();
});
test('consecutive skip dates render as one leave range without swallowing an override',()=>{
  const rows=helpers.dateExceptionRows({'2026-10-25':{action:'skip'},'2026-10-26':{action:'skip'},
    '2026-10-27':{action:'override',begin:'14:00',end:'18:00',seat_ids:['2']},'2026-10-28':{action:'skip'}});
  assert.equal(rows.length,3);assert.equal(rows[0].start_date,'2026-10-25');assert.equal(rows[0].end_date,'2026-10-26');
  assert.equal(rows[1].action,'override');assert.equal(rows[2].start_date,'2026-10-28');
});
async function component(name,dependencies={}){
  const {descriptor}=parse(readFileSync(new URL('components/'+name+'.vue',frontend),'utf8'));
  let code=transformViteAssets(compileScript(descriptor,{id:name,inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content);
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  for(const [path,url] of Object.entries(dependencies))code=code.replaceAll(JSON.stringify(path),JSON.stringify(url)).replaceAll("'"+path+"'",JSON.stringify(url));
  const url='data:text/javascript;base64,'+Buffer.from(code).toString('base64');return {url,value:(await import(url)).default};
}
test('real component shows saved date rules and the three simple actions',async()=>{
  const icon=await component('AppIcon'),dialog=await component('AppDialog',{'@/components/AppIcon.vue':icon.url});
  const library=await component('SeatLibrary');
  const panel=await component('DateExceptions',{'@/components/AppDialog.vue':dialog.url,'@/components/AppIcon.vue':icon.url,'@/components/SeatLibrary.vue':library.url});
  const html=await renderToString(Vue.createSSRApp(panel.value,{account:{...account,date_exceptions:{'2099-01-01':{action:'override',begin:'14:00',end:'18:00',seat_ids:['2']}}},policy,seats,sessionKey:1}));
  assert.match(html,/跳过下次/);assert.match(html,/请假几天/);assert.match(html,/只改某天/);
  assert.match(html,/2099-01-01/);assert.match(html,/14:00/);assert.match(html,/A002/);
});

test('date edits preserve every segment and ordered seats through save and reopen',async()=>{
  const segments=[{id:'am',begin:'09:00',end:'12:00',seat_ids:['1']},{id:'pm',begin:'14:00',end:'18:00',seat_ids:['2','1']}];
  const saved={...account,plan:{...account.plan,segments}},model=helpers.calendarDateEditor('2099-01-05',saved,policy);
  let sent;const e=editor({updateDateExceptions:async(id,data)=>{sent=data;return {account:{...saved,revision:3,date_exceptions:{[data.date]:{action:'override',segments:data.segments}}}};}},{account:saved,modelValue:model});
  await e.p.selectSegment(1);e.p.moveSeat(1,-1);e.p.editSegment(1,'begin','15:00');
  assert.deepEqual([...e.props.modelValue.segments[0].seat_ids],['1']);assert.deepEqual([...e.props.modelValue.segments[1].seat_ids],['1','2']);
  await e.p.saveEditor();assert.equal(sent.segments.length,2);assert.deepEqual([...sent.segments].map(s=>s.id),['am','pm']);assert.equal(sent.begin,undefined);
  const refreshed=e.events.find(([name])=>name==='saved')[1];const again=helpers.calendarDateEditor('2099-01-05',refreshed,policy);
  assert.equal(again.segments[1].begin,'15:00');assert.deepEqual(saved.plan.segments,segments);e.scope.stop();
});
test('calendar editing preserves scalar single overrides and explicit one-segment payloads',async()=>{
  for(const segmented of [false,true]){
    const rule=segmented?{action:'override',segments:[{id:'retained',begin:'14:00',end:'18:00',seat_ids:['2','1']}]}:{action:'override',begin:'14:00',end:'18:00',seat_ids:['2','1']};
    const saved={...account,date_exceptions:{'2099-01-05':rule}},model=helpers.calendarDateEditor('2099-01-05',saved,policy);let sent;
    const e=editor({updateDateExceptions:async(id,data)=>{sent=data;return {account:{...saved,revision:3}};}},{account:saved,modelValue:model});
    e.p.editSegment(0,'begin','15:00');await e.p.saveEditor();
    if(segmented){assert.equal(sent.segments[0].id,'retained');assert.equal(sent.begin,undefined);}else{assert.equal(sent.begin,'15:00');assert.equal(sent.segments,undefined);}
    e.scope.stop();
  }
  const model=helpers.calendarDateEditor('2099-01-05',account,policy);assert.equal(model.segment_payload,false);
});
test('date editor add/delete, overlap and Friday guards keep invalid edits local',async()=>{
  let writes=0;const e=editor({updateDateExceptions:async()=>{writes++;}});e.p.openEditor('override');e.p.change('date','2099-01-02');
  e.p.addSegment();assert.equal(e.props.modelValue.segments.length,2);
  e.p.editSegment(1,'begin','11:00');await e.p.saveEditor();assert.match(e.p.error.value,/重叠/);assert.equal(writes,0);
  e.p.editSegment(1,'begin','19:00');e.p.editSegment(1,'end','22:00');await e.p.saveEditor();assert.match(e.p.error.value,/周五/);assert.equal(writes,0);
  await e.p.removeSegment(1);assert.equal(e.props.modelValue.segments.length,1);e.scope.stop();
});
test('same-account refresh never upgrades an old date draft CAS revision',async()=>{
  let writes=0;const e=editor({updateDateExceptions:async()=>{writes++;}});e.p.openEditor('override');e.p.change('date','2099-01-05');e.p.change('begin','10:00');e.p.change('end','14:00');
  e.props.account={...account,revision:3,date_exceptions:{'2099-01-05':{action:'override',begin:'16:00',end:'20:00',seat_ids:['2']}}};
  await Vue.nextTick();await e.p.saveEditor();assert.equal(writes,0);assert.equal(e.props.modelValue.base_revision,2);assert.equal(e.props.modelValue.begin,'10:00');assert.match(e.p.error.value,/配置已变化/);e.scope.stop();
});
test('server CAS conflict preserves all date segments for explicit reread or cancellation',async()=>{
  const conflict=Object.assign(new Error('account changed'),{code:'conflict'});
  const e=editor({updateDateExceptions:async()=>{throw conflict;}});e.p.openEditor('override');e.p.change('date','2099-01-05');e.p.addSegment();
  const before=JSON.stringify(e.props.modelValue);await e.p.saveEditor();
  assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(e.p.error.value,'account changed');assert.equal(e.p.busy.value,false);
  e.setDiscard(false);await e.p.closeEditor();assert.equal(e.props.modelValue.segments.length,2);
  e.setDiscard(true);await e.p.closeEditor();assert.equal(e.props.modelValue,null);e.scope.stop();
});
test('a legacy cached date draft without a base revision cannot inherit the current account revision',async()=>{
  let writes=0;const e=editor({updateDateExceptions:async()=>{writes++;}});e.p.openEditor('override');delete e.props.modelValue.base_revision;
  await e.p.saveEditor();assert.equal(writes,0);assert.match(e.p.error.value,/配置已变化/);e.scope.stop();
});
test('conflict reload needs explicit discard and reads the latest rule and baseline',async()=>{
  const latest={...account,revision:3,date_exceptions:{'2099-01-05':{action:'override',begin:'16:00',end:'20:00',seat_ids:['2']}}};let reads=0;
  const e=editor({listAccounts:async()=>{reads++;return {accounts:[latest]};}});e.p.openEditor('override');e.p.change('date','2099-01-05');e.p.change('begin','10:00');
  e.setDiscard(false);await e.p.reloadEditor();assert.equal(reads,0);assert.equal(e.props.modelValue.begin,'10:00');
  e.setDiscard(true);await e.p.reloadEditor();assert.equal(e.props.modelValue.begin,'16:00');assert.equal(e.props.modelValue.base_revision,3);assert.equal(e.props.modelValue.dirty,false);e.scope.stop();
});
test('dual capability blocks multi submit without truncating a saved draft',async()=>{
  let sent;const e=editor({updateDateExceptions:async(id,data)=>{sent=data;return {account:{...account,revision:3}};}},{account:{...account,capabilities:{date_override_segments:false}}});
  e.p.openEditor('override');e.p.change('date','2099-01-05');e.p.addSegment();assert.equal(e.props.modelValue.segments.length,1);await e.p.saveEditor();assert.equal(sent.begin,'09:00');assert.equal(sent.segments,undefined);
  e.p.openEditor('override');e.props.modelValue.segments.push({id:'extra',begin:'14:00',end:'18:00',seat_ids:['2']});sent=null;await e.p.saveEditor();assert.equal(sent,null);assert.equal(e.props.modelValue.segments.length,2);assert.match(e.p.error.value,/双机/);e.scope.stop();
});
test('switching segments protects nested library drafts',async()=>{
  const e=editor();e.p.openEditor('override');e.p.addSegment();e.props.modelValue.libraryEditor={dirty:true,text:'keep'};e.setDiscard(false);
  await e.p.selectSegment(1);assert.equal(e.props.modelValue.seat_index,0);await e.p.removeSegment(1);assert.equal(e.props.modelValue.segments.length,2);
  e.setDiscard(true);await e.p.selectSegment(1);assert.equal(e.props.modelValue.seat_index,1);assert.equal(e.props.modelValue.libraryEditor,null);e.scope.stop();
});
