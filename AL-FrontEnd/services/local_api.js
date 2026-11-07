let csrf='',authGeneration=0;
const stale=()=>Object.assign(new Error('会话已切换，已忽略旧请求'),{code:'stale_session'});
const raw = (path,method='GET',data,generation=authGeneration) => new Promise((resolve,reject)=>uni.request({
  url:'/api/local'+path,method,data,timeout:60000,
  header:{'Content-Type':'application/json',...(method==='GET'?{}:{'X-CSRF-Token':csrf})},
  success:r=>{
    if(generation!==authGeneration){reject(stale());return;}
    if(r.statusCode>=200&&r.statusCode<300)resolve(r.data);
    else {if(['authentication_required','offline_login_required'].includes(r.data?.code)){authGeneration++;uni.$emit('njfu-auth-required');}reject(Object.assign(new Error(r.data?.error||'本机服务请求失败'),{status:r.statusCode,code:r.data?.code}));}
  },fail:()=>reject(generation!==authGeneration?stale():new Error('连接中断或请求超时，请检查本机后台；请勿反复提交'))
}));
export async function bootstrap(generation=authGeneration){const result=await raw('/bootstrap','GET',undefined,generation);if(generation!==authGeneration)throw stale();csrf=result.csrf;return result;}
async function call(path,method='GET',data){const generation=authGeneration;if(!csrf)await bootstrap(generation);if(generation!==authGeneration)throw stale();try{return await raw(path,method,data,generation);}catch(e){if(e.code!=='invalid_csrf'||generation!==authGeneration)throw e;await bootstrap(generation);return raw(path,method,data,generation);}}
export const listAccounts=()=>call('/accounts');
export const verifyAccount=data=>call('/accounts/verify','POST',data);
export const savePlan=(id,plan,revision)=>call(`/accounts/${encodeURIComponent(id)}/plan`,'PUT',{plan,expected_revision:revision,request_id:globalThis.crypto.randomUUID()});
export const checkAccount=id=>call(`/accounts/${encodeURIComponent(id)}/check`,'POST',{});
export const updateDateExceptions=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/date-exceptions`,'POST',data);
export const getSeatPreferences=id=>call(`/accounts/${encodeURIComponent(id)}/seat-preferences`);
export const saveSeatPreferences=(id,preferences,revision)=>call(`/accounts/${encodeURIComponent(id)}/seat-preferences`,'PUT',{preferences,expected_revision:revision});
export const previewToday=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/today/preview`,'POST',data);
export const submitToday=(id,previewToken)=>call(`/accounts/${encodeURIComponent(id)}/today/submit`,'POST',{preview_token:previewToken});
export const previewTodayBatch=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/today/batches/preview`,'POST',data);
export const submitTodayBatch=(id,previewToken)=>call(`/accounts/${encodeURIComponent(id)}/today/batches/submit`,'POST',{preview_token:previewToken});
export const getTodayBatch=(id,key)=>call(`/accounts/${encodeURIComponent(id)}/today/batches/${encodeURIComponent(key)}`);
export const latestTodayBatch=id=>call(`/accounts/${encodeURIComponent(id)}/today/batches`);
export const queryRecords=id=>call(`/accounts/${encodeURIComponent(id)}/query`,'POST',{});
export const getRecords=id=>call(`/accounts/${encodeURIComponent(id)}/records`);
export const getCalendar=(id,start,end)=>call(`/accounts/${encodeURIComponent(id)}/calendar?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
export const getNotificationSettings=id=>call(`/accounts/${encodeURIComponent(id)}/notifications`);
export const updateNotificationSettings=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/notifications`,'PUT',data);
export const requestNotificationVerification=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/notifications/verification`,'POST',data);
export const confirmNotificationVerification=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/notifications/verification/confirm`,'POST',data);
export const getMailStatus=()=>call('/admin/mail');
export const previewCancellation=(id,data)=>call(`/accounts/${encodeURIComponent(id)}/cancellations/preview`,'POST',data);
export const submitCancellation=(id,token)=>call(`/accounts/${encodeURIComponent(id)}/cancellations/submit`,'POST',{preview_token:token});
export const getCancellation=(id,key)=>call(`/accounts/${encodeURIComponent(id)}/cancellations/${encodeURIComponent(key)}`);
export const reconcileCancellation=(id,key)=>call(`/accounts/${encodeURIComponent(id)}/cancellations/${encodeURIComponent(key)}/reconcile`,'POST',{});
export const listRuns=filters=>call('/runs?'+Object.entries(filters||{}).filter(([,v])=>v!==''&&v!=null).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&'));
export const getStatus=()=>call('/status');
export const getPolicy=()=>call('/policy');
export const getSeats=()=>call('/seats');
export const reconcileRun=key=>call(`/runs/${encodeURIComponent(key)}/reconcile`,'POST',{});

export const getSession=()=>call('/auth/me');
export const login=data=>{authGeneration++;return call('/auth/login','POST',data);};
export const logout=()=>{authGeneration++;return call('/auth/logout','POST',{});};
export const listUsers=()=>call('/admin/users');
export const updateAccess=(student,data)=>call('/admin/users/'+encodeURIComponent(student),'PUT',data);
export const listAudit=()=>call('/admin/audit');
export const getDashboard=filters=>call('/admin/dashboard?'+Object.entries(filters).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&'));
export const getInbox=()=>call('/inbox');
export const getAdminInbox=()=>call('/admin/inbox');
export const controlExecutor=action=>call('/admin/executor','POST',{action});
export const listFormImports=()=>call('/admin/form-imports');
export const createFormImport=data=>call('/admin/form-imports','POST',data);
export const getFormImport=id=>call('/admin/form-imports/'+encodeURIComponent(id));
export const confirmFormImport=id=>call('/admin/form-imports/'+encodeURIComponent(id)+'/confirm','POST',{});
export const getMacPower=()=>call('/admin/power');
export const updateMacPower=data=>call('/admin/power','PUT',data);
export const installMacPower=()=>call('/admin/power/install','POST',{});
