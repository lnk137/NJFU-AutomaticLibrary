import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url);
const require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const {descriptor}=parse(await readFile(new URL('components/MacPowerPanel.vue',frontend),'utf8'));
let code=compileScript(descriptor,{id:'MacPowerPanel',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`);
const component=(await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
const data={supported:true,installed:true,config:{enabled:true,paused:false,revision:2},native:{armed:true,next_wake:1792882500},agent_online:true,agent_fresh:true,native_fresh:true,backend_fresh:true,agent:{hold:true,reason:'隔离测试保护原因',service_online:true,system:{ac:true,lid_closed:false,blockers:['OtherApp']}}};
const render=props=>renderToString(Vue.createSSRApp(component,props));
test('local root sees real wake time, release review and lid limitations',async()=>{
 const html=await render({role:'root',data});
 assert.match(html,/06:55/);assert.match(html,/07:00/);assert.match(html,/07:05/);
 assert.match(html,/隔离测试保护原因/);assert.match(html,/合盖/);assert.match(html,/不会强制/);
});
test('student cannot render supplied private power data or controls',async()=>{
 const html=await render({role:'student',data});
 assert.doesNotMatch(html,/06:55|隔离测试保护原因|OtherApp/);
});
test('uninstalled and disabled states cannot be presented as armed',async()=>{
 const html=await render({role:'root',data:{...data,installed:false,native:{},agent_online:false}});
 assert.match(html,/安装并启用/);assert.match(html,/未安装/);assert.doesNotMatch(html,/已确认唤醒/);
});
test('stale evidence and failed installation remain visible',async()=>{
 const html=await render({role:'root',data:{...data,agent_online:false,backend_fresh:false,install_error:'系统授权未完成'}});
 assert.match(html,/系统授权未完成/);assert.match(html,/过期|待恢复|未连接/);
});
test('stale held lease is historical and degraded installations expose repair',async()=>{
 const html=await render({role:'root',data:{...data,agent_online:false,agent_fresh:false,native_fresh:false,native:{armed:true},install_error:'组件待修复'}});
 assert.doesNotMatch(html,/已确认唤醒|正在时段保护/);
 assert.match(html,/修复安装/);
});
