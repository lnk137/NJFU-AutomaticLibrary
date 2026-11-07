import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import * as helpers from '../../AL-FrontEnd/services/seat_preferences.mjs';

const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer'),{parse,compileScript}=require('@vue/compiler-sfc');
const seats=[{id:'1',name:'A001',location:'2F'},{id:'2',name:'A002',location:'2F'}];
const account={id:'a',revision:7,plan:{seat_ids:['1'],enabled:true},seat_preferences_revision:1,
  seat_preferences:{favorites:[{seat_id:'1',tags:['自定义']}],groups:[{id:'g',name:'我的组',seat_ids:['2','1']}]}};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function panel(api={},changes={}){
  const props=Vue.reactive({account:structuredClone(account),seats,visibleSeats:seats,selected:['1'],search:'',sessionKey:1,disabled:false,offline:false,editor:null,...changes});
  const events=[],hooks={};let discard=true;
  const emit=(name,value)=>{events.push([name,value]);if(name==='update:editor')props.editor=value;};
  const source=readFileSync(new URL('components/SeatLibrary.vue',frontend),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const context={...Vue,...helpers,api,defineProps:()=>props,defineEmits:()=>emit,crypto:{randomUUID:()=> 'new-group'},
    onMounted:f=>hooks.mount=f,onBeforeUnmount:f=>hooks.unmount=f,uni:{showModal:o=>o.success({confirm:discard})}};
  const scope=Vue.effectScope();scope.run(()=>vm.runInNewContext(source+'\nglobalThis.subject={reload,toggleFavorite,openTags,openGroup,changeEditor,saveEditor,cancelEditor,moveEditorSeat,replaceEditorSeats,deleteGroup,applyGroup,data,error,loading,saving,currentEditor,activeTab};',context));
  return {p:context.subject,props,events,hooks,scope,setDiscard:v=>discard=v};
}
test('favorite and custom-tag saves use independent metadata versions only',async()=>{
  const writes=[],e=panel({saveSeatPreferences:async(id,preferences,revision)=>{writes.push({id,preferences,revision});return {account_id:id,revision:revision+1,...preferences};}});
  await e.p.toggleFavorite('2');assert.equal(writes[0].revision,1);assert.equal(writes[0].id,'a');
  assert.deepEqual([...writes[0].preferences.favorites].map(s=>s.seat_id),['1','2']);
  e.p.openTags(e.p.data.value.favorites[0]);e.p.changeEditor('text',' 新标签，自己填写 ');await e.p.saveEditor();
  assert.deepEqual([...writes[1].preferences.favorites[0].tags],['新标签','自己填写']);
  assert.equal(e.props.account.revision,7);assert.deepEqual([...e.props.selected],['1']);e.scope.stop();
});
test('applying and editing a group do not mutate the original selected order or plan',async()=>{
  let saved;const e=panel({saveSeatPreferences:async(id,preferences,revision)=>{saved=preferences;return {account_id:id,revision:revision+1,...preferences};}});
  e.p.applyGroup(e.p.data.value.groups[0]);const applied=e.events.find(([name])=>name==='apply')[1];
  assert.deepEqual([...applied],['2','1']);assert.deepEqual([...e.props.selected],['1']);
  e.p.openGroup(e.p.data.value.groups[0]);e.p.changeEditor('name','另一个名字');e.p.moveEditorSeat(0,1);await e.p.saveEditor();
  assert.deepEqual([...saved.groups[0].seat_ids],['1','2']);assert.deepEqual([...applied],['2','1']);
  assert.deepEqual([...e.props.account.plan.seat_ids],['1']);e.scope.stop();
});
test('offline cached groups can fill drafts but metadata cannot be written',async()=>{
  let requests=0;const e=panel({getSeatPreferences:async()=>{requests++;},saveSeatPreferences:async()=>{requests++;}},{offline:true});
  await e.p.reload();await e.p.toggleFavorite('2');e.p.applyGroup(e.p.data.value.groups[0]);
  assert.equal(requests,0);assert.equal(e.events.filter(([name])=>name==='apply').length,1);e.scope.stop();
});
test('late reads and writes cannot leak into a new account or session',async()=>{
  for(const action of ['read','write']){
    const wait=deferred(),e=panel({getSeatPreferences:()=>wait.promise,saveSeatPreferences:()=>wait.promise});
    const operation=action==='read'?e.p.reload():e.p.toggleFavorite('2');
    e.props.account={id:'b',revision:2};e.props.sessionKey++;await Vue.nextTick();
    wait.resolve({account_id:'a',revision:2,favorites:[{seat_id:'2',tags:['private']}],groups:[]});await operation;
    assert.equal(e.p.data.value.account_id,'b');assert.equal(e.p.data.value.favorites.length,0);
    assert.equal(e.events.filter(([name])=>name==='saved').length,0);assert.equal(e.p.error.value,'');e.scope.stop();
  }
});
test('stale metadata writes preserve the tag editor and the last saved library',async()=>{
  const e=panel({saveSeatPreferences:async()=>{throw Object.assign(new Error('请重新读取'),{code:'preferences_conflict'});}});
  e.p.openTags(e.p.data.value.favorites[0]);e.p.changeEditor('text','保留输入');await e.p.saveEditor();
  assert.equal(e.props.editor.text,'保留输入');assert.equal(e.props.editor.dirty,true);
  assert.deepEqual([...e.p.data.value.favorites[0].tags],['自定义']);assert.match(e.p.error.value,/重新读取/);e.scope.stop();
});
test('canceling dirty metadata can be declined and an old owner editor stays hidden',async()=>{
  const e=panel();e.p.openGroup(e.p.data.value.groups[0]);e.p.changeEditor('name','未保存');e.setDiscard(false);
  await e.p.cancelEditor();assert.equal(e.props.editor.name,'未保存');
  e.props.account={id:'b',revision:2};await Vue.nextTick();assert.equal(e.p.currentEditor.value,null);e.scope.stop();
});
test('opening another edit or deleting metadata cannot discard unfinished tag input',async()=>{
  let writes=0;const e=panel({saveSeatPreferences:async()=>{writes++;}});
  e.p.openTags(e.p.data.value.favorites[0]);e.p.changeEditor('text','尚未保存的自定义标签');
  e.p.openGroup(e.p.data.value.groups[0]);assert.equal(e.props.editor.kind,'tags');
  assert.equal(e.props.editor.text,'尚未保存的自定义标签');
  await e.p.toggleFavorite('1');await e.p.deleteGroup(e.p.data.value.groups[0]);
  assert.equal(writes,0);assert.equal(e.props.editor.text,'尚未保存的自定义标签');e.scope.stop();
});
test('a group containing an unavailable seat cannot silently apply a shortened list',()=>{
  const a=structuredClone(account);a.seat_preferences.groups[0].seat_ids=['2','missing'];
  const e=panel({}, {account:a});e.p.applyGroup(e.p.data.value.groups[0]);
  assert.equal(e.events.filter(([name])=>name==='apply').length,0);assert.match(e.p.error.value,/目录|座位/);e.scope.stop();
});
test('a retired-seat group can open for repair while application stays blocked',async()=>{
  const a=structuredClone(account);a.seat_preferences.groups[0].seat_ids=['2','missing'];
  let saved;const e=panel({saveSeatPreferences:async(id,preferences,revision)=>{saved=preferences;return {account_id:id,revision:revision+1,...preferences};}},{account:a});
  e.p.openGroup(e.p.data.value.groups[0]);assert.ok(e.props.editor);
  assert.deepEqual([...e.props.editor.seat_ids],['2','missing']);e.p.replaceEditorSeats();await e.p.saveEditor();
  assert.deepEqual([...saved.groups[0].seat_ids],['1']);e.scope.stop();
});
test('restored dirty group editor keeps its original version across automatic reads',async()=>{
  const a=structuredClone(account);a.seat_preferences_revision=2;a.seat_preferences.groups[0].seat_ids=['1','2'];
  const remote={account_id:'a',revision:2,...a.seat_preferences},writes=[];
  const e=panel({getSeatPreferences:async()=>remote,saveSeatPreferences:async(id,preferences,revision)=>{
    writes.push({preferences,revision});if(revision!==2)throw new Error('请重新读取后核对');return {account_id:id,revision:3,...preferences};
  }},{account:a,editor:{account_id:'a',kind:'group',id:'g',name:'保留我的名称',seat_ids:['2','1'],creating:false,dirty:true,base_revision:1,baseline:{name:'我的组',seat_ids:['2','1']}}});
  await e.p.reload();await e.p.saveEditor();assert.equal(writes[0].revision,1);assert.equal(e.props.editor.name,'保留我的名称');
  await e.p.reload(true);assert.equal(e.props.editor.base_revision,2);assert.deepEqual([...e.props.editor.seat_ids],['1','2']);
  await e.p.saveEditor();assert.equal(writes[1].revision,2);assert.deepEqual([...writes[1].preferences.groups[0].seat_ids],['1','2']);e.scope.stop();
});
test('restored tag input cannot silently adopt a newer metadata revision',async()=>{
  const a=structuredClone(account);a.seat_preferences_revision=2;let sent;
  const e=panel({saveSeatPreferences:async(id,preferences,revision)=>{sent=revision;throw new Error('版本冲突');}},
    {account:a,editor:{account_id:'a',kind:'tags',seat_id:'1',text:'旧的待保存输入',dirty:true,base_revision:1,baseline:{tags:['自定义']}}});
  await e.p.saveEditor();assert.equal(sent,1);assert.equal(e.props.editor.text,'旧的待保存输入');e.scope.stop();
});
test('explicit reload adopts untouched remote tags and never recreates a deleted group',async()=>{
  const remote={account_id:'a',revision:2,favorites:[{seat_id:'1',tags:['新标签']}],groups:[]};let writes=0;
  const e=panel({getSeatPreferences:async()=>remote,saveSeatPreferences:async()=>{writes++;}});
  e.p.openTags(e.p.data.value.favorites[0]);await e.p.reload(true);
  assert.equal(e.props.editor.text,'新标签');assert.equal(e.props.editor.base_revision,2);
  e.props.editor={account_id:'a',kind:'group',id:'g',name:'保留名称',seat_ids:['1'],creating:false,dirty:true,base_revision:1};
  await e.p.reload(true);assert.equal(e.props.editor.name,'保留名称');assert.equal(e.props.editor.base_revision,1);
  assert.match(e.p.error.value,/删除/);await e.p.saveEditor();assert.equal(writes,0);e.scope.stop();
});
test('real seat library renders personal labels and group order without predefined tags',async()=>{
  const {descriptor}=parse(readFileSync(new URL('components/SeatLibrary.vue',frontend),'utf8'));
  let code=compileScript(descriptor,{id:'library',inlineTemplate:true,templateOptions:{compilerOptions:{isCustomElement:tag=>['view','text'].includes(tag)}}}).content;
  code=code.replace(/from (['"])vue\1/g,`from '${pathToFileURL(require.resolve('vue')).href}'`)
    .replace(/(['"])@\/services\/([^'"]+)\1/g,(_,q,path)=>JSON.stringify(new URL('services/'+path,frontend).href));
  const component=(await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
  const html=await renderToString(Vue.createSSRApp(component,{account,seats,visibleSeats:seats,selected:['1'],sessionKey:1,editor:{account_id:'a',kind:'group',id:'g',name:'我的组',seat_ids:['2','1'],dirty:false}}));
  assert.match(html,/我的收藏/);assert.match(html,/偏好分组/);assert.match(html,/我的组/);assert.match(html,/A002.*A001/s);
  assert.doesNotMatch(html,/有插座/);
});
