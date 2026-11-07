import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as helpers from '../../AL-FrontEnd/services/workspace.mjs';

function workspace(api={}){
  let options;
  const source=readFileSync(new URL('../../AL-FrontEnd/stores/workspace.js',import.meta.url),'utf8')
    .replace(/^import .*\n/gm,'').replace('export const useWorkspace=','const useWorkspace=');
  vm.runInNewContext(source,{api,...helpers,defineStore:(_,value)=>{options=value;}});
  const store={...options.state()};store.$reset=()=>Object.assign(store,options.state());
  for(const [name,method] of Object.entries(options.actions))store[name]=method.bind(store);
  return store;
}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

test('logout clears all private account state without touching server plans',async()=>{
  let calls=0;const store=workspace({logout:async()=>{calls++;}});
  Object.assign(store,{user:{role:'student'},accounts:[{id:'old'}],activeAccountId:'old',drafts:{old:{alias:'Alice'}},snapshots:{old:{records:[1]}},runs:[1],counts:{total:1}});
  await store.logout();
  assert.equal(calls,1);assert.equal(store.user,null);assert.equal(store.activeAccountId,'');
  for(const key of ['accounts','runs'])assert.equal(store[key].length,0);
  for(const key of ['drafts','snapshots','counts'])assert.equal(Object.keys(store[key]).length,0);
});

test('late session identity cannot restore a logged-out principal',async()=>{
  const pending=deferred();const store=workspace({getSession:()=>pending.promise});
  const request=store.refresh();store.clearSession();pending.resolve({user:{role:'student',account_id:'old'}});
  await request;assert.equal(store.user,null);assert.equal(store.accounts.length,0);
});

test('late account and history responses cannot leak across a session switch',async()=>{
  const account=deferred(),runs=deferred();
  const store=workspace({getSession:async()=>({user:{role:'student',account_id:'old'}}),listAccounts:()=>account.promise,getStatus:async()=>({}),getPolicy:async()=>({}),listRuns:()=>runs.promise});
  const refresh=store.refresh();await Promise.resolve();
  const history=store.loadRuns();store.clearSession();store.user={role:'student',account_id:'new'};
  account.resolve({accounts:[{id:'old'}]});runs.resolve({items:[{account_id:'old'}],counts:{total:1}});
  await Promise.all([refresh,history]);
  assert.equal(store.user.account_id,'new');assert.equal(store.accounts.length,0);assert.equal(store.runs.length,0);
});

function apiClient(responses){
  const events=[],paths=[];
  const source=readFileSync(new URL('../../AL-FrontEnd/services/local_api.js',import.meta.url),'utf8').replace(/export /g,'');
  const context={uni:{$emit:event=>events.push(event),request:request=>{paths.push(request.url);request.success(responses.shift());}}};
  vm.runInNewContext(source+'\nglobalThis.subject={getSession,login,verifyAccount};',context);
  return {...context.subject,events,paths};
}
const boot={statusCode:200,data:{csrf:'fixture'}};

test('expired local sessions trigger privacy reset but school password failures do not',async()=>{
  const expired=apiClient([boot,{statusCode:401,data:{code:'authentication_required'}}]);
  await assert.rejects(expired.getSession());assert.deepEqual(expired.events,['njfu-auth-required']);
  const school=apiClient([boot,{statusCode:401,data:{code:'login_failed'}}]);
  await assert.rejects(school.login({}));assert.equal(school.events.length,0);
});

test('a rejected stale CSRF token refreshes bootstrap once before retry',async()=>{
  const client=apiClient([boot,{statusCode:403,data:{code:'invalid_csrf'}},boot,{statusCode:200,data:{user:{role:'student'}}}]);
  assert.equal((await client.login({})).user.role,'student');
  assert.deepEqual(client.paths,['/api/local/bootstrap','/api/local/auth/login','/api/local/bootstrap','/api/local/auth/login']);
});

test('a delayed old 401 cannot log out a newly logged-in principal',async()=>{
  const waiting=[],events=[];
  const source=readFileSync(new URL('../../AL-FrontEnd/services/local_api.js',import.meta.url),'utf8').replace(/export /g,'');
  const context={uni:{$emit:event=>events.push(event),request:request=>waiting.push(request)}};
  vm.runInNewContext(source+'\nglobalThis.subject={getSession,login};',context);
  const old=context.subject.getSession();waiting.shift().success(boot);
  await Promise.resolve();await Promise.resolve();
  const oldRead=waiting.shift();
  const fresh=context.subject.login({username:'new'});
  waiting.shift().success({statusCode:200,data:{user:{role:'student',account_id:'new'}}});await fresh;
  oldRead.success({statusCode:401,data:{code:'authentication_required'}});
  await assert.rejects(old);assert.deepEqual(events,[]);
});
