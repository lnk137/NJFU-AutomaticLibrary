const copy=value=>JSON.parse(JSON.stringify(value));

export function parseSeatTags(text){
  const tags=[...new Set(String(text||'').split(/[,，\n]/).map(v=>v.trim()).filter(Boolean))];
  if(tags.length>5)throw new Error('每个座位最多填写5个标签');
  if(tags.some(tag=>Array.from(tag).length>20))throw new Error('每个标签最多20个字符');
  return tags;
}
export function librarySnapshot(account){
  return {account_id:account?.id,revision:account?.seat_preferences_revision||0,
    favorites:copy(account?.seat_preferences?.favorites||[]),groups:copy(account?.seat_preferences?.groups||[])};
}
export function groupSeatSnapshot(group,seats){
  const ids=group?.seat_ids,known=new Set((seats||[]).map(s=>s.id));
  if(!Array.isArray(ids)||!ids.length||ids.length>5||new Set(ids).size!==ids.length||ids.some(id=>!known.has(id)))
    throw new Error('分组须包含1–5个有效目录座位，请先修改分组');
  return [...ids];
}
export function favoriteEntries(data,seats,search=''){
  const map=new Map((seats||[]).map(s=>[s.id,s])),query=search.trim().toLowerCase();
  return (data.favorites||[]).map(f=>({...map.get(f.seat_id),id:f.seat_id,name:map.get(f.seat_id)?.name||f.seat_id,
    location:map.get(f.seat_id)?.location||'目录中已无此座位',available:map.has(f.seat_id),tags:[...f.tags]}))
    .filter(s=>(s.name+' '+s.location+' '+s.tags.join(' ')).toLowerCase().includes(query));
}
export function mergeLibraryAccount(account,response){
  if(!account||response.account_id!==account.id||!Number.isInteger(response.revision)||response.revision<(account.seat_preferences_revision||0))return account;
  return {...account,seat_preferences_revision:response.revision,
    seat_preferences:{favorites:copy(response.favorites),groups:copy(response.groups)}};
}
