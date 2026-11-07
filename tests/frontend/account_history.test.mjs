import test from 'node:test';
import assert from 'node:assert/strict';

async function moduleUnderTest(){
  try{return await import('../../AL-FrontEnd/services/account_history.mjs');}
  catch(error){if(error.code==='ERR_MODULE_NOT_FOUND')assert.fail('管理员账号历史读取逻辑尚未提供');throw error;}
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const page=(items,cursor=null)=>({items,counts:{total:items.length,success:items.filter(x=>x.status==='success').length},next_cursor:cursor});

test('an administrator sees only the selected account and pagination preserves earlier results',async()=>{
  const {createAccountHistoryLoader}=await moduleUnderTest();let current;const calls=[];
  const loader=createAccountHistoryLoader({fetch:async filters=>{
    calls.push(filters);return filters.cursor?page([{id:'two',account_id:'alice',status:'failed'},{id:'one',account_id:'alice',status:'success'}]):page([{id:'one',account_id:'alice',status:'success'}],'next');
  },changed:value=>current=value});
  await loader.select('alice',1);
  assert.deepEqual(current.items.map(x=>x.id),['one']);assert.equal(current.nextCursor,'next');
  await loader.more();assert.deepEqual(current.items.map(x=>x.id),['one','two']);
  assert.deepEqual(calls,[{account_id:'alice',limit:10},{account_id:'alice',limit:10,cursor:'next'}]);
  assert.equal(current.nextCursor,null);
});

test('a delayed old account response cannot appear after switching accounts or session',async()=>{
  const {createAccountHistoryLoader}=await moduleUnderTest();const first=deferred();let current;
  const loader=createAccountHistoryLoader({fetch:filters=>filters.account_id==='alice'?first.promise:Promise.resolve(page([{id:'bob',account_id:'bob',status:'failed'}])),changed:value=>current=value});
  const old=loader.select('alice',1);await loader.select('bob',1);
  first.resolve(page([{id:'alice',account_id:'alice',status:'success'}]));await old;
  assert.deepEqual(current.items.map(x=>x.id),['bob']);
  const oldSession=loader.select('alice',1);loader.stop();first.resolve(page([{id:'alice',account_id:'alice',status:'success'}]));await oldSession;
  assert.deepEqual(current.items,[]);
});

test('a failed refresh retains prior data but marks it stale, while initial failure is not an empty history',async()=>{
  const {createAccountHistoryLoader}=await moduleUnderTest();let current,fail=false;
  const loader=createAccountHistoryLoader({fetch:async()=>{if(fail)throw new Error('读取失败');return page([{id:'one',account_id:'alice',status:'success'}]);},changed:value=>current=value});
  await loader.select('alice',1);fail=true;await loader.refresh();
  assert.deepEqual(current.items.map(x=>x.id),['one']);assert.equal(current.stale,true);assert.match(current.error,/读取失败/);
  await loader.select('bob',1);assert.equal(current.loaded,false);assert.equal(current.stale,false);assert.match(current.error,/读取失败/);
});

test('a response containing another account is rejected instead of displaying private history',async()=>{
  const {createAccountHistoryLoader}=await moduleUnderTest();let current;
  const loader=createAccountHistoryLoader({fetch:async()=>page([{id:'other',account_id:'bob',status:'success'}]),changed:value=>current=value});
  await loader.select('alice',1);
  assert.deepEqual(current.items,[]);assert.equal(current.loaded,false);assert.match(current.error,/异常/);
});

test('first local request time is shown in Beijing time and missing timing is explicit',async()=>{
  const {firstRequestTime}=await moduleUnderTest();
  assert.equal(firstRequestTime({attempts:[{timings:{request_started_at:1793228400.123}}]}),'07:00:00.123');
  assert.equal(firstRequestTime({attempts:[]}),'—');
  assert.equal(firstRequestTime({attempts:[{timings:{request_started_at:'invalid'}}]}),'—');
});
