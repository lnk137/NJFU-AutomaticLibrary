import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import * as scheduled from '../../AL-FrontEnd/services/scheduled_segments.mjs';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
import * as inbox from '../../AL-FrontEnd/services/inbox_status.mjs';
import * as preferences from '../../AL-FrontEnd/services/seat_preferences.mjs';
import * as exceptions from '../../AL-FrontEnd/services/date_exceptions.mjs';
import * as workspaceHelpers from '../../AL-FrontEnd/services/workspace.mjs';
const require=createRequire(new URL('../../AL-FrontEnd/package.json',import.meta.url));
const {reactive,ref,computed}=require('vue');
const clone=value=>JSON.parse(JSON.stringify(value));
const source=path=>readFileSync(new URL('../../AL-FrontEnd/'+path,import.meta.url),'utf8');
const script=path=>source(path).split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
const policy={open:'07:30',close:'22:00',friday_close:'20:00',day_offset:1,booking_open:'07:00:00'};
const plan={mode:'day',time_mode:'custom',begin:'09:00',end:'12:00',trigger:'07:00:00',seat_ids:['s1'],day_offset:1,week_times:{},enabled:false};
function baseStore(){return reactive({user:{role:'student'},sessionEpoch:1,activeAccountId:'a',accounts:[{id:'a',student_id:'99000001',alias:'A',revision:1,credential_revision:1,capabilities:{execution_mode:'dual_node',credential_sync_state:'matched'},plan:clone(plan)}],drafts:{},snapshots:{a:{records:[],checked_at:'2026-11-07T08:00:00+08:00'}},seats:[],seatMap:{s1:{name:'Seat 1'},s2:{name:'Seat 2'}},status:{allow_live:true},policy,runs:[],counts:{},nextCursor:null,get activeAccount(){return this.accounts.find(a=>a.id===this.activeAccountId);},async refresh(){},select(id){this.activeAccountId=id;},invalidate(){},async records(){},async loadRuns(){}});}
function accountPage(){
  let now=Date.parse('2026-11-07T06:59:59+08:00');class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  const store=baseStore(),hooks={},prompts=[],timers=[];let confirm=true;
  const context={...scheduled,...drafts,...inbox,...preferences,...exceptions,ref,computed,watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,onUnload:f=>hooks.unload=f,onLoad:f=>hooks.load=f,useWorkspace:()=>store,api:{},Date:Clock,setInterval:f=>{timers.push(f);return timers.length;},clearInterval:()=>{},uni:{showModal:o=>{prompts.push(o);o.success({confirm});}}};
  vm.runInNewContext(script('pages/account_info/account_info.vue')+'\nload();globalThis.subject={openScheduled,applyScheduled,plan,week,message,modal,scheduledDraft,readDraft,load,nextWindow,restoreOrdinary:typeof restoreOrdinary===\'function\'?restoreOrdinary:null,undoOrdinary:typeof undoOrdinary===\'function\'?undoOrdinary:null};',context);
  return {...context.subject,store,hooks,prompts,setConfirm:v=>confirm=v,advance:at=>{now=Date.parse(at);for(const tick of timers)tick();}};
}
test('managed accounts cannot enter or apply the unsupported scheduled-segment editor',()=>{
  const p=accountPage(),before=clone(p.plan.value);p.openScheduled();assert.notEqual(p.modal.value,'scheduled');assert.match(p.message.value,/双机|普通/);
  p.applyScheduled(scheduled.segmentedPlan(before));assert.deepEqual(clone(p.plan.value),before);
});
test('an old offline snapshot without execution capabilities cannot create a segmented plan',()=>{
  const p=accountPage();p.store.user.auth_mode='offline';delete p.store.accounts[0].capabilities;const before=clone(p.plan.value);p.openScheduled();assert.notEqual(p.modal.value,'scheduled');assert.match(p.message.value,/实时登录|执行方式/);p.applyScheduled(scheduled.segmentedPlan(before));assert.deepEqual(clone(p.plan.value),before);
});
test('recovering a managed segmented plan requires confirmation and remains reversible until saving',async()=>{
  const p=accountPage(),segmented={...clone(plan),segments:[{id:'a',begin:'09:00',end:'12:00',seat_ids:['s1']},{id:'b',begin:'14:00',end:'18:00',seat_ids:['s2']}],week_segments:{}};
  p.store.accounts[0].plan=clone(segmented);p.load(true);assert.equal(typeof p.restoreOrdinary,'function');
  p.setConfirm(false);await p.restoreOrdinary();assert.deepEqual(clone(p.plan.value),segmented);
  p.setConfirm(true);await p.restoreOrdinary();assert.equal(scheduled.isSegmentedPlan(p.plan.value),false);assert.equal(p.plan.value.enabled,false);assert.deepEqual(clone(p.store.activeAccount.plan),segmented);assert.match(p.prompts.at(-1).content,/09:00.*12:00/);assert.match(p.prompts.at(-1).content,/保存|草稿/);
  assert.equal(typeof p.undoOrdinary,'function');await p.undoOrdinary();assert.deepEqual(clone(p.plan.value),segmented);
});
test('undoing recovery cannot discard later ordinary-plan edits without confirmation',async()=>{
  const p=accountPage();p.store.accounts[0].plan={...clone(plan),segments:[{id:'a',begin:'09:00',end:'12:00',seat_ids:['s1']}],week_segments:{}};p.load(true);await p.restoreOrdinary();p.plan.value.begin='10:00';p.setConfirm(false);await p.undoOrdinary();assert.equal(p.plan.value.begin,'10:00');assert.equal(scheduled.isSegmentedPlan(p.plan.value),false);
});
test('weekly recovery retains first intervals and explicitly resolves differing seat orders',()=>{
  assert.equal(typeof scheduled.ordinaryPlanRecovery,'function');
  const original={...clone(plan),mode:'week',segments:[],week_segments:{1:[{id:'a',begin:'09:00',end:'12:00',seat_ids:['s1']},{id:'b',begin:'14:00',end:'18:00',seat_ids:['s2']}],5:[{id:'c',begin:'10:00',end:'14:00',seat_ids:['s2']}]}};
  const value=scheduled.ordinaryPlanRecovery(original);assert.equal(scheduled.isSegmentedPlan(value.plan),false);assert.deepEqual(value.plan.week_times,{'1':['09:00','12:00'],'5':['10:00','14:00']});assert.deepEqual(value.plan.seat_ids,['s1']);assert.equal(value.removed,1);assert.equal(value.seatsChanged,true);assert.equal(original.week_segments[1].length,2);
});
test('online plan preview advances after release without changing any draft input',async()=>{
  const p=accountPage();await p.hooks.show();assert.equal(p.nextWindow.value.release_date,'2026-11-07');const before=clone(p.plan.value);
  p.advance('2026-11-07T07:00:01+08:00');assert.equal(p.nextWindow.value.release_date,'2026-11-08');assert.deepEqual(clone(p.plan.value),before);p.hooks.hide();
});
function home(runApi=null){
  const store=baseStore(),hooks={},calls=[],timers=[];
  if(runApi){workspace(runApi,store);store.refresh=async()=>{};}
  else store.loadRuns=async(filters,append=false,preserve=false)=>{calls.push(['runs',append]);store.runs=append?[...store.runs,{id:'older'}]:preserve?[{id:'newest'},...store.runs.filter(row=>row.id!=='newest')]:[{id:'newest'}];};
  store.records=async()=>{calls.push(['records']);store.snapshots.a={records:[{id:'fresh',status:1027,begin:'2099-01-01 09:00:00',end:'2099-01-01 12:00:00',seat_id:'s1'}],checked_at:'2026-11-07T09:00:00+08:00'};};
  const context={...drafts,ref,computed,useWorkspace:()=>store,onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,onUnload:f=>hooks.unload=f,setInterval:f=>{timers.push(f);return 1;},clearInterval:()=>{},uni:{}};
  vm.runInNewContext(script('pages/index/index.vue')+'\nglobalThis.subject={refresh,loadMore,detailId,detail,confirmedRecord,calendarRef,recordsError:typeof recordsError===\'undefined\'?null:recordsError};',context);context.subject.calendarRef.value={async reload(){}};
  return {...context.subject,store,hooks,calls,poll:async()=>{for(const tick of timers)await tick();}};
}
test('manual overview refresh reads the latest local receipt snapshot even when a cache exists',async()=>{
  const p=home();await p.refresh();assert.equal(p.calls.filter(([name])=>name==='records').length,1);assert.equal(p.confirmedRecord.value.id,'fresh');
});
test('automatic refresh keeps loaded history pages and an open historical detail',async()=>{
  const p=home();p.hooks.show();await new Promise(resolve=>setImmediate(resolve));await p.loadMore();p.detailId.value='older';await p.poll();
  assert.ok(p.store.runs.some(row=>row.id==='older'));assert.equal(p.detail.value.id,'older');p.hooks.hide();
});
test('receipt refresh failure preserves the previous card and reports its stale state',async()=>{
  const p=home();p.store.records=async()=>{throw new Error('snapshot offline');};await p.refresh();assert.ok(p.recordsError);assert.match(p.recordsError.value,/snapshot offline/);assert.equal(p.store.snapshots.a.checked_at,'2026-11-07T08:00:00+08:00');
});
function workspace(api,existingStore=null){
  let options;const context={...workspaceHelpers,api,defineStore:(_,value)=>{options=value;},uni:{reLaunch(){}}};vm.runInNewContext(source('stores/workspace.js').replace(/^import .*\n/gm,'').replace('export const useWorkspace=','const useWorkspace='),context);
  const store=existingStore||{...options.state()};if(existingStore)for(const [key,value] of Object.entries(options.state()))if(!(key in store))store[key]=value;
  store.$reset=()=>Object.assign(store,options.state());for(const [name,method] of Object.entries(options.actions))store[name]=method.bind(store);return store;
}
for(const manual of [false,true])test((manual?'manual':'automatic')+' overview refresh updates an open detail and retains expanded history',async()=>{
  let updated=false,reads=0;const p=home({
    listRuns:async filters=>({items:filters.cursor?[{id:'older',status:'submitting'}]:[{id:'newest',status:'queued'}],counts:{total:2},next_cursor:filters.cursor?null:'page-2'}),
    getRun:async id=>{reads++;return {run:{id,status:updated?'success':'submitting'}};}
  });
  await p.refresh();await p.loadMore();p.detailId.value='older';updated=true;await p.refresh(manual);
  assert.equal(p.detail.value?.id,'older');assert.equal(p.detail.value.status,'success');assert.equal(reads,1);
  assert.deepEqual(clone(p.store.runs.map(row=>row.id)),['newest','older']);
});
test('automatic overview refresh updates a detail already present on the first page',async()=>{
  let updated=false;const p=home({listRuns:async()=>({items:[{id:'newest',status:updated?'success':'queued'}],counts:{total:1},next_cursor:null})});
  await p.refresh();p.detailId.value='newest';updated=true;await p.refresh(false);assert.equal(p.detail.value.status,'success');
});
test('retained history refresh is bounded and rotates older active records with detail priority',async()=>{
  const requests=[];let lists=0;const store=workspace({listRuns:async()=>{lists++;return {items:[{id:'newest'}],counts:{total:21},next_cursor:'first-next'};},getRun:async id=>{requests.push(id);return {run:{id,status:'submitting'}};}});
  await store.loadRuns();store.runs.push(...Array.from({length:20},(_,i)=>({id:'old-'+i,status:i===19?'success':'submitting'})));store.nextCursor='older-next';
  await store.loadRuns({},false,true,'old-19');assert.equal(lists,2);assert.equal(requests.length,4);assert.equal(requests[0],'old-19');assert.equal(store.nextCursor,'older-next');
  const first=requests.slice(1);await store.loadRuns({},false,true,'old-19');assert.equal(requests.length,8);assert.equal(requests[4],'old-19');assert.equal(requests.slice(5).some(id=>first.includes(id)),false);
});
test('an offline cache miss retains the open historical row with a visible stale notice',async()=>{
  const store=workspace({listRuns:async()=>({items:[{id:'newest'}],counts:{total:1},next_cursor:null,cache_only:true,cached_at:2,cache_limit:100}),getRun:async()=>{throw Object.assign(new Error('not cached'),{status:404});}});
  await store.loadRuns();store.runs.push({id:'older',status:'uncertain'});store.nextCursor='expired-cache-cursor';
  await store.loadRuns({},false,true,'older');assert.equal(store.runs.find(row=>row.id==='older').status,'uncertain');assert.match(store.runRefreshError,/缓存|刷新/);assert.equal(store.nextCursor,null);
});
test('a newly inserted full page uses its cursor to avoid skipping intermediate records',async()=>{
  let changed=false;const store=workspace({listRuns:async filters=>!changed?{items:[{id:'old'}],counts:{total:1},next_cursor:'older-tail'}:filters.cursor?{items:[{id:'middle'},{id:'old'}],counts:{total:3},next_cursor:'older-tail'}:{items:[{id:'new'}],counts:{total:3},next_cursor:'new-gap'}});
  await store.loadRuns();changed=true;await store.loadRuns({},false,true);assert.equal(store.nextCursor,'new-gap');
  await store.loadRuns({},false,true);assert.equal(store.nextCursor,'new-gap');
  assert.ok(store.runs.some(row=>row.id==='old'));
  await store.loadRuns({cursor:store.nextCursor},true);assert.deepEqual(clone(store.runs.map(row=>row.id)),['new','middle','old']);assert.equal(store.nextCursor,'older-tail');
});
test('multiple gap pages stay before the old history even before they reach an overlapping row',async()=>{
  let changed=false;const pages={gap1:{items:[{id:'new-4'},{id:'new-3'}],next_cursor:'gap2'},gap2:{items:[{id:'new-2'},{id:'new-1'}],next_cursor:'old-page'},'old-page':{items:[{id:'old-2'},{id:'old-1'}],next_cursor:null}};
  const store=workspace({listRuns:async filters=>({counts:{total:8},...(!changed?pages['old-page']:filters.cursor?pages[filters.cursor]:{items:[{id:'new-6'},{id:'new-5'}],next_cursor:'gap1'})})});
  await store.loadRuns();changed=true;await store.loadRuns({},false,true);
  for(const page of ['gap1','gap2','old-page']){assert.equal(store.nextCursor,page);await store.loadRuns({cursor:page},true);await store.loadRuns({},false,true);}
  assert.deepEqual(clone(store.runs.map(row=>row.id)),['new-6','new-5','new-4','new-3','new-2','new-1','old-2','old-1']);assert.equal(store.nextCursor,null);
});
test('failed first-page refresh preserves the complete history and pagination position',async()=>{
  let fail=false;const store=workspace({listRuns:async()=>{if(fail)throw new Error('offline');return {items:[{id:'old',status:'submitting'}],counts:{total:1},next_cursor:'tail'};}});
  await store.loadRuns();fail=true;await assert.rejects(store.loadRuns({},false,true),/offline/);
  assert.deepEqual(clone(store.runs),[{id:'old',status:'submitting'}]);assert.equal(store.nextCursor,'tail');
});
test('an account-mismatched single-run response cannot replace an existing detail',async()=>{
  const store=workspace({listRuns:async()=>({items:[{id:'new'}],counts:{total:1},next_cursor:null}),getRun:async()=>({run:{id:'old',account_id:'foreign',status:'success'}})});
  await store.loadRuns();store.runs.push({id:'old',account_id:'own',status:'uncertain'});await store.loadRuns({},false,true,'old');
  assert.equal(store.runs.find(row=>row.id==='old').account_id,'own');assert.equal(store.runs.find(row=>row.id==='old').status,'uncertain');assert.match(store.runRefreshError,/刷新失败/);
});
for(const field of ['date','account_id'])test('a failed '+field+' filter cannot mix the previous displayed rows into the recovered filter',async()=>{
  let fail=true,details=0;const store=workspace({
    listRuns:async filters=>{if(filters[field]==='B'&&fail)throw new Error('filter unavailable');return {items:[{id:filters[field],status:'queued'}],counts:{total:1},next_cursor:'tail-'+filters[field]};},
    getRun:async id=>{details++;return {run:{id,status:'queued'}};}
  });
  await store.loadRuns({[field]:'A'});store.runs.push({id:'older-A',status:'uncertain'});
  await assert.rejects(store.loadRuns({[field]:'B'}),/filter unavailable/);
  assert.deepEqual(clone(store.runs.map(row=>row.id)),['A','older-A']);
  fail=false;await store.loadRuns({[field]:'B'},false,true,'older-A');
  assert.deepEqual(clone(store.runs.map(row=>row.id)),['B']);assert.equal(store.nextCursor,'tail-B');assert.equal(details,0);
});
test('loading more cannot apply the previous cursor to a filter whose first page failed',async()=>{
  const calls=[];const store=workspace({listRuns:async filters=>{calls.push(clone(filters));if(filters.date==='B'&&!filters.cursor)throw new Error('filter unavailable');return {items:[{id:filters.date}],counts:{total:1},next_cursor:'tail-A'};}});
  await store.loadRuns({date:'A'});await assert.rejects(store.loadRuns({date:'B'}),/filter unavailable/);
  await store.loadRuns({date:'B',cursor:store.nextCursor},true);
  assert.equal(calls.length,2);assert.deepEqual(clone(store.runs.map(row=>row.id)),['A']);
});
for(const change of ['session','filter'])test('a delayed single-run refresh cannot overwrite a newer '+change,async()=>{
  let release;const store=workspace({listRuns:async filters=>({items:[{id:filters.date||'newest'}],counts:{total:1},next_cursor:null}),getRun:()=>new Promise(resolve=>release=resolve)});
  await store.loadRuns();store.runs.push({id:'old',status:'submitting'});const pending=store.loadRuns({},false,true,'old');await Promise.resolve();await Promise.resolve();
  if(change==='session'){store.clearSession();store.runs=[{id:'private-new'}];}else await store.loadRuns({date:'new-filter'});
  release?.({run:{id:'old',status:'success'}});await pending;
  assert.deepEqual(clone(store.runs.map(row=>row.id)),[change==='session'?'private-new':'new-filter']);
});
test('failed logout clears private local state and blocks restoring the unrevoked session',async()=>{
  const store=workspace({logout:async()=>{throw new Error('network down');}});Object.assign(store,{user:{role:'student'},accounts:[{id:'private'}],drafts:{private:{password:''}},runs:[{id:'private'}]});await store.logout();
  assert.equal(store.user,null);assert.equal(store.accounts.length,0);assert.equal(Object.keys(store.drafts).length,0);assert.equal(store.suppressAutoLogin,true);assert.match(store.logoutNotice,/未确认|未能确认/);
});
test('gateway logout with an unconfirmed upstream revocation displays a precise local-only notice',async()=>{
  const store=workspace({logout:async()=>({ok:true,upstream_revoked:false})});await store.logout();assert.equal(store.suppressAutoLogin,true);assert.match(store.logoutNotice,/未确认|未能确认/);
});
test('explicit login clears local logout suppression after successful authentication',async()=>{
  const store=workspace({logout:async()=>{throw new Error('offline');},login:async()=>({user:{role:'student',account_id:'new'}})});await store.logout();await store.login({});assert.equal(store.user.account_id,'new');assert.equal(store.suppressAutoLogin,false);assert.equal(store.logoutNotice,'');
});
test('login bootstrap cannot automatically restore a session after local-only logout',async()=>{
  const hooks={},routes=[],store={suppressAutoLogin:true,user:null},context={ref,useWorkspace:()=>store,bootstrap:async()=>({user:{role:'student'},admin_login:false}),onShow:f=>hooks.show=f,onHide:()=>{},takeReservationLink:()=>null,uni:{reLaunch:r=>routes.push(r)}};
  vm.runInNewContext(script('pages/login/login.vue'),context);await hooks.show();assert.equal(store.user,null);assert.equal(routes.length,0);
});
test('offline history stores cache metadata and deduplicates overlapping pages',async()=>{
  let call=0;const store=workspace({listRuns:async()=>({items:call++?[{id:'one'},{id:'two'}]:[{id:'one'}],counts:{total:2},next_cursor:call===1?'cache-next':null,cache_only:true,cached_at:123,cache_limit:100,cache_truncated:true,cached_total:100})});
  await store.loadRuns({date:'2026-11-07'});await store.loadRuns({date:'2026-11-07',cursor:'cache-next'},true);assert.deepEqual(clone(store.runs.map(row=>row.id)),['one','two']);assert.equal(store.runCache?.cache_only,true);assert.equal(store.runCache.cached_at,123);
});
function profilePage(){
  const store=baseStore(),prompts=[],routes=[],notices=[];let confirm=true,calls=0,refreshes=0;
  store.refresh=async()=>{refreshes++;};store.logout=async notice=>{notices.push(notice);store.user=null;store.sessionEpoch++;};
  const context={ref,computed,watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},onShow:()=>{},onHide:()=>{},useWorkspace:()=>store,api:{verifyAccount:async()=>{calls++;return {account:{...store.activeAccount,credential_revision:2}};}},uni:{showModal:o=>{prompts.push(o);o.success({confirm});},reLaunch:r=>routes.push(r)},clearInterval:()=>{},setInterval:()=>1};
  vm.runInNewContext(script('pages/profile/profile.vue')+'\nglobalThis.subject={saveAccount,password,message};',context);
  return {...context.subject,store,prompts,routes,notices,calls:()=>calls,refreshes:()=>refreshes,setConfirm:v=>confirm=v};
}
test('changed managed password explains maintenance and reauthenticates before a stale session refresh',async()=>{
  const p=profilePage();p.password.value='new-password';await p.saveAccount(true);assert.equal(p.prompts.length,1);assert.match(p.prompts[0].content,/两台|双机/);assert.equal(p.calls(),1);assert.equal(p.refreshes(),0);assert.match(p.notices[0],/主后台.*管理员|管理员.*同步/);assert.equal(p.routes[0].url,'/pages/login/login');assert.equal(p.password.value,'');
});
test('declining managed password maintenance keeps the input and sends no password request',async()=>{
  const p=profilePage();p.password.value='still-editing';p.setConfirm(false);await p.saveAccount(true);assert.equal(p.calls(),0);assert.equal(p.password.value,'still-editing');assert.equal(p.notices.length,0);
});
test('logout restoration suppression survives a page reload without storing private account data',()=>{
  assert.equal(typeof workspaceHelpers.writeLogoutMarker,'function');assert.equal(typeof workspaceHelpers.readLogoutMarker,'function');const values=new Map(),cache={setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k)||null,removeItem:k=>values.delete(k)};
  workspaceHelpers.writeLogoutMarker('撤销未确认',cache);assert.equal(workspaceHelpers.readLogoutMarker(cache).notice,'撤销未确认');assert.equal(workspaceHelpers.readLogoutMarker(cache).blocked,true);workspaceHelpers.writeLogoutMarker(null,cache);assert.equal(workspaceHelpers.readLogoutMarker(cache).blocked,false);
});
