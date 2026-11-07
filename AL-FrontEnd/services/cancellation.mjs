const prefix='njfu-cancel-v1:';
export function newCancellationState(){return {open:false,busy:false,preview:null,result:null,error:'',pending:null};}
export function createCancellationController({api,context,state=newCancellationState(),cache=globalThis.sessionStorage,clock=()=>Date.now()/1000}){
  let generation=0,previewContext=null;
  const key=c=>prefix+c.accountId+':'+c.recordId;
  const same=(c,t)=>t===generation&&JSON.stringify(c)===JSON.stringify(context());
  function remember(value){state.pending=value;try{if(value)cache?.setItem(key(context()),JSON.stringify(value));else cache?.removeItem(key(context()));}catch{}}
  async function perform(action){
    if(state.busy)return false;const c={...context()},t=generation;state.busy=true;state.error='';
    try{const result=await action(c);if(!same(c,t))return false;return result;}
    catch(e){if(same(c,t))state.error=e.message;return false;}
    finally{if(same(c,t))state.busy=false;}
  }
  async function read(reconcile=false){
    const pending=state.pending,t=generation;if(!pending)return;
    if(reconcile&&readAction()!=='reconcile')return false;
    const result=await perform(async c=>{
      try{return await (reconcile?api.reconcileCancellation:api.getCancellation)(c.accountId,pending.id);}
      catch(e){if(e.status===404)return {missing:true};throw e;}
    });
    if(t!==generation||!result)return false;
    if(result.missing){if(clock()>=pending.expires_at){remember(null);state.preview=null;state.result=null;state.error='上次确认已过期且未提交，可重新核对预约';}else state.error='取消请求是否到达仍待核实，请稍后读取结果';return false;}
    state.result=result;return result;
  }
  function readAction(){
    if(!state.pending)return null;
    if(!state.result||state.result.state==='submitting')return 'read';
    return state.result.state==='uncertain'&&state.result.can_reconcile===true?'reconcile':null;
  }
  async function prepare(){
    if(state.pending)return read();
    if(state.busy)return false;
    const c={...context()},t=generation;state.preview=null;previewContext=null;
    const result=await perform(c=>api.previewCancellation(c.accountId,{record_id:c.recordId,expected_revision:c.revision}));
    if(result&&same(c,t)){
      if(result.record?.id&&result.record.id!==c.recordId){state.error='学校返回的预约与链接不一致，请重新核对';return false;}
      state.preview=result;previewContext={context:c,generation:t};
    }
    return result;
  }
  async function refreshExpired(){
    state.preview=null;previewContext=null;
    const result=await prepare();
    if(result)state.error='取消确认已过期，已重新核对座位与时段，请再次确认。';
    return false;
  }
  return {
    state,
    reset(){generation++;previewContext=null;Object.assign(state,newCancellationState());try{const value=JSON.parse(cache?.getItem(key(context()))||'null');if(value&&typeof value.id==='string'&&Number.isFinite(value.expires_at))state.pending=value;}catch{}},
    open({preview=false}={}){state.open=true;if(state.pending)return read();if(preview)return prepare();},
    close(){if(state.busy)return false;state.open=false;state.preview=null;previewContext=null;state.error='';return true;},
    prepare,
    async confirm(){
      if(state.busy||!state.preview||state.pending)return false;
      if(!previewContext||!same(previewContext.context,previewContext.generation)){state.preview=null;previewContext=null;state.error='预约目标已变化，请重新核对';return false;}
      if(clock()>=state.preview.expires_at)return refreshExpired();
      const preview=state.preview,c={...context()},t=generation;remember({id:preview.cancel_id,expires_at:preview.expires_at});
      let expired=false;
      const result=await perform(async c=>{try{return await api.submitCancellation(c.accountId,preview.preview_token);}catch(e){expired=e.code==='preview_expired';throw e;}});
      if(expired&&same(c,t)){remember(null);return refreshExpired();}
      if(result&&same(c,t))state.result=result;
      return result;
    },
    read,readAction,
    dispose(){generation++;previewContext=null;state.busy=false;state.preview=null;state.open=false;}
  };
}
