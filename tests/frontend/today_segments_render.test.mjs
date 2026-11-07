import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const dataURL=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
const vue=pathToFileURL(require.resolve('vue')).href;
const dialog=dataURL(`import {h} from '${vue}';export default {props:['open','title'],setup(props,{slots}){return ()=>h('section',{role:'dialog'},[h('h2',props.title),slots.default?.(),h('footer',slots.footer?.())]);}};`);
const stub=dataURL(`import {h} from '${vue}';export default {setup(){return ()=>h('span');}};`);
async function render(overrides={},at=Date.parse('2026-11-06T08:10:00+08:00'),stateOverride=null){
  let source;try{source=await readFile(new URL('components/TodaySegments.vue',frontend),'utf8');}catch{assert.fail('segmented same-day dialog is missing');}
  const {descriptor}=parse(source);
  let code=compileScript(descriptor,{id:'today-segments',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${vue}'`)
    .replace(/(['"])@\/components\/AppDialog.vue\1/g,JSON.stringify(dialog))
    .replace(/(['"])@\/components\/[^'"]+\1/g,JSON.stringify(stub))
    .replace(/(['"])@\/services\/local_api.js\1/g,JSON.stringify(dataURL('export {};')))
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>{
      const url=new URL('services/'+path,frontend).href;
      return JSON.stringify(path==='today_segments.mjs'&&stateOverride?dataURL(`import * as feature from '${url}';export * from '${url}';export function createTodayBatchController(options){const controller=feature.createTodayBatchController(options);Object.assign(options.state,${JSON.stringify(stateOverride)});return controller;}`):url);
    });
  const component=(await import(dataURL(code))).default;
  const date=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(at));
  const editor={account_id:'account-one',date,segments:[{begin:'09:00',end:'12:00',seat_ids:['1','2']},{begin:'14:00',end:'18:00',seat_ids:['3']}]};
  const OriginalDate=globalThis.Date;
  globalThis.Date=class extends OriginalDate{constructor(...args){super(...(args.length?args:[at]));}static now(){return at;}};
  try{return await renderToString(Vue.createSSRApp(component,{account:{id:'account-one',revision:1},revision:1,policy:{open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120},sessionKey:1,allowLive:true,seats:[{id:'1',name:'A001'},{id:'2',name:'A002'},{id:'3',name:'B001'}],editor,...overrides}));}
  finally{globalThis.Date=OriginalDate;}
}
test('same-day dialog renders separate interval cards, ordered seats and one batch check',async()=>{
  const html=await render();
  for(const text of ['当天分段预约','时段 1','时段 2','A001','A002','B001','添加时段','检查全部时段'])assert.ok(html.includes(text),text);
  assert.equal((html.match(/aria-label="第 [12] 段开始"/g)||[]).length,2);
  assert.equal((html.match(/检查全部时段/g)||[]).length,1);
  assert.doesNotMatch(html,/确认提交真实预约|保存并开启定时|学校密码/);
});
test('offline state and future execution impact remain visible',async()=>{
  const html=await render({offline:true,allowLive:false});
  assert.match(html,/重新实时登录/);assert.match(html,/原定时计划保留/);
  assert.match(html,/disabled[^>]*>检查全部时段/);
});
test('the plan page mounts the focused dialog and retains a separate account-owned draft',async()=>{
  const page=await readFile(new URL('pages/account_info/account_info.vue',frontend),'utf8');
  assert.match(page,/<TodaySegments\b/);
  assert.match(page,/v-model:editor="todayEditor"/);
  assert.match(page,/:revision="revision"/);
  assert.doesNotMatch(page,/@click="confirmToday"/);
});
test('late hours show an empty state with no fabricated interval and disable booking actions',async()=>{
  for(const text of ['2026-11-06T18:00:00+08:00','2026-11-06T20:05:00+08:00','2026-11-07T23:40:00+08:00']){
    const html=await render({editor:null},Date.parse(text));
    assert.match(html,/今天已无可预约时段/);assert.doesNotMatch(html,/class="interval-card"|24:00|20:30–20:00/);
    assert.match(html,/disabled[^>]*>检查全部时段/);assert.match(html,/disabled[^>]*>[\s\S]*?添加时段/);
    assert.match(html,/>关闭</);
  }
});
test('an uncertain stopped result offers explicit school verification and keeps local refresh separate',async()=>{
  const batch={batch_id:'old',date:'2026-11-06',state:'stopped',segments:[{begin:'09:00',end:'12:00',seat_ids:['1'],status:'uncertain'}]};
  const state={batch,confirmedId:'old'};
  const html=await render({},undefined,state);
  assert.match(html,/刷新本地进度/);assert.match(html,/>核对学校记录</);assert.doesNotMatch(html,/>再预约其他时段</);
  for(const mode of [{offline:true},{disabled:true}]){
    const locked=await render(mode,undefined,state);assert.match(locked,/disabled[^>]*>核对学校记录/);
  }
  const busy=await render({},undefined,{...state,reconciling:true});
  assert.match(busy,/disabled[^>]*>核对中…/);assert.match(busy,/disabled[^>]*>刷新本地进度/);
});
test('an unapplied restored picker exposes verification without discarding its selections',async()=>{
  const value={date:'2026-11-06',segments:[{begin:'16:00',end:'19:00',seat_ids:['1']}]};
  const editor={account_id:'account-one',...value,baseline:JSON.stringify(value),dirty:true,
    seat_index:0,pending_seats:['2'],seat_baseline:JSON.stringify(['1']),library_editor:null};
  const batch={batch_id:'old',date:value.date,state:'stopped',segments:[{begin:'09:00',end:'12:00',seat_ids:['1'],status:'uncertain'}]};
  const html=await render({editor},undefined,{batch,confirmedId:'old',error:'query unavailable'});
  assert.match(html,/选择座位 · 时段 1/);assert.match(html,/query unavailable/);
  assert.match(html,/首选 · A002/);assert.match(html,/>核对学校记录</);assert.doesNotMatch(html,/>应用座位</);
  const verified={...batch,segments:batch.segments.map(s=>({...s,status:'success'}))};
  const after=await render({editor},undefined,{batch:verified,confirmedId:'old'});
  assert.match(after,/首选 · A002/);assert.match(after,/>应用座位</);
});
