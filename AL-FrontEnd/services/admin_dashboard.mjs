export const formatMs=value=>Number.isFinite(value)?value.toFixed(1)+' ms':'无数据';
export const planSeats=plan=>Array.isArray(plan?.seat_ids)?plan.seat_ids.filter(id=>typeof id==='string'):[];

export function lineSegments(rows,key,max) {
  const segments=[];let points=[];
  rows.forEach((row,i)=>{
    const value=row.request?.[key];
    if(!Number.isFinite(value)){if(points.length)segments.push(points.join(' '));points=[];return;}
    points.push(`${24+i*592/Math.max(rows.length-1,1)},${170-value/Math.max(max,1)*140}`);
  });
  if(points.length)segments.push(points.join(' '));
  return segments;
}

export function mergeNotes(users,drafts={},previous={}) {
  return Object.fromEntries(users.map(user=>[user.student_id,
    Object.hasOwn(previous,user.student_id)&&drafts[user.student_id]!==previous[user.student_id]
      ?drafts[user.student_id]:user.note||'']));
}

export function filterUsers(users,query='',status='all') {
  const text=query.trim().toLowerCase();
  return users.filter(user=>[user.student_id,user.note,user.school_name,user.name].filter(Boolean).join(' ').toLowerCase().includes(text))
    .filter(user=>status==='all'||status==='enabled'&&user.enabled||status==='disabled'&&!user.enabled||
      status==='scheduled'&&user.account?.plan?.enabled||status==='unconfigured'&&!user.account);
}

export function createDashboardLoader({fetch,identity,changed=()=>{}}) {
  const state={data:null,error:'',loading:false,stale:false};
  let active=true,busy=false,version=0,pending=null;
  const notify=()=>changed({...state});
  const stop=()=>{active=false;version++;pending=null;Object.assign(state,{data:null,error:'',loading:false,stale:false});notify();};
  async function load(filters) {
    if(!identity()){stop();return;}
    active=true;pending={...filters};version++;
    if(state.data)state.stale=true;
    if(busy){notify();return;}
    busy=true;state.loading=true;notify();
    try {
      while(active&&pending&&identity()) {
        const request=pending,sequence=version,principal=identity();pending=null;
        try {
          const data=await fetch(request);
          if(active&&identity()===principal&&sequence===version)Object.assign(state,{data,error:'',stale:false});
        } catch(error) {
          if(active&&identity()===principal&&sequence===version)Object.assign(state,{error:error.message,stale:true});
        }
        if(identity()!==principal)Object.assign(state,{data:null,error:'',stale:false});
        if(active)notify();
      }
    } finally {
      busy=false;if(active){state.loading=false;notify();}
    }
  }
  return {state,load,stop};
}
