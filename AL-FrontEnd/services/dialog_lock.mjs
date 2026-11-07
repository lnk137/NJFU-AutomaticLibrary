// A dialog owns one lease; switching or overlapping dialogs must not unlock each other.
const locks=new WeakMap();
export function acquireScrollLock(body){
  let state=locks.get(body);
  if(!state){state={count:0,overflow:body.style.overflow};locks.set(body,state);}
  state.count++;body.style.overflow='hidden';let released=false;
  return ()=>{
    if(released)return false;released=true;
    if(--state.count)return false;
    body.style.overflow=state.overflow;locks.delete(body);return true;
  };
}
