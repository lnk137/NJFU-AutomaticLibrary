import test from 'node:test';import assert from 'node:assert/strict';
import {createCancellationController,newCancellationState} from '../../AL-FrontEnd/services/cancellation.mjs';
import {rememberReservationLink,takeReservationLink} from '../../AL-FrontEnd/services/reservation_link.mjs';
const cache=()=>{const m=new Map();return {setItem:(k,v)=>m.set(k,v),getItem:k=>m.get(k),removeItem:k=>m.delete(k)};};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function setup(overrides={},storage=cache()){
 let context={accountId:'a',recordId:'r',revision:1,session:1};const state=newCancellationState(),calls=[];
 const api={previewCancellation:async()=>({cancel_id:'id',expires_at:200,preview_token:'private-token'}),submitCancellation:async()=>{calls.push('cancel');return {state:'success'};},getCancellation:async()=>({state:'success'}),...overrides};
 const subject=createCancellationController({api,state,cache:storage,context:()=>context,clock:()=>100});subject.reset();return {subject,state,calls,storage,switch:()=>{context={...context,accountId:'b',session:2};subject.reset();}};
}
test('preparing and closing never cancel; repeated confirmation sends once',async()=>{const p=setup();p.subject.open();await p.subject.prepare();assert.deepEqual(p.calls,[]);p.subject.close();assert.deepEqual(p.calls,[]);p.subject.open();await p.subject.prepare();await p.subject.confirm();await p.subject.confirm();assert.deepEqual(p.calls,['cancel']);assert.equal(p.state.result.state,'success');});
test('uncertain transport survives reopen using cached ID, never caches preview token',async()=>{const p=setup({submitCancellation:async()=>{throw new Error('timeout');}});await p.subject.prepare();await p.subject.confirm();assert.equal(p.state.pending.id,'id');assert.equal(JSON.stringify(p.storage.getItem('njfu-cancel-v1:a:r')).includes('private-token'),false);const q=setup({},p.storage);await q.subject.open();assert.equal(q.state.result.state,'success');assert.deepEqual(q.calls,[]);});
test('pending missing batch before expiry remains uncertain',async()=>{const p=setup({submitCancellation:async()=>{throw new Error('timeout');},getCancellation:async()=>{throw Object.assign(new Error('missing'),{status:404});}});await p.subject.prepare();await p.subject.confirm();await p.subject.read();assert.equal(p.state.pending.id,'id');assert.match(p.state.error,/待核实/);assert.equal(await p.subject.confirm(),false);});
test('late prepare and result reads cannot write into switched account',async()=>{for(const method of ['previewCancellation','getCancellation']){const d=deferred(),p=setup({[method]:()=>d.promise});if(method==='getCancellation')p.state.pending={id:'id',expires_at:200};const work=method==='getCancellation'?p.subject.read():p.subject.prepare();p.switch();d.resolve({state:'success',cancel_id:'old',expires_at:200});await work;assert.equal(p.state.preview,null);assert.equal(p.state.result,null);assert.equal(p.state.pending,null);assert.equal(p.state.busy,false);}});
test('late submit cannot overwrite a switched account',async()=>{const d=deferred(),p=setup({submitCancellation:()=>d.promise});await p.subject.prepare();const work=p.subject.confirm();p.switch();d.resolve({state:'success'});await work;assert.equal(p.state.result,null);});
test('read-only reopen uses GET while explicit reconciliation never repeats cancellation',async()=>{const calls=[],p=setup({getCancellation:async()=>{calls.push('get');return {state:'uncertain',can_reconcile:true};},reconcileCancellation:async()=>{calls.push('reconcile');return {state:'success',can_reconcile:false};}});p.state.pending={id:'id',expires_at:200};await p.subject.open();await p.subject.read(true);assert.deepEqual(calls,['get','reconcile']);assert.equal(p.state.result.state,'success');});
test('uncertain results without readback capability cannot invoke a no-op school reconciliation',async()=>{
  for(const capability of [false,undefined]){
    const calls=[],p=setup({reconcileCancellation:async()=>{calls.push('reconcile');return {state:'uncertain',can_reconcile:capability};}});
    p.state.pending={id:'id',expires_at:200};p.state.result={state:'uncertain',can_reconcile:capability};
    assert.equal(await p.subject.read(true),false);assert.deepEqual(calls,[]);
    assert.equal(p.state.result.state,'uncertain');assert.equal(p.state.pending.id,'id');
    assert.equal(await p.subject.confirm(),false);
  }
});
test('mail link resumes after login only for the correct account',()=>{const c=cache();rememberReservationLink('a','receipt?x',c);assert.match(takeReservationLink({role:'student',account_id:'a'},c),/record_id=receipt%3Fx/);assert.equal(takeReservationLink({role:'student',account_id:'a'},c),null);rememberReservationLink('a','r',c);assert.equal(takeReservationLink({role:'student',account_id:'b'},c),null);});
