export const beginRequest = (accountId, generation, sequence=0) => ({accountId,generation,sequence});
export const canApplyResponse = (request, accountId, generation) => request.accountId===accountId && request.generation===generation;
export const chooseAccount = (state,id) => ({...state,activeAccountId:id,requestGeneration:state.requestGeneration+1});
export const canApplySnapshot = (request, latestSequence, accountRevision, responseRevision) => request.sequence===latestSequence && accountRevision===responseRevision;
export const canApplyRunPage = (request,currentKey,currentSequence) => request.key===currentKey && request.sequence===currentSequence;

const logoutKey='njfu-local-logout-v1';
export function readLogoutMarker(storage){
  if(!storage&&typeof window==='undefined')return {blocked:false,notice:''};
  try{const value=JSON.parse((storage||globalThis.sessionStorage)?.getItem(logoutKey)||'null');return {blocked:value?.blocked===true,notice:typeof value?.notice==='string'?value.notice:''};}
  catch{return {blocked:false,notice:''};}
}
export function writeLogoutMarker(notice,storage){
  if(!storage&&typeof window==='undefined')return;
  try{const cache=storage||globalThis.sessionStorage;if(notice===null)cache?.removeItem(logoutKey);else cache?.setItem(logoutKey,JSON.stringify({blocked:true,notice:String(notice)}));}catch{}
}
