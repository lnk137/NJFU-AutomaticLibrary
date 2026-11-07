import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
async function component(name,dependencies={}){
  const {descriptor}=parse(await readFile(new URL('components/'+name+'.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:name,inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text','picker'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  for(const [path,url] of Object.entries(dependencies))code=code.replaceAll(JSON.stringify(path),JSON.stringify(url)).replaceAll("'"+path+"'",JSON.stringify(url));
  const url='data:text/javascript;base64,'+Buffer.from(code).toString('base64');return {url,value:(await import(url)).default};
}
const charts=await component('AdminCharts'),status=await component('SchedulerStatus');
const dashboard=await component('AdminDashboard',{'@/components/AdminCharts.vue':charts.url,'@/components/SchedulerStatus.vue':status.url});
const data={date:'2026-10-24',generated_at:'2026-10-24T09:00:00+08:00',summary:{total:9,success:0,failed:0,skipped:9,uncertain:0,pending:0,closed:9,success_rate:0},
  population:{authorized:14,saved:12,enabled:9,checked:9,attention:0},runtime:{clock_ok:true,scheduler_running:true,allow_live:true},
  trend:[{date:'2026-10-24',total:9,success:0,failed:0,skipped:9,uncertain:0,pending:0,request:{count:0,p50:null,p95:null}}],
  delays:[],delay_count:0,delay_missing:9,accounts:[],alerts:[],alert_total:0,runs:[],runs_total:0};
const render=props=>renderToString(Vue.createSSRApp(dashboard.value,props));
test('real dashboard renders separate outcome counts and explicit missing timing',async()=>{
  const html=await render({role:'root',data,filters:{date:'2026-10-24',days:14,kind:'scheduled'}});
  assert.match(html,/已跳过/);assert.match(html,/本地检查通过/);assert.match(html,/首次请求/);assert.match(html,/无.*计时|无.*请求/);
  assert.doesNotMatch(html,/0\.0 ms/);
});
test('student identity cannot render provided administrator data',async()=>{
  const html=await render({role:'student',data});assert.doesNotMatch(html,/14|首次请求|下一轮/);
});
test('root sees the actual reported submit capacity and unknown capacity stays absent',async()=>{
  for(const capacity of [24,50]) {
    const html=await render({role:'root',data:{...data,runtime:{...data.runtime,capacity}}});
    assert.match(html,new RegExp(`提交并发上限\\s*${capacity}`));
  }
  assert.doesNotMatch(await render({role:'root',data}),/提交并发上限/);
  assert.doesNotMatch(await render({role:'student',data:{...data,runtime:{capacity:50}}}),/提交并发上限/);
});
test('stale refresh and unavailable data stay distinguishable from a fresh empty result',async()=>{
  assert.match(await render({role:'root',data,error:'fixture timeout',stale:true}),/fixture timeout/);
  assert.match(await render({role:'root',data,error:'fixture timeout',stale:true}),/上次|旧数据/);
  assert.match(await render({role:'root',data:null}),/读取|加载|暂无/);
});
const reminder={start_date:'2026-10-25',end_date:'2026-10-31',checked_accounts:2,unchecked_accounts:0,has_more:false,
  items:[{target_date:'2026-10-26',seat:{id:'1',name:'A001',location:'2F'},begin:'10:00',end:'12:00',kind:'preferred',accounts:[
    {account_id:'a',student_id:'99000001',name:'测试同学甲',begin:'09:00',end:'12:00',rank:1},
    {account_id:'b',student_id:'99000002',name:'测试同学乙',begin:'10:00',end:'13:00',rank:1}]}]};
test('root sees advisory collision date, seat, overlapping interval and both people',async()=>{
  const html=await render({role:'root',data:{...data,seat_conflicts:reminder}});
  assert.match(html,/内部撞座提醒/);assert.match(html,/首选重叠/);assert.match(html,/测试同学甲/);assert.match(html,/测试同学乙/);
  assert.match(html,/2026-10-26/);assert.match(html,/A001/);assert.match(html,/10:00[–—-]12:00/);
  assert.match(html,/99000001/);assert.match(html,/09:00[–—-]12:00/);assert.match(html,/查看计划/);
  const student=await render({role:'student',data:{...data,seat_conflicts:reminder}});
  assert.doesNotMatch(student,/内部撞座提醒|测试同学|9900000/);
});
test('backup overlap and unchecked empty state remain explicit',async()=>{
  const backup={...reminder,items:[{...reminder.items[0],kind:'backup',accounts:[reminder.items[0].accounts[0],{...reminder.items[0].accounts[1],rank:3}]}]};
  const html=await render({role:'root',data:{...data,seat_conflicts:backup}});
  assert.match(html,/含备选/);assert.match(html,/备选2/);
  const empty=await render({role:'root',data:{...data,seat_conflicts:{...reminder,items:[],unchecked_accounts:1}}});
  assert.match(empty,/未发现已检查计划/);assert.match(empty,/1.*未参与检查/);assert.match(empty,/2026-10-25.*2026-10-31/s);
  assert.doesNotMatch(empty,/全部.*无冲突/);
});
test('truncated collisions never claim an exact total and initially show a compact list',async()=>{
  const items=Array.from({length:50},(_,i)=>({...reminder.items[0],accounts:reminder.items[0].accounts.map(a=>({...a,name:a.name+'-'+i}))}));
  const html=await render({role:'root',data:{...data,seat_conflicts:{...reminder,items,has_more:true}}});
  assert.match(html,/50\+/);assert.match(html,/最多展示50/);assert.match(html,/展开.*47/);
  assert.match(html,/测试同学甲-2/);assert.doesNotMatch(html,/测试同学甲-3/);
});
test('collision explanations collapse while unchecked plans and actionable overlaps stay visible',async()=>{
  const html=await render({role:'root',data:{...data,seat_conflicts:{...reminder,unchecked_accounts:1}}});
  const panel=html.slice(html.indexOf('aria-label="内部撞座提醒"'),html.indexOf('</section>',html.indexOf('aria-label="内部撞座提醒"')));
  assert.match(panel,/<details/);assert.doesNotMatch(panel,/<details[^>]*\bopen\b/);
  const visible=panel.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g,'');
  for(const text of ['2026-10-26','A001','测试同学甲','查看计划','未参与检查'])assert.ok(visible.includes(text),text);
  assert.match(visible,/role="status"/);assert.doesNotMatch(visible,/按已保存且尚未放号/);
});
