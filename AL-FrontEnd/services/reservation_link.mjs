const key='njfu-reservation-link-v1';
export function reservationLinkTarget(accountId,recordId,intent=''){
  if(typeof accountId!=='string'||typeof recordId!=='string'||!accountId||!recordId||accountId.length>128||recordId.length>200)return null;
  return {accountId,recordId,intent:intent==='cancel'?'cancel':''};
}
export function rememberReservationLink(accountId,recordId,cache=globalThis.sessionStorage,intent=''){
  const target=reservationLinkTarget(accountId,recordId,intent);if(!target)return;
  try{cache?.setItem(key,JSON.stringify({...target,created:Date.now()}));}catch{}
}
export function clearReservationLink(cache=globalThis.sessionStorage){try{cache?.removeItem(key);}catch{}}
export function takeReservationLink(user,cache=globalThis.sessionStorage){
  try{
    const saved=JSON.parse(cache?.getItem(key)||'null');
    if(!saved||!reservationLinkTarget(saved.accountId,saved.recordId,saved.intent)||!Number.isFinite(saved.created)||Date.now()-saved.created<0||Date.now()-saved.created>1800000){clearReservationLink(cache);return null;}
    if(user?.role!=='root'&&user?.account_id!==saved.accountId)return null;
    clearReservationLink(cache);
    return '/pages/reservation_info/reservation_info?account_id='+encodeURIComponent(saved.accountId)+'&record_id='+encodeURIComponent(saved.recordId)+(saved.intent==='cancel'?'&intent=cancel':'');
  }catch{return null;}
}
