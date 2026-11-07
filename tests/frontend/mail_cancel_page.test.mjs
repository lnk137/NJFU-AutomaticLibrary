import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
const file=new URL('../../AL-FrontEnd/static/mail-cancel.js',import.meta.url);
const source=existsSync(file)?readFileSync(file,'utf8'):'export const runMailCancellation=undefined;';
const {runMailCancellation}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const reply=(value,ok=true)=>({ok,status:ok?200:400,json:async()=>value});
function fixture(responses,{hash='#ticket=fixture-only-capability'}={}){
  const values=new Map(),calls=[],views=[],events=[];
  return {calls,views,values,events,options:{
    location:{hash,pathname:'/static/mail-cancel.html',search:''},
    history:{replaceState(_a,_b,path){events.push('clear-fragment');assert.equal(path,'/static/mail-cancel.html');}},
    storage:{getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)},
    cryptoApi:webcrypto,view:v=>views.push(v),sleep:async()=>{},pollLimit:3,
    fetcher:async(url,options={})=>{events.push('fetch');calls.push({url,options});const next=responses.shift();if(next instanceof Error)throw next;assert.ok(next,'Unexpected extra request');return next;}
  }};
}
test('mail click clears secret fragment then submits once without login or confirmation',async()=>{
  assert.equal(typeof runMailCancellation,'function');
  const f=fixture([reply({csrf:'fixture-csrf',user:null}),reply({state:'success',message:'学校已确认取消本段预约'})]);
  await runMailCancellation(f.options);
  assert.equal(f.events[0],'clear-fragment');assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].url,'/api/local/mail-cancellation/submit');
  assert.deepEqual(JSON.parse(f.calls[1].options.body),{ticket:'fixture-only-capability'});
  assert.equal(f.calls[1].options.credentials,'same-origin');assert.equal(f.calls[1].options.headers['X-CSRF-Token'],'fixture-csrf');
  assert.equal(f.views.at(-1).state,'success');assert.ok([...f.values.keys()].every(k=>!k.includes('capability')));
});
test('missing ticket performs no network request',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([],{hash:''});await runMailCancellation(f.options);
  assert.equal(f.calls.length,0);assert.equal(f.views.at(-1).state,'failed');
});
test('bootstrap failure never starts cancellation',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([new Error('offline')]);await runMailCancellation(f.options);
  assert.equal(f.calls.length,1);assert.equal(f.values.size,0);
});
test('ordinary preview is rejected without retaining a dispatch marker',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([reply({csrf:'fixture-csrf'}),reply({code:'mail_navigation_required',error:'仅预览'},false)]);
  await runMailCancellation(f.options);assert.equal(f.calls.length,2);assert.equal(f.values.size,0);assert.equal(f.views.at(-1).state,'failed');
});
test('ambiguous submit only polls status and never repeats submit',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([reply({csrf:'fixture-csrf'}),new Error('timeout'),reply({state:'pending',message:'核对中'}),reply({state:'success',message:'已取消'})]);
  await runMailCancellation(f.options);assert.equal(f.calls.filter(c=>c.url.endsWith('/submit')).length,1);
  assert.equal(f.calls.filter(c=>c.url.endsWith('/status')).length,2);assert.equal(f.views.at(-1).state,'success');
});
test('reopening a previously attempted ticket only checks its durable status',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([reply({csrf:'fixture-csrf'}),reply({state:'uncertain',message:'待核实'}),reply({state:'uncertain'}),reply({state:'uncertain'}),reply({state:'uncertain'})]);
  await runMailCancellation(f.options);const g=fixture([reply({csrf:'fixture-csrf'}),reply({state:'success',message:'已取消'})]);g.options.storage=f.options.storage;
  await runMailCancellation(g.options);assert.equal(g.calls[1].url,'/api/local/mail-cancellation/status');
});
test('not-started readback permits a future explicit mail click without automatic replay',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([reply({csrf:'fixture-csrf'}),new Error('offline'),reply({state:'not_started',message:'请求未开始'})]);
  await runMailCancellation(f.options);assert.equal(f.calls.length,3);assert.equal(f.values.size,0);
});
test('invalid capability fails without login redirect or another submit',async()=>{
  assert.equal(typeof runMailCancellation,'function');const f=fixture([reply({csrf:'fixture-csrf'}),reply({code:'invalid_ticket',error:'链接已失效'},false)]);
  await runMailCancellation(f.options);assert.equal(f.calls.length,2);assert.equal(f.views.at(-1).state,'failed');
});
test('storage getter denial still erases fragment before stopping',async()=>{
  const old=Object.getOwnPropertyDescriptor(globalThis,'sessionStorage');const f=fixture([]);delete f.options.storage;
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,get(){throw new Error('denied');}});
  try{await runMailCancellation(f.options);assert.equal(f.events[0],'clear-fragment');assert.equal(f.calls.length,0);assert.equal(f.views.at(-1).state,'failed');}
  finally{if(old)Object.defineProperty(globalThis,'sessionStorage',old);else delete globalThis.sessionStorage;}
});
test('storage read denial performs no cancellation request',async()=>{
  const f=fixture([]);f.options.storage={getItem(){throw new Error('denied');}};await runMailCancellation(f.options);
  assert.equal(f.events[0],'clear-fragment');assert.equal(f.calls.length,0);assert.equal(f.views.at(-1).state,'failed');
});
test('dispatch marker write failure cannot start a cancellation',async()=>{
  const f=fixture([reply({csrf:'fixture-csrf'})]);f.options.storage={getItem:()=>null,setItem(){throw new Error('denied');}};
  await runMailCancellation(f.options);assert.equal(f.calls.length,1);assert.equal(f.views.at(-1).state,'failed');
});
test('silently rejected dispatch marker cannot start a cancellation',async()=>{
  const f=fixture([reply({csrf:'fixture-csrf'})]);f.options.storage={getItem:()=>null,setItem(){}};
  await runMailCancellation(f.options);assert.equal(f.calls.length,1);assert.equal(f.views.at(-1).state,'failed');
});
test('digest failure stops before any request and does not retain a raw ticket',async()=>{
  const f=fixture([]);f.options.cryptoApi={subtle:{async digest(){throw new Error('unavailable');}}};
  await runMailCancellation(f.options);assert.equal(f.events[0],'clear-fragment');assert.equal(f.calls.length,0);assert.equal(f.values.size,0);
});
