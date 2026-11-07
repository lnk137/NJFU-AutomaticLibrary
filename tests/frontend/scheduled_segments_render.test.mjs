import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const dataURL=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64'),vue=pathToFileURL(require.resolve('vue')).href;
const dialog=dataURL(`import {h} from '${vue}';export default {props:['open','title'],setup(props,{slots}){return ()=>h('section',{role:'dialog'},[h('h2',props.title),slots.default?.(),h('footer',slots.footer?.())]);}};`);
const stub=dataURL(`import {h} from '${vue}';export default {setup(){return ()=>h('span');}};`);
const {descriptor}=parse(await readFile(new URL('components/ScheduledSegments.vue',frontend),'utf8'));
let code=compileScript(descriptor,{id:'scheduled-segments',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
code=code.replace(/from (['"])vue\1/g,`from '${vue}'`).replace(/(['"])\.\/AppDialog.vue\1/g,JSON.stringify(dialog)).replace(/(['"])\.\/[^'"]+\.vue\1/g,JSON.stringify(stub)).replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
const component=(await import(dataURL(code))).default;
const segments=[{id:'am',begin:'09:00',end:'12:00',seat_ids:['1','2']},{id:'pm',begin:'14:00',end:'17:00',seat_ids:['2']}];
const plan={mode:'day',segments,week_segments:{},seat_ids:['1']};
const render=(overrides={})=>renderToString(Vue.createSSRApp(component,{modelValue:{plan,day:'1'},account:{id:'a',revision:1},seats:[{id:'1',name:'A001'},{id:'2',name:'A002'}],policy:{open:'07:30',close:'22:00',friday_close:'20:00'},sessionKey:1,...overrides}));
test('scheduled editor renders distinct intervals with independent ordered seats and explicit application',async()=>{
 const html=await render();for(const label of ['定时预约时段','时段 1','时段 2','A001 → A002','添加时段','应用时段','周五最晚20:00'])assert.ok(html.includes(label),label);
 assert.doesNotMatch(html,/立即提交|检查全部时段|学校密码/);assert.match(html,/scheduled-begin-0/);assert.match(html,/scheduled-begin-1/);
});
test('weekly editor renders each weekday and selected day independent segment count',async()=>{
 const html=await render({modelValue:{plan:{...plan,mode:'week',week_segments:{'1':segments,'5':[segments[0]]}},day:'5'}});
 for(const label of ['周一','周二','周三','周四','周五','周六','周日','本日不预约'])assert.ok(html.includes(label),label);
 assert.match(html,/时段 1/);assert.doesNotMatch(html,/时段 2/);
});
test('overlap error stays visible and disables applying invalid intervals',async()=>{
 const html=await render({modelValue:{plan:{...plan,segments:[segments[0],{...segments[1],begin:'11:00'}]},day:'1'}});
 assert.match(html,/时段不能重叠/);assert.match(html,/disabled[^>]*>应用时段/);
});
