/* Standalone capability page: no account login, no second confirmation. */
export async function runMailCancellation({location=globalThis.location,history=globalThis.history,
  fetcher=globalThis.fetch,storage,cryptoApi,
  view=()=>{},sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),pollLimit=8}={}){
  const show=(state,message)=>view({state,title:state==='success'?'预约已取消':state==='failed'?'未执行取消':'正在核对取消结果',message});
  const ticket=new URLSearchParams((location?.hash||'').slice(1)).get('ticket');
  try{history?.replaceState(null,'',(location?.pathname||'/static/mail-cancel.html')+(location?.search||''));}catch{}
  if(!ticket||ticket.length>8192||/[\s\x00-\x1f]/.test(ticket)){show('failed','取消链接无效或不完整，请检查原提醒邮件。');return;}
  let key,attempted;
  try{
    if(storage===undefined)storage=globalThis.sessionStorage;
    if(cryptoApi===undefined)cryptoApi=globalThis.crypto;
    const hash=await cryptoApi.subtle.digest('SHA-256',new TextEncoder().encode(ticket));
    key='njfu-mail-cancel:'+Array.from(new Uint8Array(hash),v=>v.toString(16).padStart(2,'0')).join('');
    attempted=!!storage.getItem(key);
  }catch{show('failed','当前浏览器无法安全保存取消状态，未提交取消。请在允许本地存储的浏览器打开原邮件按钮。');return;}
  const remember=()=>{try{storage.setItem(key,'pending');return storage.getItem(key)==='pending';}catch{return false;}};
  const forget=()=>{try{if(key)storage?.removeItem(key);}catch{}};
  let csrf;
  show('pending','正在核对邮件中的这条预约，请稍候。');
  try{
    const response=await fetcher('/api/local/bootstrap',{credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',redirect:'error'});
    const data=await response.json();
    if(!response.ok||typeof data.csrf!=='string'||!data.csrf||data.csrf.length>256)throw new Error();
    csrf=data.csrf;
  }catch{show('failed','暂时无法连接预约后台，没有发送取消请求。请稍后重新打开邮件按钮。');return;}
  const request=async action=>{
    try{
      const response=await fetcher('/api/local/mail-cancellation/'+action,{method:'POST',credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',redirect:'error',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({ticket})});
      const data=await response.json();
      if(response.ok&&data&&typeof data.state==='string')return data;
      if(data?.code==='mail_navigation_required'){forget();return {state:'failed',message:'这次打开仅作预览，没有取消预约。请直接点击邮件中的“取消预约”按钮。'};}
      if(response.status>=400&&response.status<500&&response.status!==409)return {state:'failed',message:typeof data?.error==='string'?data.error:'取消链接已失效，请查看原预约记录。'};
    }catch{}
    return {state:'pending',message:'网络结果尚待核实，正在读取这条预约的取消状态；不会重复提交。'};
  };
  const action=attempted?'status':'submit';
  if(action==='submit'&&!remember()){show('failed','当前浏览器无法保存本次处理状态，未提交取消。请在允许本地存储的浏览器打开原邮件按钮。');return;}
  let result=await request(action);
  for(let attempt=0;;attempt++){
    if(result.state==='not_started'){forget();show('failed',result.message||'后台尚未开始取消，请重新点击原邮件中的按钮。');return;}
    if(result.state==='success'||result.state==='failed'){show(result.state,result.message||'请以学校预约记录为准。');return;}
    show('pending',result.message||'取消结果待核实，正在查询，请稍候。');
    if(attempt>=pollLimit){show('pending','取消结果仍待核实。请稍后重新打开原邮件按钮查看结果；系统不会重复取消。');return;}
    await sleep(2000);result=await request('status');
  }
}

if(typeof document!=='undefined'&&typeof window!=='undefined'){
  const title=document.getElementById('result-title'),message=document.getElementById('result-message'),card=document.getElementById('result');
  runMailCancellation({view:state=>{if(title)title.textContent=state.title;if(message)message.textContent=state.message;if(card)card.dataset.state=state.state;}});
}
