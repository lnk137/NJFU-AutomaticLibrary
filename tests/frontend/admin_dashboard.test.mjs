import test from 'node:test';
import assert from 'node:assert/strict';
import {createDashboardLoader,formatMs,lineSegments,mergeNotes,filterUsers,planSeats} from '../../AL-FrontEnd/services/admin_dashboard.mjs';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

test('one inflight request coalesces changes and applies only the latest filter',async()=>{
  const pending=[],calls=[];
  const loader=createDashboardLoader({identity:()=> 'root:1',fetch:filters=>{calls.push(filters.date);const p=deferred();pending.push(p);return p.promise;}});
  const first=loader.load({date:'a'});await loader.load({date:'b'});await loader.load({date:'c'});
  assert.deepEqual(calls,['a']);pending[0].resolve({date:'a'});await Promise.resolve();await Promise.resolve();
  assert.deepEqual(calls,['a','c']);assert.equal(loader.state.data,null);
  pending[1].resolve({date:'c'});await first;assert.equal(loader.state.data.date,'c');assert.equal(loader.state.loading,false);
});
test('read errors retain the last known result with a visible stale warning',async()=>{
  let fail=false;const loader=createDashboardLoader({identity:()=> 'root:1',fetch:async()=>{if(fail)throw Error('fixture timeout');return {date:'a'};}});
  await loader.load({date:'a'});fail=true;await loader.load({date:'a'});
  assert.equal(loader.state.data.date,'a');assert.equal(loader.state.stale,true);assert.match(loader.state.error,/timeout/);
});
test('logout and an old failure cannot repopulate data or errors for a new session',async()=>{
  let identity='root:1';const p=deferred();const loader=createDashboardLoader({identity:()=>identity,fetch:()=>p.promise});
  const request=loader.load({});identity=null;loader.stop();p.reject(Error('old private error'));await request;
  assert.equal(loader.state.data,null);assert.equal(loader.state.error,'');assert.equal(loader.state.loading,false);
});
test('missing timing is never rendered as zero and line graphs break across gaps',()=>{
  assert.equal(formatMs(null),'无数据');assert.equal(formatMs(NaN),'无数据');assert.equal(formatMs(0),'0.0 ms');
  const paths=lineSegments([{request:{p50:10}},{request:{p50:null}},{request:{p50:30}}],'p50',30);
  assert.equal(paths.length,2);assert.equal(paths[0].split(' ').length,1);
});
test('stat refresh preserves unsaved root notes while clean notes follow the server',()=>{
  const result=mergeNotes([{student_id:'a',note:'remote-a'},{student_id:'b',note:'remote-b'}],{a:'draft',b:'old-b'},{a:'old-a',b:'old-b'});
  assert.deepEqual(result,{a:'draft',b:'remote-b'});
});
test('root search and authorization filters compose without changing user data',()=>{
  const users=[{student_id:'01',note:'Alice',enabled:true,account:{plan:{enabled:true}}},{student_id:'02',note:'Bob',enabled:false}];
  assert.equal(filterUsers(users,' ALICE ','scheduled').length,1);
  assert.equal(filterUsers(users,'','disabled')[0].student_id,'02');assert.equal(users.length,2);
});
test('malformed legacy seat fields cannot break the root management list',()=>{
  for(const plan of ['bad',{},null,{seat_ids:null}])assert.deepEqual(planSeats(plan),[]);
  assert.deepEqual(planSeats({seat_ids:['1',null,'2']}),['1','2']);
});
