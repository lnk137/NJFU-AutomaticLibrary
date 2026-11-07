import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createCancellationController,newCancellationState} from '../../AL-FrontEnd/services/cancellation.mjs';
import {transformViteAssets} from './sfc_test_helpers.mjs';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
async function component(name,dependencies={}){
  const {descriptor}=parse(readFileSync(new URL('components/'+name+'.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:name,inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  code=transformViteAssets(code);
  for(const [path,url] of Object.entries(dependencies))code=code.replaceAll(JSON.stringify(path),JSON.stringify(url)).replaceAll("'"+path+"'",JSON.stringify(url));
  const url='data:text/javascript;base64,'+Buffer.from(code).toString('base64');return {url,value:(await import(url)).default};
}
const icon=await component('AppIcon'),dialog=await component('AppDialog',{'@/components/AppIcon.vue':icon.url});
const {descriptor}=parse(readFileSync(new URL('components/CancelReservation.vue',frontend),'utf8'));
const render=Vue.compile(descriptor.template.content);
async function panel(result){
  const state=Vue.reactive({...newCancellationState(),open:true,pending:{id:'fixture-cancel',expires_at:200},result});
  const controller=createCancellationController({state,api:{},cache:null,context:()=>({accountId:'fixture',recordId:'record',revision:1})});
  const context={};
  const page={components:{AppDialog:dialog.value},render,setup:()=>({state,controller,standalone:true,record:null,disabled:false,allowLive:true,close(){},confirm(){},refreshResult(){},seatName:id=>id})};
  const html=await renderToString(Vue.createSSRApp(page),context);
  return html+(context.teleports?.body||'');
}
test('no-ACK uncertainty directs the customer to school without an ineffective reconciliation button',async()=>{
  const html=await panel({state:'uncertain',can_reconcile:false,message:'取消结果待核实'});
  assert.match(html,/不能自动|无法自动/);assert.match(html,/学校预约页面/);
  assert.doesNotMatch(html,/<button[^>]*>核对取消结果<\/button>/);
  assert.doesNotMatch(html,/可点击核对取消结果/);
});
test('only a supported readback is offered as school reconciliation',async()=>{
  assert.match(await panel({state:'uncertain',can_reconcile:true,message:'待核实'}),/<button[^>]*>核对取消结果<\/button>/);
  assert.match(await panel(null),/<button[^>]*>读取取消状态<\/button>/);
  assert.doesNotMatch(await panel({state:'success',can_reconcile:false,message:'已取消'}),/<button[^>]*>核对取消结果<\/button>/);
});
