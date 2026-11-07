import {defineStore} from 'pinia';
import * as api from '@/services/local_api.js';
import {beginRequest,canApplyResponse,chooseAccount,canApplySnapshot,canApplyRunPage,readLogoutMarker,writeLogoutMarker} from '@/services/workspace.mjs';

export const useWorkspace=defineStore('workspace',{
  state:()=>({user:null,sessionEpoch:0,logoutNotice:readLogoutMarker().notice,suppressAutoLogin:readLogoutMarker().blocked,accounts:[],activeAccountId:'',requestGeneration:0,recordSequences:{},drafts:{},snapshots:{},runs:[],counts:{},nextCursor:null,runCache:null,
    seats:[],policy:null,status:{allow_live:false},error:'',ready:false,loading:false,runFilterKey:'',runAppliedKey:'',runCursorAfterId:'',runSequence:0,runRefreshOffset:0,runRefreshError:''}),
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
    async loadRuns(filters={},append=false,preserve=false,detailId=''){
      const key=JSON.stringify([filters.account_id||'',filters.date||'']);
      if(append&&key!==this.runAppliedKey)return;
      preserve=preserve&&key===this.runAppliedKey;
      const previous=append||preserve?[...this.runs]:[],oldCursor=this.nextCursor,oldCursorAfterId=this.runCursorAfterId;
      if(key!==this.runAppliedKey)this.runRefreshOffset=0;
      const epoch=this.sessionEpoch,generation=this.requestGeneration,sequence=++this.runSequence;this.runFilterKey=key;
      const current=()=>epoch===this.sessionEpoch&&generation===this.requestGeneration&&canApplyRunPage({key,sequence},this.runFilterKey,this.runSequence);
      const result=await api.listRuns(filters);
      if(!current())return;
      const fresh=new Map(result.items.map(row=>[row.id,row])),seen=new Set(previous.map(row=>row.id));
      let refreshError='';
      if(preserve){
        const focused=previous.find(row=>row.id===detailId&&!fresh.has(row.id));
        const active=previous.filter(row=>row.id!==focused?.id&&!fresh.has(row.id)&&['queued','preparing','submitting','uncertain'].includes(row.status));
        const offset=active.length?this.runRefreshOffset%active.length:0,rotated=[...active.slice(offset),...active.slice(0,offset)];
        const targets=[...(focused?[focused]:[]),...rotated].slice(0,4);
        const updates=await Promise.allSettled(targets.map(async row=>{const response=await api.getRun(row.id);if(response.run?.id!==row.id||response.run.account_id!==row.account_id)throw new Error('记录响应不一致');return response.run;}));
        if(!current())return;
        this.runRefreshOffset=offset+targets.length-(focused?1:0);
        for(const update of updates){if(update.status==='fulfilled')fresh.set(update.value.id,update.value);else refreshError=result.cache_only?'离线缓存未包含或未能刷新部分较早记录，保留上次结果；请实时登录后核对。':'部分较早记录刷新失败，已保留上次结果；稍后将继续刷新。';}
      }
      let rows;
      if(append){
        const anchor=previous.findIndex(row=>row.id===oldCursorAfterId),at=anchor<0?previous.length:anchor+1;
        rows=[...previous.slice(0,at).filter(row=>!fresh.has(row.id)),...result.items,...previous.slice(at).filter(row=>!fresh.has(row.id))];
      }else rows=[...result.items,...previous.filter(row=>!result.items.some(item=>item.id===row.id))];
      this.runs=rows.map(row=>fresh.get(row.id)||row);this.counts=result.counts;this.runAppliedKey=key;
      const overlap=result.items.some(row=>seen.has(row.id));
      const keepCursor=preserve&&!result.cache_only&&overlap;
      this.nextCursor=keepCursor?oldCursor:result.next_cursor;
      this.runCursorAfterId=keepCursor?oldCursorAfterId:result.items.at(-1)?.id||'';
      this.runRefreshError=refreshError;
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
