<template>
<AppShell v-if="store.user?.role==='root'" :active="tab==='dashboard'?'overview':'admin'" :before-leave="confirmLeave">
  <view v-if="executorMessage" class="notice" :class="{error:executorFailed}" role="status">{{executorMessage}}</view>
  <AdminDashboard :executor-busy="executorBusy" @executor-control="controlExecutor" v-if="tab==='dashboard'" :role="store.user.role" :data="dashboard.data" :filters="filters" :loading="dashboard.loading" :error="dashboard.error" :stale="dashboard.stale" @refresh="loadDashboard" @filters="changeFilters" @open-account="openAccount" @open-history="goOverview"><template #monitor><FormImport :role="store.user.role" :session-key="store.sessionEpoch" :active="visible&&foreground" :seats="store.seats" @changed="refresh();loadDashboard()"/><InboxMonitor :role="store.user.role" :data="inboxData" :error="inboxError" @refresh="loadInbox" @open-account="openAccount"/></template></AdminDashboard>
  <template v-else><view class="page-heading"><h1>{{tab==='users'?'用户管理':'管理记录'}}</h1><view class="actions"><button class="text-button" @click="tab=tab==='users'?'audit':'users'">{{tab==='users'?'管理记录':'用户管理'}}</button><button v-if="tab==='users'" class="button" :disabled="busy" @click="openAuthorization"><AppIcon name="plus"/>授权学号</button></view></view>
  <view v-if="error" class="notice error" role="alert">{{error}}</view><view v-if="message" class="notice success" role="status">{{message}}</view>
  <view v-if="!usersLoaded" class="empty">{{refreshing?'正在读取管理数据…':'管理数据未能载入，请刷新重试。'}}</view><template v-else-if="tab==='users'"><view class="management-counts"><text>全部 <b>{{users.length}}</b></text><text>已授权 <b>{{users.filter(u=>u.enabled).length}}</b></text><text>账号已保存 <b>{{users.filter(u=>u.account).length}}</b></text><text>定时开启 <b>{{users.filter(u=>u.account?.plan?.enabled).length}}</b></text></view>
    <view class="user-filters"><input class="input" v-model="search" placeholder="搜索学号、姓名或备注" aria-label="搜索授权用户"/><select class="filter" v-model="statusFilter" aria-label="授权用户状态"><option v-for="s in statusOptions" :key="s.key" :value="s.key">{{s.label}}</option></select><button class="button ghost" :disabled="busy||refreshing" @click="refresh">{{refreshing?'刷新中…':'刷新名单'}}</button></view>
    <view v-if="dirty" class="notice">有未保存备注。刷新保留草稿，保存只更新该同学的备注。</view><view v-if="!filtered.length" class="empty">暂无匹配学号<button class="text-button" @click="search='';statusFilter='all'">清除筛选</button></view>
    <view class="management-table-head"><text>账号</text><text>权限与计划</text><text>操作</text></view>
    <view v-for="u in filtered" :key="u.student_id" class="management-row"><view class="person"><h3>{{u.school_name||u.note||'待同学登录'}}</h3><text class="row-sub">{{u.student_id}}</text><text class="row-sub">最近网页登录 {{formatTime(u.last_login)}}</text></view><view class="permission-plan"><text class="pill" :class="u.enabled?'green':'red'">{{u.enabled?'已授权':'已停用'}}</text><text class="row-sub">{{u.account?.plan?.enabled?'定时已开启':'定时未开启'}}</text><text v-if="u.account?.plan" class="row-sub">{{u.account.plan.trigger||'未设放号时间'}} · {{u.account.plan.mode==='week'?'每周':'每天'}}<br/>{{planSeats(u.account.plan).map(seatName).join(' → ')||'座位配置待核对'}}</text></view><view class="management-actions"><button v-if="u.account" class="text-button" @click="openAccount(u.account.id)">计划与记录<AppIcon name="chevron-right"/></button><button class="button ghost small-button" :disabled="busy" @click="toggle(u)">{{u.enabled?'停用权限':'重新授权'}}</button></view><view class="management-note"><label :for="'note-'+u.student_id">管理备注</label><input :id="'note-'+u.student_id" :aria-label="'管理备注 '+u.student_id" class="input" v-model="notes[u.student_id]" placeholder="管理备注" maxlength="120" :disabled="busy"/><button class="text-button" :disabled="busy||notes[u.student_id]===(u.note||'')" @click="saveNote(u)">{{notes[u.student_id]===(u.note||'')?'备注已保存':'保存备注'}}</button></view></view>
    <p class="hint" style="margin-top:24px">停用会使会话失效并关闭未来计划；学校已有预约与历史保留。重新授权不会自动开启计划。</p>
  </template>
  <section v-else class="audit-section"><view class="section-head"><p class="muted small">最近100条 · 匹配 {{filteredAudit.length}} 条</p><button class="button ghost" :disabled="refreshing||busy" @click="refresh">刷新日志</button></view><input class="input" v-model="auditSearch" placeholder="搜索学号或操作"/><view v-if="!filteredAudit.length" class="empty">暂无匹配记录</view><view v-for="(item,i) in filteredAudit" :key="i" class="audit-row"><text>{{formatTime(item.at)}}</text><strong>{{actionName(item.action)}}</strong><text v-if="item.subject">{{item.subject}}</text></view></section>
  </template>
  <AppDialog :open="authorizationOpen" title="授权学号" :busy="busy" @close="closeAuthorization"><view class="field"><label>学号</label><input class="input" v-model="student" :disabled="busy" placeholder="5–20位数字" maxlength="20" @confirm="authorize"/></view><view class="field"><label>管理备注</label><input class="input" v-model="note" :disabled="busy" placeholder="例如：同学姓名" maxlength="120" @confirm="authorize"/></view><p class="hint">授权后由同学自行登录填写，不需要管理员代填学校密码。</p><view v-if="error" class="notice error">{{error}}</view><template #footer><button class="button secondary" :disabled="busy" @click="closeAuthorization">取消</button><button class="button" :disabled="busy||!student.trim()" @click="authorize">{{busy?'保存中…':'添加并授权'}}</button></template></AppDialog>
</AppShell>
</template>
<script setup>
import {ref,reactive,computed,watch,onMounted,onBeforeUnmount} from 'vue';
import {onShow,onHide,onLoad} from '@dcloudio/uni-app';
import {useWorkspace} from '@/stores/workspace.js';
import * as api from '@/services/local_api.js';
import {createDashboardLoader,filterUsers,mergeNotes,planSeats} from '@/services/admin_dashboard.mjs';
import SessionBar from '@/components/SessionBar.vue';
import AdminDashboard from '@/components/AdminDashboard.vue';import AppShell from '@/components/AppShell.vue';import AppDialog from '@/components/AppDialog.vue';import AppIcon from '@/components/AppIcon.vue';
import InboxMonitor from '@/components/InboxMonitor.vue';
import FormImport from '@/components/FormImport.vue';
const store=useWorkspace(),tab=ref('dashboard'),visible=ref(false),foreground=ref(true);
const tabs=[{key:'dashboard',label:'运行看板'},{key:'users',label:'用户管理'},{key:'audit',label:'管理日志'}];
const usersLoaded=ref(false),users=ref([]),notes=ref({}),previousNotes=ref({}),audit=ref([]),student=ref(''),note=ref(''),search=ref(''),statusFilter=ref('all'),auditSearch=ref(''),error=ref(''),message=ref(''),busy=ref(false),refreshing=ref(false);
let generation=0,timer=null;const authorizationOpen=ref(false);
const executorBusy=ref(false),executorMessage=ref(''),executorFailed=ref(false);
onLoad(q=>{if(['users','audit'].includes(q.tab))tab.value=q.tab;});
const filters=reactive({date:new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date()),days:14,kind:'scheduled'});
const dashboard=reactive({data:null,error:'',loading:false,stale:false});
const inboxData=ref(null),inboxError=ref('');let inboxGeneration=0;
const principal=()=>store.user?.role==='root'?store.sessionEpoch:null;
const loader=createDashboardLoader({fetch:api.getDashboard,identity:()=>visible.value&&foreground.value&&tab.value==='dashboard'&&principal()!==null?'root:'+principal():null,changed:state=>Object.assign(dashboard,state)});
const active=computed(()=>visible.value&&foreground.value&&tab.value==='dashboard'&&store.user?.role==='root');
watch([active,()=>store.sessionEpoch],()=>{clearInterval(timer);loader.stop();if(active.value){loader.load({...filters});timer=setInterval(loadDashboard,30000);}});
const loadDashboard=()=>{loader.load({...filters});loadInbox();};
async function loadInbox(){if(store.user?.role!=='root')return;const epoch=store.sessionEpoch,ticket=++inboxGeneration;try{const result=await api.getAdminInbox();if(epoch!==store.sessionEpoch||ticket!==inboxGeneration)return;inboxData.value=result;inboxError.value='';}catch(e){if(epoch===store.sessionEpoch&&ticket===inboxGeneration)inboxError.value=e.message;}}
async function controlExecutor(action){
  if(executorBusy.value||principal()===null)return;
  if(action!=='recover'){
    const confirmed=await new Promise(resolve=>uni.showModal({title:action==='pause'?'暂停自动预约？':'继续自动预约？',content:action==='pause'?'暂停未来自动提交，已有预约和计划保留。':'按现有计划继续；错过的放号不会补交。',success:r=>resolve(r.confirm)}));
    if(!confirmed)return;
  }
  const epoch=principal();executorBusy.value=true;executorMessage.value='';executorFailed.value=false;
  try{await api.controlExecutor(action);if(principal()!==epoch)return;executorMessage.value=action==='pause'?'自动预约已暂停':'执行状态已更新';loadDashboard();}
  catch(e){if(principal()===epoch){executorFailed.value=true;executorMessage.value=e.message;loadDashboard();}}
  finally{if(principal()===epoch)executorBusy.value=false;}
}
function changeFilters(changes){Object.assign(filters,changes);loadDashboard();}
const statusOptions=[{key:'all',label:'全部学号'},{key:'enabled',label:'已授权'},{key:'disabled',label:'已停用'},{key:'scheduled',label:'定时开启'},{key:'unconfigured',label:'尚未登录'}];
const filtered=computed(()=>filterUsers(users.value,search.value,statusFilter.value));
const dirty=computed(()=>users.value.some(u=>notes.value[u.student_id]!==previousNotes.value[u.student_id]));
const formatTime=value=>value?value.replace('T',' ').slice(0,19):'暂无记录';
const seatName=id=>store.seatMap[id]?.name||id;
const actionName=value=>({root_initialized:'初始化管理员',login:'登录',access_enabled:'授权/更新学号',access_disabled:'停用学号'}[value]||value);
const filteredAudit=computed(()=>audit.value.filter(item=>(formatTime(item.at)+' '+actionName(item.action)+' '+(item.subject||'')).includes(auditSearch.value.trim())));
async function refresh(){
  const gen=++generation,epoch=store.sessionEpoch;refreshing.value=true;
  try{
    await store.refresh();if(gen!==generation||epoch!==store.sessionEpoch)return;
    if(store.user?.role!=='root'){uni.reLaunch({url:'/pages/index/index'});return;}loadInbox();
    const [u,a]=await Promise.all([api.listUsers(),api.listAudit()]);
    if(gen!==generation||epoch!==store.sessionEpoch||store.user?.role!=='root')return;
    notes.value=mergeNotes(u.users,notes.value,previousNotes.value);previousNotes.value=Object.fromEntries(u.users.map(u=>[u.student_id,u.note||'']));users.value=u.users;audit.value=a.items;usersLoaded.value=true;error.value='';
  }catch(e){if(gen===generation&&epoch===store.sessionEpoch)error.value=e.message;}
  finally{if(gen===generation&&epoch===store.sessionEpoch)refreshing.value=false;}
}
async function update(id,data){
  if(busy.value)return false;const epoch=principal();busy.value=true;message.value='';error.value='';
  try{await api.updateAccess(id,data);if(principal()!==epoch)return false;await refresh();if(principal()!==epoch)return false;message.value='管理信息已更新。';return true;}
  catch(e){if(principal()===epoch)error.value=e.message;return false;}
  finally{if(principal()===epoch)busy.value=false;}
}
async function add(){const id=student.value.trim();if(!/^[0-9]{5,20}$/.test(id)){error.value='学号须为5–20位数字';return;}if(users.value.some(u=>u.student_id===id)){error.value='该学号已存在，请在名单中管理。';return;}if(await update(id,{enabled:true,note:note.value,expected_revision:null})){student.value='';note.value='';}}
const saveNote=u=>update(u.student_id,{enabled:u.enabled,note:notes.value[u.student_id],expected_revision:u.revision});
async function toggle(u){if(u.enabled){const confirmed=await new Promise(resolve=>uni.showModal({title:'停用 '+(u.school_name||u.note||u.student_id)+'？',content:'学号 '+u.student_id+' 将退出登录并关闭未来定时计划。学校已有预约和历史保留。',success:r=>resolve(r.confirm)}));if(!confirmed)return;}await update(u.student_id,{enabled:!u.enabled,note:u.note||'',expected_revision:u.revision});}
async function confirmLeave(){if(busy.value)return false;if(!dirty.value)return true;return new Promise(resolve=>uni.showModal({title:'有未保存的管理备注',content:'离开页面将放弃未保存的备注，是否继续？',success:r=>resolve(r.confirm)}));}
async function openAccount(id){if(!id||!await confirmLeave())return;store.select(id);uni.navigateTo({url:'/pages/account_info/account_info?account_id='+encodeURIComponent(id)});}
async function goOverview(){if(await confirmLeave())uni.reLaunch({url:'/pages/index/index'});}
const documentVisibility=()=>{foreground.value=typeof document==='undefined'||!document.hidden;};
onMounted(()=>{documentVisibility();if(typeof document!=='undefined')document.addEventListener('visibilitychange',documentVisibility);});
onBeforeUnmount(()=>{clearInterval(timer);loader.stop();if(typeof document!=='undefined')document.removeEventListener('visibilitychange',documentVisibility);});
watch(()=>store.sessionEpoch,()=>{executorBusy.value=false;executorMessage.value='';executorFailed.value=false;inboxGeneration++;inboxData.value=null;inboxError.value='';generation++;usersLoaded.value=false;users.value=[];notes.value={};previousNotes.value={};audit.value=[];error.value='';message.value='';busy.value=false;refreshing.value=false;student.value='';note.value='';});
onShow(()=>{visible.value=true;refresh();});onHide(()=>{visible.value=false;generation++;inboxGeneration++;refreshing.value=false;});

function openAuthorization(){student.value='';note.value='';error.value='';message.value='';authorizationOpen.value=true;}
async function authorize(){const valid=/^[0-9]{5,20}$/.test(student.value.trim());await add();if(valid&&!student.value&&message.value&&!error.value)authorizationOpen.value=false;}
async function closeAuthorization(){if(busy.value)return;if(student.value.trim()||note.value.trim()){const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的授权信息',content:'是否放弃当前填写？',success:r=>resolve(r.confirm)}));if(!discard)return;}authorizationOpen.value=false;}
</script>
<style scoped>
.management-counts{display:flex;gap:24px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-bottom:26px}.management-counts b{color:var(--ink);margin-left:6px}.user-filters{display:flex;gap:12px;align-items:center;margin-bottom:22px}.user-filters>.input{flex:1;max-width:440px}.management-table-head,.management-row{display:grid;grid-template-columns:1.05fr 1.5fr auto;gap:20px}.management-table-head{border-bottom:1px solid var(--line);padding:12px 0;color:var(--muted);font-size:12px}.management-row{padding:24px 0;border-bottom:1px solid var(--line);align-items:start}.management-actions{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap}.management-note{grid-column:1/-1;display:flex;align-items:center;gap:12px;max-width:640px}.management-note label{font-size:12px;color:var(--muted);flex:none}.management-note .input{flex:1;min-width:0;height:44px;min-height:44px}.management-note .text-button{flex:none;font-size:12px}.permission-plan{min-width:0}.audit-row{display:flex;gap:22px;flex-wrap:wrap;font-size:14px;padding:20px 0}.audit-row>text:first-child{font-size:13px;color:var(--muted)}@media(max-width:900px){.management-row{grid-template-columns:1fr 1.3fr}.management-actions{grid-column:1/-1}.management-table-head{display:none}.management-note{max-width:none}}@media(max-width:599px){.page-heading{align-items:flex-start}.page-heading>.actions{gap:4px}.page-heading .button{padding:10px 12px}.management-counts{gap:12px 18px}.user-filters{flex-wrap:wrap}.user-filters>.input{max-width:none;flex-basis:100%}.user-filters>select{flex:1;min-width:0}.management-row{grid-template-columns:1fr;gap:14px}.management-actions,.management-note{grid-column:1}.permission-plan{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.permission-plan>.row-sub:last-child{width:100%}.management-note{display:grid;grid-template-columns:1fr auto;gap:8px}.management-note label{grid-column:1/-1}.management-note .input{grid-column:1}.audit-row{gap:6px 14px}.audit-row>text:first-child{width:100%}}
</style>
