import test from 'node:test';
import assert from 'node:assert/strict';
import {createNotificationController} from '../../AL-FrontEnd/services/notification_settings.mjs';
const settings=(id='a',revision=1)=>({account_id:id,revision,verified:true,address_masked:'a***@example.com',configured:true,results_enabled:false,reminders_enabled:false,reminder_minutes:30});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {resolve,reject,promise};};
function setup(overrides={}){
  const calls=[],api={getNotificationSettings:async id=>settings(id),
    requestNotificationVerification:async(id,data)=>{calls.push(['request',id,data]);return {...settings(id),challenge_id:'challenge',expires_at:99};},
    confirmNotificationVerification:async(id,data)=>{calls.push(['confirm',id,data]);return settings(id,2);},
    updateNotificationSettings:async(id,data)=>{calls.push(['save',id,data]);return {...settings(id,data.revision+1),...data,revision:data.revision+1};},...overrides};
  const controller=createNotificationController(api);controller.switchAccount('a','session-1');return {controller,state:controller.state,calls};
}
test('load opens explicit draft, cancel restores saved preferences and clears private inputs',async()=>{
  const {controller:c,state:s}=setup();await c.reload();c.edit();s.draft.results_enabled=true;s.address='private@example.com';s.code='123456';
  assert.equal(c.dirty(),true);assert.equal(c.cancel(),true);assert.equal(s.open,false);
  assert.equal(s.draft.results_enabled,false);assert.equal(s.address,'');assert.equal(s.code,'');assert.equal(s.settings.revision,1);
});
test('verification request and confirmation use current account with separate steps',async()=>{
  const {controller:c,state:s,calls}=setup();await c.reload();c.edit();s.address=' private@example.com ';
  assert.equal(await c.request(),true);assert.equal(s.challenge.id,'challenge');assert.equal(s.settings.verified,true);
  s.code='123456';assert.equal(await c.confirm(),true);assert.equal(s.challenge,null);assert.equal(s.address,'');
  assert.deepEqual(calls,[['request','a',{address:'private@example.com'}],['confirm','a',{challenge_id:'challenge',code:'123456'}]]);
});
test('saving has independent revision and converts minute draft without changing account',async()=>{
  const {controller:c,state:s,calls}=setup();await c.reload();c.edit();s.draft.results_enabled=true;s.draft.reminders_enabled=true;s.draft.reminder_minutes='45';
  await c.save();assert.deepEqual(calls[0],['save','a',{revision:1,results_enabled:true,reminders_enabled:true,reminder_minutes:45}]);
  assert.equal(s.settings.revision,2);assert.equal(s.open,false);assert.equal(s.settings.reminder_minutes,45);
});
test('old account loading response cannot replace new account or reset its busy state',async()=>{
  const old=deferred(),{controller:c,state:s}=setup({getNotificationSettings:id=>id==='a'?old.promise:Promise.resolve(settings(id,4))});
  const pending=c.reload();c.switchAccount('b','session-1');await c.reload();old.resolve(settings('a'));await pending;
  assert.equal(s.settings.account_id,'b');assert.equal(s.settings.revision,4);assert.equal(s.loading,false);
});
test('session change and close invalidate pending verification and clear destination/code',async()=>{
  for(const action of ['session','close']){
    const pending=deferred(),{controller:c,state:s}=setup({requestNotificationVerification:()=>pending.promise});
    await c.reload();c.edit();s.address='private@example.com';const request=c.request();
    assert.equal(s.busy,true);assert.equal(c.cancel(),false);
    if(action==='session')c.switchAccount('a','session-2');else c.close();
    pending.resolve({...settings(),challenge_id:'old'});await request;
    assert.equal(s.challenge,null);assert.equal(s.address,'');assert.equal(s.busy,false);
  }
});
test('failed save retains editable draft and error; retry uses same settings revision',async()=>{
  let fail=true;const {controller:c,state:s}=setup({updateNotificationSettings:async(id,data)=>{if(fail)throw new Error('网络中断');return {...settings(id,2),...data,revision:2};}});
  await c.reload();c.edit();s.draft.reminder_minutes='40';assert.equal(await c.save(),false);
  assert.equal(s.error,'网络中断');assert.equal(s.draft.reminder_minutes,'40');assert.equal(s.open,true);assert.equal(s.busy,false);
  fail=false;assert.equal(await c.save(),true);assert.equal(s.error,'');
});
test('mismatched server account is rejected and saved data stays untouched',async()=>{
  const {controller:c,state:s}=setup({getNotificationSettings:async()=>settings('wrong')});
  assert.equal(await c.reload(),false);assert.equal(s.settings,null);assert.match(s.error,/账号响应不一致/);
});
test('reload deliberately discards draft only after successful response',async()=>{
  const {controller:c,state:s}=setup();await c.reload();c.edit();s.draft.reminder_minutes=90;s.address='other@example.com';
  await c.reload();assert.equal(s.draft.reminder_minutes,30);assert.equal(s.address,'');assert.equal(s.challenge,null);
});
test('requesting code preserves unrelated notification draft and pending verification prevents silent save',async()=>{
  const {controller:c,state:s,calls}=setup();await c.reload();c.edit();s.draft.reminders_enabled=true;s.draft.reminder_minutes=45;s.address='new@example.com';
  await c.request();assert.equal(s.draft.reminders_enabled,true);assert.equal(s.draft.reminder_minutes,45);
  assert.equal(await c.save(),false);assert.match(s.error,/先完成或取消邮箱验证/);
  assert.equal(c.cancelVerification(),true);await c.save();assert.equal(calls.at(-1)[0],'save');assert.equal(s.settings.reminder_minutes,45);
});
