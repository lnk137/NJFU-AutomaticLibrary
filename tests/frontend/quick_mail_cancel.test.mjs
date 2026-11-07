import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import * as cancellation from '../../AL-FrontEnd/services/cancellation.mjs';
import * as links from '../../AL-FrontEnd/services/reservation_link.mjs';
const require=createRequire(new URL('../../AL-FrontEnd/package.json',import.meta.url));
const {reactive,ref,computed,watch,nextTick}=require('vue');
const cache=()=>{const values=new Map();return {setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k),removeItem:k=>values.delete(k)};};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const record={id:'receipt-1',student_id:'99000001',seat_id:'seat-8',begin:'2026-11-07 14:00:00',end:'2026-11-07 18:00:00',status:1027};
const preview={cancel_id:'cancel-1',expires_at:200,preview_token:'preview-private',record};
function controller(overrides={}){
  let at=100;const context={accountId:'a',recordId:record.id,revision:3,session:1},calls=[];
  const api={previewCancellation:async(id,data)=>{calls.push(['preview',id,data]);return {...preview};},submitCancellation:async(id,token)=>{calls.push(['submit',id,token]);return {state:'success',message:'已取消'};},getCancellation:async()=>({state:'uncertain'}),...overrides};
  const subject=cancellation.createCancellationController({api,context:()=>context,cache:cache(),clock:()=>at});subject.reset();
  return {subject,state:subject.state,calls,context,clock:value=>at=value};
}
test('mail intent survives an account-bound login return while ordinary links stay ordinary',()=>{
  const storage=cache();links.rememberReservationLink('a','receipt?x',storage,'cancel');
  assert.equal(links.takeReservationLink({role:'student',account_id:'a'},storage),'/pages/reservation_info/reservation_info?account_id=a&record_id=receipt%3Fx&intent=cancel');
  links.rememberReservationLink('a','receipt?x',storage);
  assert.equal(links.takeReservationLink({role:'student',account_id:'a'},storage),'/pages/reservation_info/reservation_info?account_id=a&record_id=receipt%3Fx');
});
test('a wrong login cannot consume another account cancellation target',()=>{
  const storage=cache();links.rememberReservationLink('a',record.id,storage,'cancel');
  assert.equal(links.takeReservationLink({role:'student',account_id:'b'},storage),null);
  assert.match(links.takeReservationLink({role:'student',account_id:'a'},storage)||'',/intent=cancel$/);
});
test('a cancellation return expires after thirty minutes and ignores unknown intent',()=>{
  const storage=cache(),now=Date.now;let at=100000;Date.now=()=>at;
  try{links.rememberReservationLink('a',record.id,storage,'cancel');at+=1800001;assert.equal(links.takeReservationLink({role:'root'},storage),null);
    links.rememberReservationLink('a',record.id,storage,'submit');assert.doesNotMatch(links.takeReservationLink({role:'root'},storage),/intent=/);
  }finally{Date.now=now;}
});
test('opening a mail cancellation previews the exact target and requires explicit confirmation',async()=>{
  const p=controller();await p.subject.open({preview:true});
  assert.equal(p.state.open,true);assert.deepEqual(p.state.preview.record,record);
  assert.deepEqual(p.calls,[['preview','a',{record_id:'receipt-1',expected_revision:3}]]);
  await p.subject.confirm();assert.equal(p.calls.filter(x=>x[0]==='submit').length,1);
});
test('expired preview refreshes seat and time without submitting in that confirmation action',async()=>{
  const p=controller();await p.subject.prepare();p.clock(201);await p.subject.confirm();
  assert.equal(p.calls.filter(x=>x[0]==='submit').length,0);assert.equal(p.calls.filter(x=>x[0]==='preview').length,2);
  assert.equal(p.state.pending,null);assert.match(p.state.error,/重新.*确认|再次确认/);
});
test('a server-expired preview permits a fresh preview and still needs another confirmation',async()=>{
  let submissions=0;const p=controller({submitCancellation:async()=>{submissions++;throw Object.assign(new Error('取消确认已过期'),{code:'preview_expired'});}});
  await p.subject.prepare();await p.subject.confirm();assert.equal(submissions,1);assert.equal(p.state.pending,null);
  assert.equal(p.calls.filter(x=>x[0]==='preview').length,2);assert.equal(p.state.result,null);
});
test('uncertain cancellation stays bound to its pending ID when mail preview is requested again',async()=>{
  const p=controller({submitCancellation:async()=>{throw new Error('timeout');}});await p.subject.prepare();await p.subject.confirm();
  await p.subject.open({preview:true});await p.subject.prepare();await p.subject.confirm();
  assert.equal(p.calls.filter(x=>x[0]==='preview').length,1);assert.equal(p.state.pending.id,'cancel-1');assert.equal(p.state.result.state,'uncertain');
});
test('a preview from the previous target cannot submit after a route context change',async()=>{
  const p=controller();await p.subject.prepare();p.context.recordId='another-receipt';
  assert.equal(await p.subject.confirm(),false);assert.equal(p.calls.filter(x=>x[0]==='submit').length,0);
});
test('a late expired response cannot clear a different target pending request',async()=>{
  const wait=deferred(),p=controller({submitCancellation:()=>wait.promise});await p.subject.prepare();const work=p.subject.confirm();
  p.context.recordId='another-receipt';p.state.pending={id:'other-cancellation',expires_at:400};
  wait.resolve(Promise.reject(Object.assign(new Error('expired'),{code:'preview_expired'})));await work;
  assert.equal(p.state.pending?.id,'other-cancellation');assert.equal(p.calls.filter(x=>x[0]==='preview').length,1);
});
const script=path=>readFileSync(new URL('../../AL-FrontEnd/'+path,import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
function component(apiOverrides={}){
  const props=reactive({accountId:'a',revision:3,record:{id:record.id},sessionKey:1,seatMap:{},disabled:false,allowLive:true,autoPreview:true,standalone:true,targetKey:0});
  const calls=[],events=[],storage=cache(),api={previewCancellation:async(id,data)=>{calls.push(['preview',id,data]);return preview;},submitCancellation:async()=>{calls.push(['submit']);return {state:'success'};},...apiOverrides};
  let dispose;const context={reactive,watch,onBeforeUnmount:f=>dispose=f,defineProps:()=>props,defineEmits:()=>((...args)=>events.push(args)),api,newCancellationState:cancellation.newCancellationState,createCancellationController:options=>cancellation.createCancellationController({...options,cache:storage,clock:()=>100})};
  vm.runInNewContext(script('components/CancelReservation.vue')+'\nglobalThis.subject={state,controller};',context);
  return {...context.subject,props,calls,events,dispose:()=>dispose?.()};
}
test('the standalone mail component previews on mount and a target change invalidates a late response',async()=>{
  const wait=deferred(),p=component({previewCancellation:()=>wait.promise});
  assert.equal(p.state.open,true);assert.equal(p.state.busy,true);
  p.props.record={id:'another-receipt'};await nextTick();wait.resolve(preview);await nextTick();await nextTick();
  assert.equal(p.state.preview?.record?.id==='receipt-1',false);assert.equal(p.calls.filter(x=>x[0]==='submit').length,0);p.dispose();
});
test('a mail target can retry preview errors without opening or submitting another target',async()=>{
  let attempts=0;const p=component({previewCancellation:async(id,data)=>{attempts++;assert.equal(id,'a');assert.equal(data.record_id,record.id);if(attempts===1)throw new Error('预约记录已变化，请重新查询');return preview;}});
  await nextTick();await nextTick();assert.match(p.state.error,/变化/);assert.equal(p.state.preview,null);
  await p.controller.prepare();assert.equal(p.state.preview.record.id,record.id);assert.equal(p.calls.filter(x=>x[0]==='submit').length,0);p.dispose();
});
function page({records=[],user={role:'student',account_id:'a'},refresh}={}){
  const hooks={},storage=cache(),calls=[],account={id:'a',revision:3},store=reactive({user,accounts:[account],activeAccountId:'a',sessionEpoch:1,snapshots:{a:{checked_at:'2026-11-06T15:00:00',records}},seatMap:{},status:{allow_live:true},get activeAccount(){return this.accounts.find(a=>a.id===this.activeAccountId);},refresh:refresh||async function(){},select(id){this.activeAccountId=id;},async records(force){calls.push(['records',this.activeAccountId,force]);},invalidate(){}});
  const context={ref,computed,watch,...links,rememberReservationLink:(accountId,recordId,_storage,intent)=>links.rememberReservationLink(accountId,recordId,storage,intent),clearReservationLink:()=>links.clearReservationLink(storage),useWorkspace:()=>store,onLoad:f=>hooks.load=f,onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,uni:{},schoolRecordLabel:()=>''};
  vm.runInNewContext(script('pages/reservation_info/reservation_info.vue')+'\nglobalThis.subject={detailId,linkMessage,query,select,mailTarget:typeof mailTarget===\"undefined\"?null:mailTarget,showDetail:typeof showDetail===\"undefined\"?null:showDetail};',context);
  return {...context.subject,hooks,store,calls,storage};
}
test('mail route opens a dedicated exact target even when no cached school record exists',async()=>{
  const p=page();p.hooks.load({account_id:'a',record_id:record.id,intent:'cancel'});await p.hooks.show();
  assert.equal(p.mailTarget?.value?.id,record.id);assert.equal(p.detailId.value,'');p.hooks.hide();
});
test('a session expiry during mail preview retains the same account and intent for login',async()=>{
  const p=page();p.hooks.load({account_id:'a',record_id:record.id,intent:'cancel'});await p.hooks.show();p.hooks.hide();
  assert.equal(links.takeReservationLink({role:'student',account_id:'a'},p.storage),'/pages/reservation_info/reservation_info?account_id=a&record_id=receipt-1&intent=cancel');
});
test('ordinary detail links continue to open the selected record without a cancellation target',async()=>{
  const p=page({records:[record]});p.hooks.load({account_id:'a',record_id:record.id});await p.hooks.show();
  assert.equal(p.detailId.value,record.id);assert.equal(p.mailTarget?.value,null);p.hooks.hide();
});
test('a foreign login and a hidden or changed route cannot expose the mail target',async()=>{
  const foreign=page({user:{role:'student',account_id:'b'}});foreign.hooks.load({account_id:'a',record_id:record.id,intent:'cancel'});await foreign.hooks.show();
  assert.equal(foreign.mailTarget?.value,null);assert.match(foreign.linkMessage.value,/对应账号|不属于/);foreign.hooks.hide();
  const wait=deferred(),p=page({refresh:()=>wait.promise});p.hooks.load({account_id:'a',record_id:record.id,intent:'cancel'});const work=p.hooks.show();p.hooks.hide();wait.resolve();await work;assert.equal(p.mailTarget?.value,null);
});
test('selecting another record dismisses the mail preview instead of stacking a detail dialog',async()=>{
  const p=page({records:[record]});p.hooks.load({account_id:'a',record_id:record.id,intent:'cancel'});await p.hooks.show();
  assert.equal(typeof p.showDetail,'function');p.showDetail('other');assert.equal(p.mailTarget.value,null);assert.equal(p.detailId.value,'other');p.hooks.hide();
});
