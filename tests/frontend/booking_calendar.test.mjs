import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import * as calendar from '../../AL-FrontEnd/services/booking_calendar.mjs';

const range=calendar.calendarRange('2026-11-06');
const payload=(account='a',window=range)=>({account_id:account,...window,warnings:[],days:Array.from({length:(Date.parse(window.end)-Date.parse(window.start))/86400000+1},(_,i)=>({date:calendar.shiftCalendarDate(window.start,i),entries:[]}))});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

test('Beijing dates and fixed month/week grids cross month and year boundaries',()=>{
  assert.equal(calendar.beijingDate(new Date('2026-11-05T16:00:00Z')),'2026-11-06');
  assert.deepEqual(range,{start:'2026-10-26',end:'2026-12-06'});
  assert.deepEqual(calendar.calendarRange('2027-01-01','week'),{start:'2026-12-28',end:'2027-01-03'});
  assert.equal(calendar.moveCalendarAnchor('2026-12-31','month',1),'2027-01-01');
  assert.equal(calendar.moveCalendarAnchor('2027-01-01','week',-1),'2026-12-25');
  for(const invalid of ['2026-2-01','2026-02-30','not-date'])assert.throws(()=>calendar.calendarRange(invalid));
});
test('incomplete or misordered calendar responses fail visibly instead of becoming empty',()=>{
  assert.throws(()=>calendar.calendarDays({...payload(),days:[]},range),/不完整/);
  const incomplete=payload();incomplete.days[2].date=incomplete.days[1].date;
  assert.throws(()=>calendar.calendarDays(incomplete,range),/不完整/);
  const value=payload();value.days[0].entries=[{state:'confirmed'},{state:'planned'},{state:'unknown'},{state:'cancelled'}];
  assert.deepEqual(calendar.calendarDays(value,range)[0].states,['confirmed','planned','uncertain','cancelled']);
});
test('account switch and session replacement ignore old responses and errors',async()=>{
  let owner={accountId:'a',sessionKey:1},applied=[],errors=[];const requests=[];
  const loader=calendar.createCalendarLoader({read:(...args)=>{const task=deferred();requests.push({task,args});return task.promise;},context:()=>owner,apply:(...args)=>applied.push(args),failure:error=>errors.push(error)});
  const first=loader.load(range);owner={accountId:'b',sessionKey:1};const second=loader.load(range);
  requests[0].task.resolve(payload('a'));await first;assert.equal(applied.length,0);
  requests[1].task.resolve(payload('b'));await second;assert.equal(applied[0][0].account_id,'b');
  const third=loader.load(range);owner={accountId:'b',sessionKey:2};requests[2].task.reject(new Error('old private failure'));await third;
  assert.equal(applied.length,1);assert.ok(!errors.includes('old private failure'));
});
test('newer range refresh wins and page hide invalidates pending response',async()=>{
  const requests=[],applied=[];let pending=false;
  const loader=calendar.createCalendarLoader({read:()=>{const task=deferred();requests.push(task);return task.promise;},context:()=>({accountId:'a',sessionKey:1}),apply:value=>applied.push(value),pending:value=>{pending=value;}});
  const first=loader.load(range),week=calendar.calendarRange('2026-11-06','week'),second=loader.load(week);
  requests[1].resolve(payload('a',week));await second;requests[0].resolve(payload());await first;
  assert.equal(applied.length,1);assert.equal(applied[0].end,week.end);assert.equal(pending,false);
  const hidden=loader.load(range);loader.invalidate();requests[2].resolve(payload());await hidden;
  assert.equal(applied.length,1);assert.equal(pending,false);
});
test('failed reads can retry and foreign payloads are rejected',async()=>{
  const results=[new Error('offline'),payload('foreign'),payload()],applied=[],errors=[];
  const loader=calendar.createCalendarLoader({read:async()=>{const next=results.shift();if(next instanceof Error)throw next;return next;},context:()=>({accountId:'a',sessionKey:1}),apply:value=>applied.push(value),failure:value=>errors.push(value)});
  await loader.load(range);assert.equal(errors.at(-1),'offline');
  await loader.load(range);assert.match(errors.at(-1),/账号不匹配/);
  await loader.load(range);assert.equal(applied.at(-1).account_id,'a');assert.equal(errors.at(-1),'');
});

function component(role='student',overrides={},currentDate=()=>calendar.beijingDate()){
  const props={accounts:[{id:'a',alias:'One',revision:3},{id:'b',alias:'Two',revision:5}],activeAccountId:'a',sessionKey:1,role,visible:true,seatMap:{1:{name:'A001'}},initialDate:'2026-11-06',...overrides};
  const script=readFileSync(new URL('../../AL-FrontEnd/components/BookingCalendar.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const calls=[],watches=[],unmounts=[];
  const context={...calendar,beijingDate:currentDate,defineProps:()=>props,defineEmits:()=>{},defineExpose:()=>{},ref:value=>({value}),computed:fn=>({get value(){return fn();}}),watch:(sources,callback,options)=>{watches.push(callback);if(options?.immediate)callback();},onBeforeUnmount:callback=>unmounts.push(callback),api:{getCalendar:async(id,start,end)=>{calls.push({id,start,end});return payload(id,{start,end});}}};
  vm.runInNewContext(script+'\nglobalThis.subject={accountId,referenceDate,anchor,selected,mode,days,payload,error,loader,chooseAccount,reload,selectedEntries,today};',context);
  return {...context.subject,props,calls,watches,unmounts};
}
test('component restricts student to active account and root selection is local to calendar',async()=>{
  const student=component();student.chooseAccount('b');await student.reload();
  assert.equal(student.accountId.value,'a');assert.equal(student.calls.at(-1).id,'a');
  const root=component('root');root.chooseAccount('b');await root.reload();
  assert.equal(root.accountId.value,'b');assert.equal(root.calls.at(-1).id,'b');assert.equal(root.props.activeAccountId,'a');
  assert.equal(root.anchor.value,'2026-11-06');
  root.props.visible=false;const count=root.calls.length;await root.reload();assert.equal(root.calls.length,count);
});
test('offline calendar never calls unsupported routes and clears previous account data',async()=>{
  const view=component('student',{offline:true});await view.reload();
  assert.equal(view.calls.length,0);assert.equal(view.payload.value,null);
  view.props.offline=false;view.watches[0]();await view.reload();
  assert.ok(view.calls.length>0);assert.equal(view.payload.value.account_id,'a');
  view.props.offline=true;view.watches[0]();const count=view.calls.length;await view.reload();
  assert.equal(view.calls.length,count);assert.equal(view.payload.value,null);assert.equal(view.days.value.length,0);
});
test('day details preserve distinct state, seats and hidden account data is cleared on context change',async()=>{
  const view=component();await view.reload();
  const entries=[{id:'plan',state:'planned',begin:'09:00',end:'12:00',seat_ids:['1']},{id:'school',state:'confirmed',begin:'14:00',end:'18:00',seat_ids:['2']}];
  view.days.value.find(day=>day.date==='2026-11-06').entries=entries;
  assert.deepEqual(view.selectedEntries.value,entries);
  view.props.visible=false;view.watches[0]();assert.equal(view.payload.value,null);assert.equal(view.days.value.length,0);
});
test('today and visible resume refresh the Beijing date after midnight without losing the browsed day',()=>{
  let day='2026-11-06';const view=component('student',{initialDate:''},()=>day);
  day='2026-11-07';view.today();
  assert.equal(view.anchor.value,'2026-11-07');assert.equal(view.selected.value,'2026-11-07');assert.equal(view.referenceDate.value,'2026-11-07');
  view.props.visible=false;view.watches[0]();day='2026-11-08';view.props.visible=true;view.watches[0]();
  assert.equal(view.referenceDate.value,'2026-11-08');assert.equal(view.anchor.value,'2026-11-07');assert.equal(view.selected.value,'2026-11-07');
});
test('explicit initialDate keeps the frozen calendar date across midnight and visible resume',()=>{
  let day='2026-11-06';const view=component('student',{initialDate:'2026-11-01'},()=>day);
  day='2026-11-08';view.today();view.props.visible=false;view.watches[0]();view.props.visible=true;view.watches[0]();
  assert.equal(view.referenceDate.value,'2026-11-01');assert.equal(view.anchor.value,'2026-11-01');assert.equal(view.selected.value,'2026-11-01');
});
test('real Vue template renders concise calendar entry and accessible month/week controls',async()=>{
  const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
  const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
  const encoded=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
  const {descriptor}=parse(readFileSync(new URL('components/BookingCalendar.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:'booking-calendar',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/local_api.js\1/g,JSON.stringify(encoded('export const getCalendar=async()=>new Promise(()=>{});')))
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  const Calendar=(await import(encoded(code))).default;
  const html=await renderToString(Vue.createSSRApp(Calendar,{accounts:[{id:'a',student_id:'student1',alias:'One'}],activeAccountId:'a',role:'root',sessionKey:1,initialDate:'2026-11-06'}));
  for(const label of ['预约日历','实际使用日','北京时间','选择日历账号','上一期日历','下一期日历','2026年11月','今天'])assert.ok(html.includes(label),label);
  assert.match(html,/aria-pressed="true"[^>]*>月/);assert.match(html,/aria-pressed="false"[^>]*>周/);
  const home=readFileSync(new URL('pages/index/index.vue',frontend),'utf8');
  assert.match(home,/<BookingCalendar\b/);assert.match(home,/account_info\/account_info\?account_id=/);
  assert.doesNotMatch(home,/updateDateExceptions\(/);
});

test('external refresh reuses the in-flight read for the same account and range',async()=>{
  const view=component();const initial=view.calls.length;await Promise.all([view.reload(),view.reload()]);assert.equal(view.calls.length,initial);
});
