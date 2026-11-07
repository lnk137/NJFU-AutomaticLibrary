import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const cache=new Map();
async function component(name){
  if(cache.has(name))return cache.get(name);
  const {descriptor}=parse(await readFile(new URL('components/'+name+'.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:name,inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:t=>['view','text'].includes(t)}}}).content;
  if(name==='FormImport')code=code.replace(/import \* as api from ['"]@\/services\/local_api.js['"];?/,"const api={listFormImports:async()=>({items:[]})};");
  if(name==='AppIcon')code=code.replace(/import\.meta\.glob\([^;]+\)/,'({})');
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  for(const match of [...code.matchAll(/(['"])@\/components\/([^'"]+)\.vue\1/g)]){
    code=code.replaceAll(match[0],JSON.stringify((await component(match[2])).url));
  }
  const url='data:text/javascript;base64,'+Buffer.from(code).toString('base64'),result={url,value:(await import(url)).default};cache.set(name,result);return result;
}
const render=async(name,props)=>renderToString(Vue.createSSRApp((await component(name)).value,props));
function withoutHelp(html){return html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g,'');}
function collapsedHelp(html){assert.match(html,/<details\b/);assert.doesNotMatch(html,/<details[^>]*\bopen\b/);}
const seats=[{id:'1',name:'A001',location:'2F'},{id:'2',name:'A002',location:'2F'}];
const account={id:'a',revision:3,plan:{seat_ids:['1'],enabled:false},date_exceptions:{'2099-01-01':{action:'skip'}},
  seat_preferences_revision:1,seat_preferences:{favorites:[{seat_id:'1',tags:['自定义']}],groups:[{id:'g',name:'我的组',seat_ids:['2','1']}]}};

test('date help is optional while saved changes, actions and paused/offline conditions remain visible',async()=>{
  const html=await render('DateExceptions',{account,seats,policy:{},offline:true});collapsedHelp(html);
  const visible=withoutHelp(html);
  for(const text of ['跳过下次','请假几天','只改某天','2099-01-01','不预约','恢复原计划','在线','暂停'])assert.ok(visible.includes(text),text);
});

test('personal library keeps ownership counts and unsaved edits visible outside optional help',async()=>{
  const html=await render('SeatLibrary',{account,seats,selected:['1'],offline:true,
    editor:{account_id:'a',kind:'group',id:'g',name:'保留输入',seat_ids:['2','1'],dirty:true}});collapsedHelp(html);
  const visible=withoutHelp(html);
  assert.match(visible,/我的收藏[\s\S]*?1/);assert.match(visible,/偏好分组[\s\S]*?1/);
  for(const text of ['取消编辑','保存分组','先保存或取消','在线','A002','A001'])assert.ok(visible.includes(text),text);
});

test('file review keeps every action and error visible, collapses routine explanations, and confirms once',async()=>{
  const batch={filename:'fixture.xlsx',state:'ready',ready_count:1,rows:[
    {line:2,student_id:'99000001',alias:'测试甲',status:'ready',action:'today',account_saved:true,message:'已验证，等待管理员确认',today:{date:'2099-01-01',begin:'09:00',end:'12:00',seat_ids:['1']}},
    {line:3,student_id:'99000002',alias:'测试乙',status:'error',action:'scheduled',message:'密码不正确'}]};
  const html=await render('FormImportReview',{role:'root',batch,seats});collapsedHelp(html);
  const visible=withoutHelp(html);
  for(const text of ['测试甲','测试乙','当天一次预约','2099-01-01','09:00','12:00','A001','密码不正确','立即提交','账号已保存'])assert.ok(visible.includes(text),text);
  assert.match(visible,/role="alert"[^>]*>密码不正确/);
  assert.doesNotMatch(visible,/已验证，等待管理员确认/);
  assert.equal((html.match(/<button/g)||[]).length,1);assert.doesNotMatch(html,/checkbox/);
  const interrupted=await render('FormImportReview',{role:'root',batch:{...batch,state:'interrupted',message:'处理已中断，请核对学校记录'},seats});
  assert.match(withoutHelp(interrupted),/role="alert"[^>]*>处理已中断/);
  assert.doesNotMatch(interrupted,/<button/);
});

test('opening import discloses automatic account saving before a file can be selected',async()=>{
  // Render real Vue event handlers and dialogs; replace only the HTTP boundary.
  const root={children:[]},body={children:[]};
  const renderer=Vue.createRenderer({
    createElement:tag=>({tag,children:[],props:{}}),createText:text=>({text}),createComment:()=>({text:''}),
    setText:(node,text)=>node.text=text,setElementText:(node,text)=>{node.text=text;node.children=[];},
    patchProp:(node,key,old,value)=>node.props[key]=value,
    insert(node,parent,anchor){if(node.parent){const i=node.parent.children.indexOf(node);if(i>=0)node.parent.children.splice(i,1);}node.parent=parent;const i=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(i<0?parent.children.length:i,0,node);},
    remove(node){const i=node.parent?.children.indexOf(node);if(i>=0)node.parent.children.splice(i,1);},
    parentNode:node=>node.parent,nextSibling:node=>node.parent?.children[node.parent.children.indexOf(node)+1],querySelector:()=>body,
    setScopeId(){},insertStaticContent(){throw new Error('Unexpected static content');}
  });
  function text(node){if(node.tag==='details'&&!node.props.open)return text(node.children.find(n=>n.tag==='summary')||{});return (node.text||'')+(node.children||[]).map(text).join('');}
  function find(node,predicate){if(predicate(node))return node;for(const child of node.children||[]){const result=find(child,predicate);if(result)return result;}}
  const app=renderer.createApp((await component('FormImport')).value,{role:'root',sessionKey:1});
  try{
    app.mount(root);await Vue.nextTick();
    find(root,node=>node.tag==='button'&&text(node)==='导入预约表单').props.onClick();await Vue.nextTick();
    const visible=text(body);
    assert.match(visible,/新账号通过后保存/);assert.match(visible,/预约仍需确认/);
    assert.match(visible,/每份一人|一人一份/);
    assert.ok(find(body,node=>node.props?.type==='file'));
  }finally{app.unmount();}
});

test('confirmed and completed batches do not misleadingly ask for confirmation again',async()=>{
  const row={line:2,student_id:'99000001',status:'ready',action:'scheduled'};
  const confirming=await render('FormImportReview',{role:'root',batch:{filename:'legacy.csv',state:'confirming',rows:[row,{...row,line:3,status:'done'}]}});
  assert.match(withoutHelp(confirming),/待处理 1/);assert.doesNotMatch(confirming,/待确认|确认执行/);
  const done=await render('FormImportReview',{role:'root',batch:{filename:'fixture.csv',state:'done',rows:[{...row,status:'done'}]}});
  assert.doesNotMatch(done,/待确认|待处理 0|确认执行/);
});
