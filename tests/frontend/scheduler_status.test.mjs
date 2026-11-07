import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const frontend=new URL('../../AL-FrontEnd/',import.meta.url);
const require=createRequire(new URL('package.json',frontend));
const Vue=require('vue');
const {renderToString}=require('@vue/server-renderer');
const {parse,compileScript}=require('@vue/compiler-sfc');
const sfc=await readFile(new URL('components/SchedulerStatus.vue',frontend),'utf8');
const {descriptor}=parse(sfc);
let code=compileScript(descriptor,{id:'scheduler-status-test',inlineTemplate:true,
  templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
  .replace(/(['"])@\/services\/scheduler_status.mjs\1/g,JSON.stringify(new URL('services/scheduler_status.mjs',frontend).href));
const {default:component}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const render=status=>renderToString(Vue.createSSRApp(component,{status}));

test('clock lock overrides a stale running message and renders an alert with the actual reason',async()=>{
  const html=await render({allow_live:true,scheduler_running:true,clock_ok:false,
    execution_message:'定时预约已开启',last_error:'本机时间发生跳变，请核对时间后重启后台'});
  assert.match(html,/role="alert"/);assert.match(html,/自动预约已暂停/);assert.match(html,/本机时间发生跳变/);
  assert.doesNotMatch(html,/定时预约已开启/);
});
test('healthy running state is displayed only after clock validation',async()=>{
  assert.match(await render({allow_live:true,scheduler_running:true,clock_ok:true}),/定时预约已开启/);
  assert.doesNotMatch(await render({allow_live:true,scheduler_running:true}),/定时预约已开启/);
});
test('stopped service and query-only mode are distinguishable',async()=>{
  assert.match(await render({allow_live:true,scheduler_running:false,clock_ok:true}),/自动预约未运行/);
  assert.match(await render({allow_live:false,scheduler_running:true,clock_ok:true}),/仅登录与查询/);
});

test('temporary loop warning is visible without claiming a permanent clock pause',async()=>{
  const html=await render({allow_live:true,scheduler_running:true,clock_ok:true,
    scheduler_warning:'后台短暂异常，正在自动重试'});
  assert.match(html,/正在自动重试/);
  assert.doesNotMatch(html,/自动预约已暂停|时钟校验正常/);
});

test('native recovery and operator pause never claim healthy automatic booking',async()=>{
  const running={allow_live:true,scheduler_running:true,clock_ok:true};
  assert.match(await render({...running,executor_state:'recovering'}),/正在恢复/);
  assert.match(await render({...running,executor_state:'blocked'}),/请联系管理员/);
  assert.match(await render({...running,executor_state:'paused',allow_live:false}),/管理员已暂停/);
});

test('dual publisher failure overrides healthy legacy heartbeat',async()=>{
  const html=await render({allow_live:true,scheduler_running:true,clock_ok:true,
    execution_scope:'dual_node',dual_node_status:{state:'blocked'},scheduler_warning:'双机计划同步尚未确认'});
  assert.match(html,/role="alert"/);assert.match(html,/双机预约需检查/);
  assert.doesNotMatch(html,/定时预约已开启/);
});

test('dual waiting and probing are visible without a false fault or ready claim',async()=>{
  for(const state of ['waiting','probing','eligible','preparing','selected','authorized','idle']) {
    const html=await render({allow_live:true,scheduler_running:true,clock_ok:true,
      ready_to_submit:false,execution_scope:'dual_node',dual_node_status:{state}});
    assert.doesNotMatch(html,/role="alert"|双机预约需检查|定时预约已开启|执行已就绪/);
    assert.match(html,/等待|探测|准备|暂无/);
  }
});

test('dual readiness uses its own confirmed scope and keeps pause higher priority',async()=>{
  const base={allow_live:true,scheduler_running:true,clock_ok:true,
    ready_to_submit:true,execution_scope:'dual_node',dual_node_status:{state:'ready'}};
  assert.match(await render(base),/双机执行已就绪/);
  assert.match(await render({...base,dual_node_status:{state:'ready',degraded:true}}),/仅一台执行节点可用/);
  assert.match(await render({...base,allow_live:false,executor_state:'paused'}),/管理员已暂停/);
  assert.doesNotMatch(await render({...base,clock_ok:false}),/执行已就绪/);
});
