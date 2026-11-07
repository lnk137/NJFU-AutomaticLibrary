export async function encodeFormFile(file){
 if(!file||! /\.(xlsx|csv)$/i.test(file.name))throw new Error('请选择 XLSX 或 CSV 表单');
 if(!file.size||file.size>32768)throw new Error('每份表单最多32 KiB');
 const bytes=new Uint8Array(await file.arrayBuffer());
 if(!bytes.length||bytes.length>32768)throw new Error('每份表单最多32 KiB');
 return {filename:file.name,content:btoa(String.fromCharCode(...bytes))};
}
export function formPlanSummary(row){
 if(row.action==='today')return `${row.today?.date||''} ${row.today?.begin||''}–${row.today?.end||''}`;
 const plan=row.plan;if(!plan)return '';
 const days=['','一','二','三','四','五','六','日'];
 const interval=plan.mode==='week'?Object.entries(plan.week_times||{}).map(([d,t])=>`周${days[d]} ${t[0]}–${t[1]}`).join('；'):`每天 ${plan.begin}–${plan.end}${plan.end>'20:00'?'（周五至20:00）':''}`;
 return interval+' · '+plan.trigger+' 放号';
}
export const importStateName=state=>({validating:'正在验证',ready:'待确认',confirming:'正在执行',done:'已处理',failed:'验证中断',interrupted:'中断，待核对'}[state]||state);
function browserStorage(){try{return globalThis.sessionStorage;}catch{return null;}}
export function createImportController({api,identity,changed=()=>{},readFile=encodeFormFile,isOpen=()=>true,schedule=setTimeout,cancel=clearTimeout,storage=browserStorage()}){
 const state={batch:null,recent:[],busy:false,error:''};let sequence=0,timer=null,pending=null,pendingOwner=null;
 function pendingId(){const owner=identity();if(owner&&pendingOwner!==owner){pendingOwner=owner;try{pending=storage?.getItem('njfu-form-upload:'+owner)||null;}catch{pending=null;}}return pending;}
 function remember(id){pendingOwner=identity();pending=id;try{storage?.setItem('njfu-form-upload:'+pendingOwner,id);}catch{}}
 function forget(){if(pendingOwner)try{storage?.removeItem('njfu-form-upload:'+pendingOwner);}catch{}pending=null;}
 const notify=()=>changed({...state}),token=()=>({owner:identity(),sequence});
 const current=t=>!!t.owner&&identity()===t.owner&&t.sequence===sequence;
 function pause(){sequence++;cancel(timer);timer=null;state.busy=false;notify();}
 function stop(){pause();forget();Object.assign(state,{batch:null,recent:[],error:'',busy:false});notify();}
 function dispose(){pause();Object.assign(state,{batch:null,recent:[],error:'',busy:false});notify();}
 function poll(){cancel(timer);timer=null;if(identity()&&isOpen()&&['validating','confirming'].includes(state.batch?.state))timer=schedule(()=>refresh(),1500);}
 async function run(work){
  if(!identity()||state.busy)return;const t=token();state.busy=true;state.error='';notify();
  try{const result=await work(t);if(current(t)&&result){state.batch=result;if(result.id===pendingId())forget();}}
  catch(e){if(current(t))state.error=e.message;}
  finally{if(current(t)){state.busy=false;notify();poll();}}
 }
 async function loadRecent(){if(pendingId())return refresh();return run(async t=>{const result=await api.listFormImports();if(!current(t))return;state.recent=result.items;return state.batch||result.items[0]||null;});}
 async function refresh(){const id=pendingId()||state.batch?.id;return id?run(()=>api.getFormImport(id)):loadRecent();}
 async function select(id){return run(()=>api.getFormImport(id));}
 async function upload(file){
  if(['validating','confirming'].includes(state.batch?.state))return;
  const request_id=crypto.randomUUID();return run(async t=>{const payload=await readFile(file);if(!current(t))return;remember(request_id.replaceAll('-',''));return api.createFormImport({...payload,request_id});});
 }
 async function confirm(){if(state.batch?.state!=='ready'||!state.batch.ready_count)return;return run(()=>api.confirmFormImport(state.batch.id));}
 return {state,loadRecent,refresh,select,upload,confirm,pause,stop,dispose};
}
