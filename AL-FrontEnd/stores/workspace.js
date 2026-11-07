import {defineStore} from 'pinia';
import * as api from '@/services/local_api.js';
import {beginRequest,canApplyResponse,chooseAccount,canApplySnapshot,canApplyRunPage,readLogoutMarker,writeLogoutMarker} from '@/services/workspace.mjs';

export const useWorkspace=defineStore('workspace',{
  state:()=>({user:null,sessionEpoch:0,logoutNotice:readLogoutMarker().notice,suppressAutoLogin:readLogoutMarker().blocked,accounts:[],activeAccountId:'',requestGeneration:0,recordSequences:{},drafts:{},snapshots:{},runs:[],counts:{},nextCursor:null,runCache:null,
    seats:[],policy:null,status:{allow_live:false},error:'',ready:false,loading:false,runFilterKey:'',runSequence:0}),
  getters:{activeAccount:s=>s.accounts.find(a=>a.id===s.activeAccountId)||null,seatMap:s=>Object.fromEntries(s.seats.map(x=>[x.id,x]))},
  actions:{
    clearSession(){const epoch=this.sessionEpoch+1,generation=this.requestGeneration+1;this.$reset();this.sessionEpoch=epoch;this.requestGeneration=generation;},
    async login(data){const result=await api.login(data);writeLogoutMarker(null);this.clearSession();this.user=result.user;},
    async logout(notice=''){
      let result,unconfirmed=false;
      try{result=await api.logout();unconfirmed=result?.upstream_revoked===false;}catch{unconfirmed=true;}
      finally{const message=[notice,unconfirmed?'本机页面数据已清除，但服务器会话撤销尚未确认；请关闭其他已登录页面，连接恢复后重新登录并退出。':''].filter(Boolean).join(' ');writeLogoutMarker(message);this.clearSession();this.suppressAutoLogin=true;this.logoutNotice=message;}
      return result;
    },
    select(id){const next=chooseAccount(this.$state,id);this.activeAccountId=next.activeAccountId;this.requestGeneration=next.requestGeneration;},
    invalidate(){this.requestGeneration++;},
    async refresh(){
      if(this.suppressAutoLogin){uni.reLaunch({url:'/pages/login/login'});return;}
      const epoch=this.sessionEpoch;const session=await api.getSession();if(epoch!==this.sessionEpoch)return;this.user=session.user;
      const [a,s,p]=await Promise.all([api.listAccounts(),api.getStatus(),this.policy?Promise.resolve(this.policy):api.getPolicy()]);if(epoch!==this.sessionEpoch)return;this.accounts=a.accounts;this.status=s;this.policy=p;
      if(this.activeAccountId&&!this.accounts.some(a=>a.id===this.activeAccountId))this.select('');
      if(!this.ready){this.ready=true;if(this.accounts.length)this.select(this.accounts[0].id);}
      if(!this.seats.length){const seats=await api.getSeats();if(epoch===this.sessionEpoch)this.seats=seats.seats;}
    },
    async loadRuns(filters={},append=false){
      const key=JSON.stringify([filters.account_id||'',filters.date||'']);
      if(append&&key!==this.runFilterKey)return;
      const epoch=this.sessionEpoch;const sequence=++this.runSequence;this.runFilterKey=key;
      const result=await api.listRuns(filters);
      if(epoch!==this.sessionEpoch)return;
      if(!canApplyRunPage({key,sequence},this.runFilterKey,this.runSequence))return;
      const previous=append?this.runs:[],seen=new Set(previous.map(row=>row.id));
      this.runs=[...previous,...result.items.filter(row=>!seen.has(row.id)&&seen.add(row.id))];this.counts=result.counts;this.nextCursor=result.next_cursor;
      this.runCache=result.cache_only?{cache_only:true,cached_at:result.cached_at,cache_limit:result.cache_limit,cache_truncated:result.cache_truncated,cached_total:result.cached_total}:null;
    },
    async records(force=false){
      const epoch=this.sessionEpoch;const id=this.activeAccountId;if(!id)return;
      const sequence=(this.recordSequences[id]||0)+1;this.recordSequences[id]=sequence;
      const request=beginRequest(id,this.requestGeneration,sequence);
      const result=await (force?api.queryRecords(id):api.getRecords(id));
      if(epoch!==this.sessionEpoch)return null;
      if(canApplySnapshot(request,this.recordSequences[id],this.accounts.find(a=>a.id===id)?.revision,result.revision))this.snapshots[id]=result;
      return canApplyResponse(request,this.activeAccountId,this.requestGeneration)?result:null;
    }
  }
});
