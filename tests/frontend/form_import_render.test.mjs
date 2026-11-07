import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
async function render(role,batch){
 let source;try{source=await readFile(new URL('components/FormImportReview.vue',frontend),'utf8');}catch{assert.fail('Form review view missing');}
 const {descriptor}=parse(source);let code=compileScript(descriptor,{id:'form-review',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
 code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`).replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
 const c=(await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
 return renderToString(Vue.createSSRApp(c,{role,batch,seats:[{id:'1',name:'A001'}]}));
}
const batch={filename:'test.csv',state:'ready',ready_count:1,rows:[{line:2,alias:'测试同学',student_id:'99000001',action:'scheduled',account_saved:true,status:'ready',message:'等待确认',plan:{seat_ids:['1'],mode:'day',begin:'09:00',end:'12:00',trigger:'07:00:00'}}]};
test('root review has one file confirmation, formal account state and no row checkboxes',async()=>{
 const html=await render('root',batch);
 assert.match(html,/测试同学/);assert.match(html,/账号已保存/);assert.match(html,/A001/);assert.match(html,/确认这份预约/);
 assert.doesNotMatch(html,/第2行|共 1 项|待确认 1/);
 assert.equal((html.match(/<button/g)||[]).length,1);assert.doesNotMatch(html,/checkbox|学校密码/);
});
test('one personal today request states the immediate effect before its single confirmation',async()=>{
 const html=await render('root',{...batch,rows:[{...batch.rows[0],action:'today',plan:null,today:{date:'2026-10-24',begin:'09:00',end:'12:00',seat_ids:['1']}}]});
 assert.match(html,/确认后立即提交本次预约/);assert.match(html,/原定时计划保留/);
 assert.equal((html.match(/<button/g)||[]).length,1);
});
test('student cannot render passed-in private import data and errors remain visible for root',async()=>{
 assert.doesNotMatch(await render('student',batch),/测试同学|99000001|确认执行/);
 const html=await render('root',{...batch,ready_count:0,rows:[{...batch.rows[0],status:'error',message:'学校验证未通过'}]});
 assert.match(html,/学校验证未通过/);assert.doesNotMatch(html,/<button|待确认/);
});
