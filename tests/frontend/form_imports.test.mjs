import test from 'node:test';
import assert from 'node:assert/strict';
let feature;
try{feature=await import('../../AL-FrontEnd/services/form_imports.mjs');}catch{}
const requireFeature=()=>{assert.ok(feature,'Form import UI helpers missing');return feature;};
test('file payload is bounded and encodes Unicode bytes without exposing row credentials',async()=>{
 const {encodeFormFile}=requireFeature(),bytes=new TextEncoder().encode('学号,学校密码\n99000001,synthetic');
 const value=await encodeFormFile({name:'requests.csv',size:bytes.length,arrayBuffer:async()=>bytes.buffer});
 assert.equal(value.filename,'requests.csv');assert.equal(atob(value.content),String.fromCharCode(...bytes));
 await assert.rejects(()=>encodeFormFile({name:'too.xlsx',size:32769}),/32/);
 await assert.rejects(()=>encodeFormFile({name:'a.xls',size:10}),/XLSX|CSV/);
});
test('ready review never executes until the single explicit confirmation action',async()=>{
 const {createImportController}=requireFeature();let confirms=0,created=0;
 const ready={id:'one',state:'ready',ready_count:1,rows:[{status:'ready'}]};
 const c=createImportController({api:{createFormImport:async()=>{created++;return ready;},confirmFormImport:async()=>{confirms++;return {...ready,state:'confirming'};}},identity:()=> 'root:1',changed:()=>{},readFile:async()=>({filename:'a.csv',content:'AA=='}),schedule:()=>0});
 await c.upload({});assert.equal(created,1);assert.equal(confirms,0);assert.equal(c.state.batch.state,'ready');
 await c.confirm();await c.confirm();assert.equal(confirms,1);c.stop();
});
test('session switching during file reading prevents transmission and clears private results',async()=>{
 const {createImportController}=requireFeature();let resolve,owner='root:1',sent=0;
 const pending=new Promise(r=>resolve=r);
 const c=createImportController({api:{createFormImport:async()=>{sent++;}},identity:()=>owner,changed:()=>{},readFile:()=>pending,schedule:()=>0});
 const work=c.upload({});owner='student:2';c.stop();resolve({filename:'x.csv',content:'AA=='});await work;
 assert.equal(sent,0);assert.equal(c.state.batch,null);assert.equal(c.state.busy,false);
});
test('a lost confirmation response retains the batch for safe status refresh',async()=>{
 const {createImportController}=requireFeature();const batch={id:'one',state:'ready',ready_count:1,rows:[]};
 const c=createImportController({api:{listFormImports:async()=>({items:[batch]}),confirmFormImport:async()=>{throw new Error('请求超时');},getFormImport:async()=>({...batch,state:'done'})},identity:()=> 'root:1',changed:()=>{},schedule:()=>0});
 await c.loadRecent();await c.confirm();assert.equal(c.state.batch.id,'one');assert.match(c.state.error,/超时/);
 await c.refresh();assert.equal(c.state.batch.state,'done');c.stop();
});
test('summary shows normalized weekday intervals and the explicit today date',()=>{
 const {formPlanSummary}=requireFeature();
 assert.match(formPlanSummary({action:'scheduled',plan:{mode:'week',week_times:{'1':['09:00','12:00'],'5':['18:00','20:00']},trigger:'07:00:00'}}),/周五.*18:00.*20:00/);
 assert.match(formPlanSummary({action:'today',today:{date:'2026-10-23',begin:'18:00',end:'20:00'}}),/2026-10-23.*18:00.*20:00/);
});
test('lost upload response is recovered by its saved ID even with an older selected batch',async()=>{
 const {createImportController}=requireFeature();let sentId,creates=0;
 const old={id:'old',state:'done',rows:[],ready_count:0},fresh={id:'new',state:'ready',rows:[],ready_count:1};
 const storage=new Map(),adapter={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
 const api={listFormImports:async()=>({items:[old]}),createFormImport:async payload=>{creates++;sentId=payload.request_id.replaceAll('-','');throw new Error('请求超时');},getFormImport:async id=>id===sentId?{...fresh,id}:old};
 const config={api,identity:()=> 'root:1',changed:()=>{},readFile:async()=>({filename:'a.csv',content:'AA=='}),schedule:()=>0,storage:adapter};
 const c=createImportController(config);await c.loadRecent();await c.upload({});assert.equal(c.state.batch.id,'old');
 c.pause();const restored=createImportController(config);await restored.refresh();
 assert.equal(restored.state.batch.id,sentId);assert.equal(creates,1);assert.equal(storage.size,0);restored.stop();c.stop();
});
