const empty=()=>({items:[],counts:null,nextCursor:null,loading:false,loaded:false,error:'',stale:false});

// This loader is private to one account view; it never changes the shared overview.
export function createAccountHistoryLoader({fetch,changed}){
  let account='',generation=0,state=empty();
  const publish=patch=>{
    state={...state,...patch};
    changed({...state,items:[...state.items],counts:state.counts?{...state.counts}:null});
  };
  async function request(append=false){
    if(!account||state.loading||(append&&!state.nextCursor))return;
    const id=account,token=++generation,filters={account_id:id,limit:10};
    if(append)filters.cursor=state.nextCursor;
    publish({loading:true,error:'',stale:false});
    try{
      const page=await fetch(filters);
      if(token!==generation||id!==account)return;
      if(!Array.isArray(page?.items)||!page.counts||typeof page.counts!=='object'
          ||page.items.some(item=>!item||item.account_id!==id||typeof item.id!=='string')){
        throw new Error('预约历史返回异常，请重试');
      }
      const previous=append?state.items:[],seen=new Set(previous.map(item=>item.id));
      const items=[...previous,...page.items.filter(item=>!seen.has(item.id)&&seen.add(item.id))];
      publish({items,counts:page.counts,nextCursor:page.next_cursor||null,loaded:true,error:'',stale:false});
    }catch(error){
      if(token===generation&&id===account)publish({error:error?.message||'预约历史读取失败，请重试',stale:state.loaded});
    }finally{
      if(token===generation&&id===account)publish({loading:false});
    }
  }
  return {
    select(id){generation++;account=id||'';state=empty();publish({});return request();},
    refresh(){return request();},
    more(){return request(true);},
    stop(){generation++;account='';state=empty();publish({});}
  };
}

export function firstRequestTime(run){
  const seconds=run?.attempts?.[0]?.timings?.request_started_at;
  if(typeof seconds!=='number'||!Number.isFinite(seconds)||seconds<=0)return '—';
  const date=new Date(seconds*1000);
  if(!Number.isFinite(date.getTime()))return '—';
  const time=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date);
  return time+'.'+String(date.getUTCMilliseconds()).padStart(3,'0');
}
