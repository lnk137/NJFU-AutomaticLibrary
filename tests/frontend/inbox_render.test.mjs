import test from 'node:test';import assert from 'node:assert/strict';import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
async function component(name){
  const {descriptor}=parse(await readFile(new URL('components/'+name+'.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:name,inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  return (await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
}
const notice=await component('InboxNotice'),monitor=await component('InboxMonitor');
const render=(component,props)=>renderToString(Vue.createSSRApp(component,props));
test('real student notice calls pending configuration ineffective and preserves effective plan wording',async()=>{
  const html=await render(notice,{user:{role:'student',student_id:'2310000001',auth_mode:'offline'},status:{inbox:{accounts:[{student_id:'2310000001',state:'pending',version:1}]}}});
  assert.match(html,/尚未生效/);assert.match(html,/现有本机计划继续运行/);assert.doesNotMatch(html,/>已生效</);
});
test('a committed local revision is required for the effective receipt label',async()=>{
  const base={user:{role:'student',student_id:'2310000001'},status:{inbox:{accounts:[{student_id:'2310000001',state:'applied',version:2,applied_revision:5}]}}};
  assert.match(await render(notice,base),/本机版本 5/);
});
test('administrator monitoring remains hidden from student rendering',async()=>{
  const data={configured:true,connected:true,accounts:[{student_id:'private-student',auth_error:'login_failed'}]};
  assert.doesNotMatch(await render(monitor,{role:'student',data}),/private-student|学校拒绝/);
  const html=await render(monitor,{role:'root',data});assert.match(html,/private-student/);assert.match(html,/学校拒绝登录/);
});
test('normal empty sync snapshots produce no student notification',async()=>{
  const html=await render(notice,{user:{role:'student',student_id:'fixture'},status:{inbox:{accounts:[{student_id:'fixture',state:null,version:0,applied_version:0}]}}});
  assert.doesNotMatch(html,/待同步|inbox-notice/);
});
test('pending guidance is collapsed while failure reasons remain immediately visible',async()=>{
  const props={user:{role:'student',student_id:'fixture'},status:{inbox:{accounts:[{student_id:'fixture',version:2,state:'pending'}]}}};
  const html=await render(notice,props);
  assert.match(html,/<details[ >]/);assert.doesNotMatch(html,/<details[^>]*\bopen\b/);
  assert.match(html,/尚未生效/);
  const failed=await render(notice,{...props,status:{inbox:{accounts:[{student_id:'fixture',version:2,state:'failed',message:'学校密码已变更，请重新登录'}]}}});
  assert.match(failed,/role="alert"/);assert.match(failed,/学校密码已变更，请重新登录/);
});
test('root sees pending and authentication problems outside the collapsed account details',async()=>{
  const html=await render(monitor,{role:'root',data:{configured:true,connected:true,accounts:[
    {student_id:'normal-fixture',version:0,state:null,plan_ready:true},
    {student_id:'pending-fixture',version:2,state:'leased'},
    {student_id:'auth-fixture',version:0,state:null,auth_error:'verification_required'}
  ]}});
  const beforeDetails=html.split('<details')[0],insideDetails=html.slice(html.indexOf('<details'));
  assert.match(beforeDetails,/pending-fixture/);assert.match(beforeDetails,/auth-fixture|学校要求人工验证/);
  assert.doesNotMatch(beforeDetails,/normal-fixture/);assert.match(insideDetails,/normal-fixture/);
  assert.doesNotMatch(html,/<details[^>]*\bopen\b/);
});
test('a failed receipt without a message is still an alert',async()=>{
  const html=await render(notice,{user:{role:'student',student_id:'fixture'},status:{inbox:{accounts:[{student_id:'fixture',state:'failed',version:3}]}}});
  assert.match(html,/role="alert"/);assert.match(html,/同步失败/);
});
test('unchecked plans remain visible as a count without classifying paused plans as authentication failures',async()=>{
  for(const receipt of [{state:null,version:0},{state:'applied',version:2,applied_revision:4}]){
    const html=await render(monitor,{role:'root',data:{configured:true,connected:true,accounts:[
      {student_id:'needs-check-fixture',plan_ready:false,...receipt},
      {student_id:'normal-fixture',plan_ready:true,state:null,version:0}
    ]}});
    const visible=html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g,'');
    assert.match(visible,/计划未开启或需核对 1 个/);
    assert.match(html,/<summary[^>]*>同步详情/);
    assert.match(html,/needs-check-fixture/);assert.doesNotMatch(visible,/认证异常/);
  }
});
