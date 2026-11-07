import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900};
const plan={seat_ids:['1'],day_offset:1,mode:'week',time_mode:'custom',begin:'09:00',end:'12:00',week_times:{1:['09:00','12:00'],3:['09:00','12:00']},trigger:'07:00:00',enabled:true};
function home(exceptions){
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/index/index.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  class FixedDate extends Date{constructor(...args){super(...(args.length?args:['2026-10-24T06:00:00+08:00']));}}
  const store={activeAccount:{id:'a',plan,date_exceptions:exceptions},user:{role:'student'},accounts:[],policy};
  const context={...drafts,Date:FixedDate,ref:value=>({value}),computed:fn=>({get value(){return fn();}}),useWorkspace:()=>store,onShow:()=>{},onHide:()=>{},onUnload:()=>{}};
  vm.runInNewContext(source+'\nglobalThis.result=planWindow.value;',context);return context.result;
}
test('home next-use summary respects long leave instead of showing the base weekday',()=>{
  const exceptions={};for(let day=25;day<=31;day++)exceptions[`2026-10-${day}`]={action:'skip'};
  for(let day=1;day<=13;day++)exceptions[`2026-11-${String(day).padStart(2,'0')}`]={action:'skip'};
  const result=home(exceptions);assert.equal(result.date,'2026-11-16');assert.equal(result.release_date,'2026-11-15');
});
test('home summary uses the explicit off-day interval and ordered seats',()=>{
  const result=home({'2026-10-25':{action:'override',begin:'14:00',end:'18:00',seat_ids:['2','1']}});
  assert.equal(result.date,'2026-10-25');assert.equal(result.begin,'14:00');assert.deepEqual(result.seat_ids,['2','1']);
});
test('calendar adjustment forwards the selected use date only for an accessible account',()=>{
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/index/index.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const routes=[],store={accounts:[{id:'a'}],activeAccount:null,user:{role:'student'},policy};
  const context={...drafts,ref:value=>({value}),computed:fn=>({get value(){return fn();}}),useWorkspace:()=>store,onShow:()=>{},onHide:()=>{},onUnload:()=>{},uni:{navigateTo:({url})=>routes.push(url)}};
  vm.runInNewContext(source+'\nglobalThis.adjust=openCalendarAdjust;',context);
  context.adjust({accountId:'a',date:'2026-11-13'});
  assert.equal(routes[0],'/pages/account_info/account_info?account_id=a&adjust_date=2026-11-13');
  context.adjust({accountId:'unknown',date:'2026-11-14'});assert.equal(routes.length,1);
});
function refreshableHome(){
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/index/index.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  let clock=0;class Clock extends Date{static now(){return clock;}}
  const calls=[],hooks={},store={accounts:[{id:'a'}],activeAccountId:'a',user:{role:'student'},snapshots:{a:{}},policy,
    async refresh(){calls.push('account');},async loadRuns(){calls.push('runs');},invalidate(){calls.push('invalidate');}};
  const context={...drafts,Date:Clock,ref:value=>({value}),computed:fn=>({get value(){return fn();}}),useWorkspace:()=>store,
    onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,onUnload:f=>hooks.unload=f,clearInterval:()=>{},setInterval:f=>{hooks.poll=f;return 1;}};
  vm.runInNewContext(source+'\nglobalThis.subject={refresh,calendarRef,loading,error};',context);
  context.subject.calendarRef.value={async reload(){calls.push('calendar');}};
  return {...context.subject,store,calls,hooks,advance:value=>clock=value};
}
test('overview manual refresh awaits calendar and existing account/run reads together',async()=>{
  const p=refreshableHome();let finish;p.calendarRef.value.reload=()=>{p.calls.push('calendar');return new Promise(resolve=>finish=resolve);};
  const pending=p.refresh();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(p.calls,['account','runs','calendar']);assert.equal(p.loading.value,true);
  finish();await pending;assert.equal(p.loading.value,false);
  p.calendarRef.value.reload=async()=>{throw new Error('calendar read failed');};await p.refresh();
  assert.equal(p.error.value,'calendar read failed');assert.equal(p.loading.value,false);
});
test('overview polling refreshes saved calendar data every 15 seconds and manual refresh remains immediate',async()=>{
  const p=refreshableHome();p.hooks.show();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(p.calls,['account','runs','calendar']);p.calls.length=0;
  p.advance(5000);await p.hooks.poll();assert.deepEqual(p.calls,['account','runs']);p.calls.length=0;
  p.advance(10000);await p.hooks.poll();assert.deepEqual(p.calls,['account','runs']);p.calls.length=0;
  p.advance(15000);await p.hooks.poll();assert.deepEqual(p.calls,['account','runs','calendar']);p.calls.length=0;
  p.advance(16000);await p.refresh();assert.deepEqual(p.calls,['account','runs','calendar']);
  p.hooks.hide();assert.equal(p.calls.at(-1),'invalidate');
});
test('overview refresh cannot continue into another account, session, hidden or unloaded page',async()=>{
  for(const change of ['account','session','hide','unload']){
    const p=refreshableHome();let finish;p.store.refresh=()=>{p.calls.push('account');return new Promise(resolve=>finish=resolve);};
    const pending=p.refresh();
    if(change==='account')p.store.activeAccountId='b';else if(change==='session')p.store.sessionEpoch=2;else p.hooks[change]();
    finish();await pending;assert.equal(p.calls.includes('runs'),false);assert.equal(p.calls.includes('calendar'),false);
    if(change==='hide'||change==='unload'){await p.refresh(false);assert.equal(p.calls.filter(c=>c==='account').length,1);}
    assert.equal(p.loading.value,false);
  }
});
test('overview polling does not overlap an unfinished calendar reload',async()=>{
  const p=refreshableHome();let finish;p.calendarRef.value.reload=()=>{p.calls.push('calendar');return new Promise(resolve=>finish=resolve);};
  const pending=p.refresh();await new Promise(resolve=>setImmediate(resolve));p.advance(20000);await p.refresh(false);
  assert.deepEqual(p.calls,['account','runs','calendar']);finish();await pending;
});
test('actual home template displays the effective next-seat order',async()=>{
  const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
  const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
  const effective=drafts.nextPlanWindow(plan,policy,new Date());
  const store={activeAccount:{id:'a',plan,date_exceptions:{[effective.date]:{action:'override',begin:'14:00',end:'18:00',seat_ids:['2','1']}}},
    activeAccountId:'a',user:{role:'student'},accounts:[],policy,seatMap:{1:{name:'Base seat'},2:{name:'One-day seat'}},
    status:{allow_live:false},snapshots:{},counts:{total:0},runs:[],nextCursor:null,runRefreshError:'部分较早记录刷新失败，已保留上次结果',runCache:{cache_only:true,cache_limit:100,cached_total:7,cache_truncated:true,cached_at:1}};
  const encoded=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
  const storeModule=encoded('export const useWorkspace=()=>('+JSON.stringify(store)+');');
  const layout=encoded(`import {h} from '${pathToFileURL(require.resolve('vue')).href}'; export default {props:{open:{default:undefined}},setup:(props,{slots})=>()=>props.open===false?null:h('div',slots.default?.())};`);
  const lifecycle=encoded('export const onShow=()=>{}; export const onHide=()=>{}; export const onUnload=()=>{};');
  const source=readFileSync(new URL('pages/index/index.vue',frontend),'utf8'),{descriptor}=parse(source);
  let code=compileScript(descriptor,{id:'home',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text','picker'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/stores\/workspace.js\1/g,JSON.stringify(storeModule))
    .replace(/(['"])@\/components\/[^'"]+\1/g,JSON.stringify(layout))
    .replace(/(['"])@dcloudio\/uni-app\1/g,JSON.stringify(lifecycle))
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  const component=(await import(encoded(code))).default;
  const html=await renderToString(Vue.createSSRApp(component));
  const preferred=html.match(/<dt[^>]*>下次首选<\/dt><dd[^>]*>(.*?)<\/dd>/s)?.[1];
  const backup=html.match(/<dt[^>]*>下次备选<\/dt><dd[^>]*>(.*?)<\/dd>/s)?.[1];
  assert.match(preferred||'',/One-day seat/);assert.match(backup||'',/Base seat/);
  assert.match(html,/<p[^>]*role="status"[^>]*>部分较早记录刷新失败，已保留上次结果<\/p>/);
  assert.match(html,/已同步的 7 条记录/);assert.match(html,/上限 100 条/);assert.doesNotMatch(html,/最近 100 条/);
});
