export function newNotificationState(){
  return {accountId:'',sessionKey:'',settings:null,draft:null,address:'',code:'',challenge:null,
    loading:false,busy:false,open:false,error:'',message:''};
}

const draftOf=settings=>({revision:settings.revision,results_enabled:settings.results_enabled,
  reminders_enabled:settings.reminders_enabled,reminder_minutes:settings.reminder_minutes});

export function createNotificationController(api,state=newNotificationState()){
  let epoch=0,disposed=false;
  const token=()=>({epoch,id:state.accountId,session:state.sessionKey});
  const current=t=>!disposed&&t.epoch===epoch&&t.id===state.accountId&&t.session===state.sessionKey;
  const clearPrivate=()=>{state.address='';state.code='';state.challenge=null;};
  function accept(result){
    if(result?.account_id!==state.accountId)throw new Error('账号响应不一致，请重新读取');
    state.settings={...result};state.draft=draftOf(result);
  }
  async function perform(action,success,preserveDraft=false){
    if(disposed||!state.accountId||state.busy||state.loading)return false;
    const t=token();state.busy=true;state.error='';state.message='';
    try{const result=await action(t.id);if(!current(t))return false;const draft=state.draft;accept(result);if(preserveDraft)state.draft=draft;success?.(result);return true;}
    catch(e){if(current(t))state.error=e.message;return false;}
    finally{if(current(t))state.busy=false;}
  }
  return {
    state,
    switchAccount(id,session=''){
      epoch++;disposed=false;Object.assign(state,newNotificationState(),{accountId:id||'',sessionKey:session});
    },
    async reload(){
      if(disposed||!state.accountId||state.busy||state.loading)return false;
      const t=token();state.loading=true;state.error='';state.message='';
      try{const result=await api.getNotificationSettings(t.id);if(!current(t))return false;accept(result);clearPrivate();return true;}
      catch(e){if(current(t))state.error=e.message;return false;}
      finally{if(current(t))state.loading=false;}
    },
    edit(){if(!state.settings||state.busy||state.loading)return;state.draft=draftOf(state.settings);clearPrivate();state.error='';state.message='';state.open=true;},
    dirty(){return !!state.settings&&(!!state.address||!!state.code||!!state.challenge||JSON.stringify(state.draft)!==JSON.stringify(draftOf(state.settings)));},
    cancel(){if(state.busy)return false;epoch++;state.open=false;clearPrivate();state.draft=state.settings?draftOf(state.settings):null;state.error='';state.message='';return true;},
    request(){const address=state.address.trim();return perform(id=>api.requestNotificationVerification(id,{address}),result=>{
      state.challenge={id:result.challenge_id,expires_at:result.expires_at};state.code='';state.message='验证码已排队发送，10 分钟内有效。';
    },true);},
    cancelVerification(){if(state.busy)return false;clearPrivate();state.message='';state.error='';return true;},
    confirm(){
      if(!state.challenge){state.error='请先获取验证码';return Promise.resolve(false);}
      const data={challenge_id:state.challenge.id,code:state.code.trim()};
      return perform(id=>api.confirmNotificationVerification(id,data),()=>{clearPrivate();state.message='邮箱已验证，请选择并保存通知设置。';});
    },
    save(){
      if(!state.draft)return Promise.resolve(false);
      if(state.address||state.code||state.challenge){state.error='请先完成或取消邮箱验证，再保存通知设置';return Promise.resolve(false);}
      const payload={...state.draft,reminder_minutes:Number(state.draft.reminder_minutes)};
      return perform(id=>api.updateNotificationSettings(id,payload),()=>{clearPrivate();state.message='通知设置已保存。';state.open=false;});
    },
    close(){epoch++;disposed=true;clearPrivate();state.busy=false;state.loading=false;state.open=false;}
  };
}
