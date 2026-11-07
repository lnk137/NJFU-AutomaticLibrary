const paths={overview:'/pages/index/index',plan:'/pages/account_info/account_info',profile:'/pages/profile/profile',dashboard:'/pages/admin/admin',admin:'/pages/admin/admin?tab=users'};
export function navigationItems(role){
  if(role==='root')return [{id:'dashboard',label:'运行看板',icon:'home'},{id:'admin',label:'用户管理',icon:'users'},{id:'profile',label:'管理员',icon:'shield'}];
  if(role==='student')return [{id:'overview',label:'我的预约',icon:'home'},{id:'plan',label:'预约计划',icon:'calendar'},{id:'profile',label:'我的',icon:'user'}];
  return [];
}
export function navigationRoute(role,id){return navigationItems(role).some(item=>item.id===id)?paths[id]:null;}
export function activeNavigation(role,page){if(role==='root')return ['plan','records','admin'].includes(page)?'admin':page==='profile'?'profile':'dashboard';return page==='records'?'overview':page;}
export function schoolRecordLabel(value){return value===1027?'学校已确认':value===null||value===undefined?'学校状态未知':'学校状态 '+value;}
