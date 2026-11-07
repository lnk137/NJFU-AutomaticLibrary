export function receiptState(receipt){
  if(!receipt||((receipt.state==null||receipt.state==='')&&(receipt.version==null||receipt.version===0)))return {kind:'none',label:'暂无收件',effective:false};
  if(receipt.state==='applied'&&Number.isInteger(receipt.applied_revision)&&receipt.applied_revision>0)return {kind:'applied',label:'已生效',effective:true};
  if(receipt.state==='failed')return {kind:'failed',label:'同步失败',effective:false};
  return {kind:'pending',label:'待同步，尚未生效',effective:false};
}
export function inboxTime(value){
  if(!Number.isFinite(value))return '尚无记录';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(value*1000));
}
export function planActivation(effectivePlan,receipt){
  const state=receiptState(receipt);
  if(state.kind==='pending')return {label:'新配置待同步，尚未生效',tone:''};
  if(state.kind==='failed')return {label:'新配置未生效 · 原计划'+(effectivePlan?.enabled?'已开启':'未开启'),tone:''};
  return {label:effectivePlan?.enabled?'已开启':'未开启',tone:effectivePlan?.enabled?'green':''};
}
