import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
import * as scheduled from '../../AL-FrontEnd/services/scheduled_segments.mjs';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
import * as inbox from '../../AL-FrontEnd/services/inbox_status.mjs';
import * as preferences from '../../AL-FrontEnd/services/seat_preferences.mjs';
import * as exceptions from '../../AL-FrontEnd/services/date_exceptions.mjs';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function page(api={},existingStore=null,clock=Date){
  const hooks={},account={id:'old',student_id:'99000001',alias:'Old',revision:1,plan:{mode:'day',time_mode:'full_day',begin:'07:30',end:'22:00',trigger:'07:00:00',seat_ids:['s1'],day_offset:1,week_times:{},enabled:false}};
  const store=existingStore||{user:{role:'student'},sessionEpoch:1,activeAccountId:'old',accounts:[account],drafts:{},seats:[],seatMap:{},status:{allow_live:true},policy:{open:'07:30',close:'22:00',friday_close:'20:00',day_offset:1,booking_open:'07:00:00'},get activeAccount(){return this.accounts.find(a=>a.id===this.activeAccountId);},refreshCalls:0,selectCalls:0,async refresh(){this.refreshCalls++;},select(id){this.selectCalls++;this.activeAccountId=id;},invalidate(){}};
  const ref=value=>({value}),computed=fn=>({get value(){return fn();}});
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/account_info/account_info.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const prompts=[];let confirm=true;
  const context={...scheduled,...drafts,...inbox,...preferences,...exceptions,Date:clock,ref,computed,watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},onLoad:f=>hooks.load=f,onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,onUnload:f=>hooks.unload=f,useWorkspace:()=>store,api,uni:{showModal:options=>{prompts.push(options);options.success({confirm});}},setInterval,clearInterval};
  vm.runInNewContext(source+'\nload();globalThis.subject={verify,check,save,password,message,todayEditor,plan,alias,student,modal,pendingSeats,timeDraft,timeWeek,triggerDraft,search,pageReady,pageLoading,openModal,openSeats,openTime,openAdvanced,closeModal,closeToday,openScheduled,applyScheduled,scheduledDraft,switchAccount,beforeSessionLeave,reload,bookingScope,setBookingMode,load,readDraft,pollInbox,error,activationState:typeof activationState===\"undefined\"?null:activationState,exceptionsSaved:typeof exceptionsSaved===\"undefined\"?null:exceptionsSaved};',context);
  vm.runInNewContext('globalThis.subject.toggle=toggle;Object.assign(globalThis.subject,{applySeats,applySeatGroup:typeof applySeatGroup===\"undefined\"?null:applySeatGroup,preferencesSaved:typeof preferencesSaved===\"undefined\"?null:preferencesSaved,libraryEditor:typeof libraryEditor===\"undefined\"?null:libraryEditor});',context);
  vm.runInNewContext('Object.assign(globalThis.subject,{refreshTodayResults,historyRefresh:typeof historyRefresh===\"undefined\"?null:historyRefresh});',context);
  context.subject.pageReady.value=true;context.subject.pageLoading.value=false;return {...context.subject,store,hooks,prompts,setConfirm:value=>confirm=value};
}
function leave(p){p.hooks.hide();p.store.sessionEpoch++;p.store.activeAccountId='new';p.store.accounts=[{id:'new',student_id:'99000002',alias:'New',revision:1}];}
test('a completed same-day batch refreshes account history without changing the scheduled draft',async()=>{
  const p=page();p.plan.value.trigger='07:00:09';
  await p.refreshTodayResults();
  assert.equal(p.store.refreshCalls,1);assert.equal(p.historyRefresh?.value,1);
  assert.equal(p.plan.value.trigger,'07:00:09');
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/account_info/account_info.vue',import.meta.url),'utf8');
  assert.match(source,/<AccountHistory[^>]+:key="[^"]*historyRefresh/);
});
test('a late batch completion does not refresh the newly selected account history',async()=>{
  const wait=deferred(),p=page();p.store.refresh=()=>wait.promise;
  const pending=p.refreshTodayResults();leave(p);wait.resolve();await pending;
  assert.equal(p.historyRefresh?.value??0,0);
});
test('applying a personal group changes only pending seats until explicit apply and save',()=>{
  const p=page();p.openSeats();assert.equal(typeof p.applySeatGroup,'function');
  const group=['s2','s1'];p.applySeatGroup(group);group.reverse();
  assert.deepEqual([...p.pendingSeats.value],['s2','s1']);assert.deepEqual([...p.plan.value.seat_ids],['s1']);
  p.applySeats();assert.deepEqual([...p.plan.value.seat_ids],['s2','s1']);
  assert.deepEqual([...p.store.activeAccount.plan.seat_ids],['s1']);assert.equal(p.store.refreshCalls,0);
});
test('library save leaves account and unsaved plan revisions untouched',()=>{
  const p=page();p.plan.value.begin='10:00';assert.equal(typeof p.preferencesSaved,'function');
  p.preferencesSaved({account_id:'old',revision:1,favorites:[{seat_id:'s2',tags:['自定义']}],groups:[]});
  assert.equal(p.readDraft().revision,1);assert.equal(p.store.activeAccount.revision,1);assert.equal(p.plan.value.begin,'10:00');
  assert.equal(p.store.activeAccount.seat_preferences_revision,1);
  leave(p);p.preferencesSaved({account_id:'old',revision:2,favorites:[],groups:[]});
  assert.equal(p.store.activeAccountId,'new');assert.equal(p.store.activeAccount.seat_preferences,undefined);
});
test('unsaved library text survives a drawer/page return and protects closing the drawer',async()=>{
  const p=page();p.openSeats();assert.ok(p.libraryEditor);
  p.libraryEditor.value={account_id:'old',kind:'tags',seat_id:'s1',text:'保留我的输入',dirty:true};
  assert.equal(p.readDraft().editor.dirty,true);p.setConfirm(false);await p.closeModal();assert.equal(p.modal.value,'seats');
  p.hooks.hide();const next=page({},p.store);assert.equal(next.modal.value,'seats');assert.equal(next.libraryEditor.value.text,'保留我的输入');
});
test('seat application cannot silently drop an unfinished library edit',()=>{
  const p=page();p.openSeats();assert.ok(p.libraryEditor);
  p.libraryEditor.value={account_id:'old',kind:'group',name:'未保存',dirty:true};p.pendingSeats.value=['s2'];
  p.applySeats();assert.equal(p.modal.value,'seats');assert.deepEqual([...p.plan.value.seat_ids],['s1']);
});
test('enabling a plan confirms the effective one-day seats rather than the base seats',async()=>{
  class FixedDate extends Date{constructor(...args){super(...(args.length?args:['2026-10-24T06:00:00+08:00']));}}
  const p=page({savePlan:async()=>({account:{...p.store.activeAccount,revision:2,plan:{...p.plan.value,enabled:true}}})},null,FixedDate);
  p.store.seatMap={s1:{name:'Base seat'},s2:{name:'One-day seat'}};
  p.store.accounts[0].date_exceptions={'2026-10-25':{action:'override',begin:'14:00',end:'18:00',seat_ids:['s2','s1']}};
  await p.toggle();assert.match(p.prompts[0].content,/2026-10-25/);
  assert.match(p.prompts[0].content,/One-day seat → Base seat/);
});
test('saving exceptions advances only the matching revision and preserves the base-plan draft',()=>{
  const p=page();p.plan.value.begin='10:00';p.plan.value.end='14:00';
  assert.equal(typeof p.exceptionsSaved,'function');
  p.exceptionsSaved({...p.store.activeAccount,revision:2,date_exceptions:{'2026-10-25':{action:'skip'}}});
  assert.equal(p.readDraft().revision,2);assert.equal(p.plan.value.begin,'10:00');assert.equal(p.plan.value.end,'14:00');
  assert.equal(p.store.activeAccount.date_exceptions['2026-10-25'].action,'skip');
});
test('an exception response cannot overwrite another account or advance a stale draft',()=>{
  const p=page();assert.equal(typeof p.exceptionsSaved,'function');
  p.store.accounts[0].revision=5;p.exceptionsSaved({...p.store.activeAccount,revision:6,date_exceptions:{}});
  assert.equal(p.readDraft().revision,1);
  leave(p);p.exceptionsSaved({id:'old',revision:7,date_exceptions:{}});
  assert.equal(p.store.activeAccountId,'new');assert.equal(p.store.activeAccount.revision,1);
});
test('an old account verification cannot select or refresh after the page/session changes',async()=>{
  const wait=deferred(),p=page({verifyAccount:()=>wait.promise});p.password.value='temporary';const operation=p.verify();leave(p);wait.resolve({account:{id:'old',student_id:'99000001',alias:'Old',revision:2,plan:p.plan.value}});await operation;
  assert.equal(p.store.selectCalls,0);assert.equal(p.store.refreshCalls,0);assert.equal(p.message.value,'');assert.equal(p.password.value,'');
});
test('late saved-plan responses cannot overwrite a newly selected session',async()=>{
  const wait=deferred(),p=page({savePlan:()=>wait.promise});const operation=p.save();leave(p);
  wait.resolve({account:{id:'old',student_id:'99000001',alias:'Old',revision:2,plan:p.plan.value}});await operation;
  assert.equal(p.store.refreshCalls,0);assert.equal(p.message.value,'');
});
for(const enabled of [false,true])test('confirmed '+(enabled?'pause':'enable')+' survives a failed follow-up refresh and advances the saved revision',async()=>{
  const revisions=[],p=page({savePlan:async(id,plan,revision)=>{revisions.push(revision);return {account:{...p.store.activeAccount,revision:revision+1,plan:structuredClone(plan)}};}});
  p.store.accounts[0].plan.enabled=enabled;p.load();
  p.store.refresh=async()=>{throw new Error('refresh unavailable');};
  await p.toggle();
  assert.equal(p.plan.value.enabled,!enabled);
  assert.equal(p.store.activeAccount.plan.enabled,!enabled);
  assert.equal(p.readDraft().revision,2);
  assert.equal(drafts.isDirty(p.readDraft()),false);
  assert.match(p.message.value,/已保存/);assert.match(p.message.value,/刷新/);
  assert.equal(await p.save(),true);assert.deepEqual(revisions,[1,2]);
});
test('an acknowledged offline save remains pending when the follow-up refresh fails',async()=>{
  const p=page({savePlan:async()=>({pending:true,receipt:{state:'pending',version:1}})});
  p.store.refresh=async()=>{throw new Error('refresh unavailable');};
  await p.toggle();
  assert.equal(p.plan.value.enabled,true);assert.equal(p.store.activeAccount.plan.enabled,false);
  assert.equal(p.readDraft().revision,1);assert.equal(drafts.isDirty(p.readDraft()),false);
  assert.match(p.message.value,/已保存到云端/);assert.match(p.message.value,/尚未生效/);assert.match(p.message.value,/刷新/);
});
test('a rejected plan write still restores the previous enable state',async()=>{
  const p=page({savePlan:async()=>{throw new Error('write rejected');}});
  await p.toggle();assert.equal(p.plan.value.enabled,false);assert.equal(p.readDraft().revision,1);
  assert.equal(p.store.refreshCalls,0);assert.match(p.message.value,/write rejected/);
});
test('late school errors remain scoped to the initiating page/session',async()=>{
  const wait=deferred(),p=page({checkAccount:()=>wait.promise});const operation=p.check();leave(p);
  wait.reject(new Error('private old account failure'));await operation;assert.equal(p.message.value,'');
});

test('confirming discard restores account fields and Today values while preserving the outer plan draft',async()=>{
  const p=page();p.openModal('account');p.alias.value='Discarded name';p.student.value='99000999';p.password.value='must-not-persist';await p.closeModal();assert.equal(p.alias.value,'Old');assert.equal(p.student.value,'99000001');assert.equal(p.password.value,'');
});
test('discarding a segmented Today draft preserves the outer scheduled draft',async()=>{
  const p=page();p.plan.value.trigger='07:00:09';p.setBookingMode(0);
  p.todayEditor.value={account_id:'old',date:drafts.todayDefaults(p.store.policy).date,segments:[{begin:'14:00',end:'18:00',seat_ids:['s2']}],dirty:true};
  await p.closeToday();assert.equal(p.todayEditor.value,null);assert.equal(p.plan.value.trigger,'07:00:09');
});
test('pending batch identity survives closing and changing accounts without carrying another account draft',async()=>{
  const p=page(),date=drafts.todayDefaults(p.store.policy).date;
  p.todayEditor.value={account_id:'old',date,segments:[{begin:'14:00',end:'18:00',seat_ids:['s1']}],dirty:false,pending:{batch_id:'confirmed-once',expires_at:date+'T08:02:00+08:00'}};
  p.setBookingMode(0);await p.closeToday();assert.equal(p.todayEditor.value.pending.batch_id,'confirmed-once');
  p.store.accounts.push({id:'new',student_id:'99000002',alias:'New',revision:1});await p.switchAccount('new');
  assert.equal(p.store.drafts.old.todayEditor.pending.batch_id,'confirmed-once');assert.equal(p.todayEditor.value,null);
  await p.switchAccount('old');assert.equal(p.todayEditor.value.pending.batch_id,'confirmed-once');
});
test('leaving retains only the pending batch when other unsaved changes are discarded',async()=>{
  const p=page(),date=drafts.todayDefaults(p.store.policy).date;
  p.todayEditor.value={account_id:'old',date,segments:[{begin:'14:00',end:'18:00',seat_ids:['s1']}],dirty:false,pending:{batch_id:'confirmed-once'}};
  p.plan.value.trigger='07:00:09';assert.equal(await p.beforeSessionLeave(),true);
  assert.equal(p.store.drafts.old.todayEditor.pending.batch_id,'confirmed-once');
  assert.equal(p.store.drafts.old.plan.trigger,'07:00:00');
});
test('reloading saved configuration keeps the identity of a confirmation still being checked',async()=>{
  const p=page(),date=drafts.todayDefaults(p.store.policy).date;
  p.todayEditor.value={account_id:'old',date,segments:[{begin:'14:00',end:'18:00',seat_ids:['s1']}],dirty:false,pending:{batch_id:'still-checking',expires_at:date+'T08:02:00+08:00'}};
  await p.reload();assert.equal(p.todayEditor.value?.pending?.batch_id,'still-checking');
});
for(const type of ['seats','time','advanced'])test('browser return restores unapplied '+type+' drawer in a fresh page instance',()=>{
    const p=page();
    if(type==='seats'){p.openSeats();p.pendingSeats.value.push('s2');p.search.value='6F-A';}
    if(type==='time'){p.openTime();p.timeDraft.value.begin='10:00';}
    if(type==='advanced'){p.openAdvanced();p.triggerDraft.value='07:00:09';}
    if(type==='advanced')p.hooks.unload();else p.hooks.hide();const next=page({},p.store);assert.equal(next.modal.value,type);
    if(type==='seats'){assert.deepEqual([...next.pendingSeats.value],['s1','s2']);assert.equal(next.search.value,'6F-A');}
    if(type==='time')assert.equal(next.timeDraft.value.begin,'10:00');
    if(type==='advanced')assert.equal(next.triggerDraft.value,'07:00:09');
});
test('cached account drawers never contain passwords and never restore into another account',()=>{
  const p=page();p.password.value='must-not-persist';p.openModal('account');p.alias.value='Unsaved alias';p.hooks.hide();assert.doesNotMatch(JSON.stringify(p.store.drafts),/must-not-persist/);
  const own=page({},p.store);assert.equal(own.alias.value,'Unsaved alias');assert.equal(own.password.value,'');
  p.store.accounts.push({id:'new',student_id:'99000002',alias:'New',revision:1});p.store.activeAccountId='new';const other=page({},p.store);assert.equal(other.modal.value,'');assert.equal(other.alias.value,'New');
});

test('page readiness follows the current local configuration load and ignores a hidden page completion',async()=>{
  for(const hidden of [false,true]){const p=page(),wait=deferred();p.store.refresh=()=>wait.promise;const operation=p.hooks.show();assert.equal(p.pageReady.value,false);assert.equal(p.pageLoading.value,true);if(hidden)p.hooks.hide();wait.resolve();await operation;assert.equal(p.pageReady.value,!hidden);p.hooks.hide();}
});
test('pending then failed offline enable never claims the disabled effective plan is enabled',async()=>{
  let state='pending';const p=page({savePlan:async()=>({pending:true,receipt:{state:'pending',version:1}}),getStatus:async()=>({allow_live:true,inbox:{accounts:[{student_id:'99000001',state,message:'fixture conflict'}]}})});
  p.store.user={role:'student',student_id:'99000001',auth_mode:'offline'};
  p.store.refresh=async()=>{p.store.status={allow_live:true,inbox:{accounts:[{student_id:'99000001',state}]}};};
  p.plan.value.enabled=true;await p.save();state='failed';await p.pollInbox();
  assert.equal(p.store.activeAccount.plan.enabled,false);assert.ok(p.activationState);
  assert.match(p.activationState.value.label,/未生效/);assert.notEqual(p.activationState.value.tone,'green');
  assert.equal(p.error.value,true);assert.match(p.message.value,/失败|未生效/);p.hooks.hide();
});

test('multi editor keeps legacy plan unchanged until explicit apply and owns a resumable draft',async()=>{
  const p=page();p.openScheduled();assert.equal(p.modal.value,'scheduled');assert.equal(p.plan.value.segments,undefined);
  p.scheduledDraft.value.plan.segments[0].begin='09:00';p.scheduledDraft.value.plan.segments[0].end='12:00';
  assert.equal(p.readDraft().editor.dirty,true);p.setConfirm(false);await p.closeModal();assert.equal(p.modal.value,'scheduled');
  p.hooks.hide();const next=page({},p.store);assert.equal(next.modal.value,'scheduled');assert.equal(next.scheduledDraft.value.plan.segments[0].begin,'09:00');
  next.applyScheduled(next.scheduledDraft.value.plan);assert.equal(next.modal.value,'');assert.equal(next.plan.value.segments[0].begin,'09:00');
  assert.equal(next.store.activeAccount.plan.segments,undefined);
});
test('switching multi schedule daily and weekly preserves segment seats and IDs',()=>{
  const p=page();p.openScheduled();p.applyScheduled(p.scheduledDraft.value.plan);const id=p.plan.value.segments[0].id;
  p.setBookingMode(2);assert.equal(p.plan.value.mode,'week');assert.equal(p.plan.value.week_segments['1'][0].id,id);
  p.setBookingMode(1);assert.equal(p.plan.value.mode,'day');assert.equal(p.plan.value.segments[0].id,id);
});
test('calendar adjustment prefills only a new unsaved date draft and keeps leave protection',async()=>{
  const p=page(),saved=JSON.stringify(p.store.activeAccount);
  p.hooks.load({account_id:'old',adjust_date:'2099-01-02'});
  try{
    await p.hooks.show();const editor=p.readDraft().dateEditor;
    assert.equal(editor?.date,'2099-01-02');assert.equal(editor.action,'override');
    assert.equal(JSON.stringify(p.store.activeAccount),saved);assert.equal(p.modal.value,'');
    editor.begin='09:00';editor.dirty=true;p.setConfirm(false);
    assert.equal(await p.beforeSessionLeave(),false);assert.equal(p.readDraft().dateEditor.begin,'09:00');
  }finally{p.hooks.hide();}
});
test('calendar adjustment cannot replace an existing ordinary or popup draft',async()=>{
  for(const kind of ['plan','scheduled','date']){
    const original=page();
    if(kind==='plan')original.plan.value.trigger='07:00:09';
    else if(kind==='scheduled'){original.openScheduled();original.scheduledDraft.value.plan.segments[0].begin='10:00';}
    else{original.store.drafts.old={...original.readDraft(),dateEditor:{action:'override',date:'2099-01-03',dirty:true}};}
    if(kind!=='date')original.hooks.hide();
    const p=page({},original.store);p.hooks.load({account_id:'old',adjust_date:'2099-01-02'});
    try{
      await p.hooks.show();
      if(kind==='plan'){assert.equal(p.plan.value.trigger,'07:00:09');assert.equal(p.readDraft().dateEditor,null);}
      else if(kind==='scheduled'){assert.equal(p.modal.value,'scheduled');assert.equal(p.scheduledDraft.value.plan.segments[0].begin,'10:00');assert.equal(p.readDraft().dateEditor,null);}
      else assert.equal(p.readDraft().dateEditor.date,'2099-01-03');
      assert.match(p.message.value,/保留|草稿|编辑/);
    }finally{p.hooks.hide();}
  }
});
test('calendar adjustment rejects a foreign account and offline editing',async()=>{
  for(const kind of ['foreign','offline']){
    const p=page();if(kind==='offline')p.store.user={role:'student',auth_mode:'offline'};
    p.hooks.load({account_id:kind==='foreign'?'another':'old',adjust_date:'2099-01-02'});
    try{await p.hooks.show();assert.equal(p.readDraft().dateEditor,null);assert.ok(p.message.value);}
    finally{p.hooks.hide();}
  }
});
