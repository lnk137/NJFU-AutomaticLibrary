<template>
<AppShell active="records"><view class="page-heading"><view class="title-row"><button class="icon-button" :aria-label="store.user?.role==='root'?'返回账号计划':'返回我的预约'" @click="back"><AppIcon name="chevron-left"/></button><h1>学校记录</h1></view><button class="button ghost" :disabled="busy||!store.activeAccount" @click="query"><AppIcon name="refresh-cw"/>{{busy?'查询中…':'刷新'}}</button></view>
<view v-if="store.user?.role==='root'" class="account-context"><strong>管理账号 · {{store.activeAccount?.alias}}</strong><text>{{store.activeAccount?.student_id}}</text><details><summary>切换账号</summary><AccountSelector can-manage :accounts="store.accounts" :active="store.activeAccountId" @select="select"/></details></view>
<view v-if="error||snapshot?.error" class="notice error" role="alert">{{error||(snapshot?.error==='cache_capacity'?'离线记录缓存超出容量，请实时认证后重新查询学校记录。':'最近查询未成功 · 显示上次查询结果。')}}</view>
<view class="filter-bar"><label>日期<select class="filter" v-model="dateFilter" aria-label="记录使用日期"><option value="all">全部日期</option><option v-for="date in dates" :key="date" :value="date">{{date}}</option></select></label><label>状态<select class="filter" v-model="statusFilter" aria-label="学校记录状态"><option value="all">全部状态</option><option value="confirmed">学校已确认</option><option value="other">其他状态</option></select></label><text class="snapshot-stamp">最近成功查询 {{checkedTime}}</text></view>
<view v-if="!snapshot?.checked_at" class="empty"><AppIcon name="file-text"/><h2>尚未查询学校记录</h2><p>查询今天至未来3天的学校实际记录。</p><button class="button secondary" style="margin-top:20px" :disabled="busy||!store.activeAccount" @click="query">查询学校记录</button></view>
<view v-else-if="!filteredRecords.length" class="empty"><AppIcon name="file-text"/><h2>暂无匹配记录</h2><button class="text-button" @click="dateFilter='all';statusFilter='all'">清除筛选</button></view>
<view v-else class="records-table"><view class="records-table-head"><text>座位</text><text>使用日期</text><text>时段</text><text>学校状态</text><text/></view><button class="school-record-row" v-for="r in filteredRecords" :key="r.id" @click="showDetail(r.id)"><strong>{{seatName(r.seat_id)}}</strong><text>{{r.begin.slice(0,10)}}</text><text>{{r.begin.slice(11,16)}}–{{r.end.slice(11,16)}}</text><text class="pill" :class="r.status===1027?'green':''">{{schoolRecordLabel(r.status)}}</text><AppIcon name="chevron-right"/></button></view>
<p v-if="cancelMessage" class="notice success" role="status">{{cancelMessage}}</p><p v-if="linkMessage" class="notice" role="status">{{linkMessage}}</p><button v-if="requested?.intent==='cancel'" class="text-button" :disabled="busy" @click="retryMailTarget">重新核对邮件中的预约</button>
<p class="footer">以学校最新记录为准。</p>
<AppDialog :open="!!detailRecord&&!mailTarget" title="预约详情" :busy="busy" @close="detailId='' "><template v-if="detailRecord"><view class="record-detail-seat">{{seatName(detailRecord.seat_id)}}</view><text class="pill" :class="detailRecord.status===1027?'green':''">{{schoolRecordLabel(detailRecord.status)}}</text><dl class="record-detail-grid"><dt>使用日期</dt><dd>{{detailRecord.begin.slice(0,10)}}</dd><dt>使用时段</dt><dd>{{detailRecord.begin.slice(11,16)}}–{{detailRecord.end.slice(11,16)}}</dd><dt>所属学号</dt><dd>{{detailRecord.student_id}}</dd><dt>学校状态码</dt><dd>{{detailRecord.status}}</dd><dt>最近查询</dt><dd>{{checkedTime}}</dd></dl><p class="hint">状态来自学校查询。</p></template><template #footer><CancelReservation v-if="detailRecord&&store.activeAccount" :account-id="store.activeAccountId" :revision="store.activeAccount.revision" :record="detailRecord" :session-key="store.sessionEpoch" :seat-map="store.seatMap" :allow-live="!!store.status.allow_live" :disabled="busy||store.user?.auth_mode==='offline'" @cancelled="cancelled"/><button class="button secondary" :disabled="busy" @click="query">{{busy?'查询中…':'重新核对学校记录'}}</button></template></AppDialog>
<CancelReservation v-if="mailTarget&&store.activeAccount" standalone auto-preview :account-id="store.activeAccountId" :revision="store.activeAccount.revision" :record="mailTarget" :session-key="store.sessionEpoch" :target-key="mailAttempt" :seat-map="store.seatMap" :allow-live="!!store.status.allow_live" :disabled="store.user?.auth_mode==='offline'" @cancelled="cancelled" @closed="linkMessage='已关闭取消确认，可重新核对邮件中的同一条预约。'"/>
</AppShell>
</template>
<script setup>
import {ref,computed,watch} from 'vue';
import CancelReservation from '@/components/CancelReservation.vue';
import {rememberReservationLink,clearReservationLink,reservationLinkTarget} from '@/services/reservation_link.mjs';import {onShow,onHide,onLoad} from '@dcloudio/uni-app';import {useWorkspace} from '@/stores/workspace.js';import AccountSelector from '@/components/AccountSelector.vue';import SessionBar from '@/components/SessionBar.vue';
import AppShell from '@/components/AppShell.vue';import AppDialog from '@/components/AppDialog.vue';import AppIcon from '@/components/AppIcon.vue';import {schoolRecordLabel} from '@/services/navigation.mjs';
const store=useWorkspace(),busy=ref(false),error=ref('');let generation=0;const requested=ref(null),mailReady=ref(false),mailAttempt=ref(0),cancelMessage=ref(''),linkMessage=ref('');
const snapshot=computed(()=>store.snapshots[store.activeAccountId]);
const mailTarget=computed(()=>mailReady.value&&requested.value?.intent==='cancel'&&store.activeAccountId===requested.value.accountId?(snapshot.value?.records?.find(r=>r.id===requested.value.recordId)||{id:requested.value.recordId}):null);
const checkedTime=computed(()=>snapshot.value?.checked_at?snapshot.value.checked_at.replace('T',' ').slice(0,19):'尚未查询');
const back=()=>uni.reLaunch({url:store.user?.role==='root'?'/pages/account_info/account_info?account_id='+encodeURIComponent(store.activeAccountId):'/pages/index/index'}),edit=()=>uni.navigateTo({url:'/pages/account_info/account_info'});
function ownsTarget(){const target=requested.value;return !!target&&store.accounts.some(a=>a.id===target.accountId)&&(store.user?.role==='root'||store.user?.account_id===target.accountId);}
function resolveLink(){
  const target=requested.value;if(!target)return;
  if(!ownsTarget()){mailReady.value=false;linkMessage.value='这条预约链接不属于当前账号，请使用对应账号登录。';return;}
  if(store.activeAccountId!==target.accountId){mailReady.value=false;linkMessage.value='已切换账号，邮件中的预约目标已保留。';return;}
  if(target.intent==='cancel'){detailId.value='';mailReady.value=true;linkMessage.value='';return;}
  const found=snapshot.value?.records?.find(r=>r.id===target.recordId);
  if(found){detailId.value=found.id;requested.value=null;clearReservationLink();}
  else linkMessage.value='上次保存的记录中未找到这条预约，请刷新学校记录核对。';
}
function showDetail(id){mailReady.value=false;detailId.value=id;}
function retryMailTarget(){if(busy.value)return;resolveLink();if(mailReady.value)mailAttempt.value++;}
async function select(id){if(busy.value)return;generation++;mailReady.value=false;store.select(id);detailId.value='';error.value='';if(!id){edit();return;}const token=generation;try{await store.records();if(token===generation)resolveLink();}catch(e){if(token===generation)error.value=e.message;}}
async function cancelled(result){const token=generation,accountId=store.activeAccountId;cancelMessage.value=result.message;detailId.value='';try{await store.records();}catch(e){if(token===generation&&accountId===store.activeAccountId)error.value=e.message;}}
async function query(){if(busy.value)return;const gen=++generation;busy.value=true;error.value='';try{await store.records(true);if(gen===generation){resolveLink();if(mailReady.value)mailAttempt.value++;}}catch(e){if(gen===generation)error.value=e.message;}finally{if(gen===generation)busy.value=false;}}
onLoad(q=>{generation++;mailReady.value=false;detailId.value='';requested.value=reservationLinkTarget(q.account_id,q.record_id,q.intent);if(requested.value)rememberReservationLink(requested.value.accountId,requested.value.recordId,undefined,requested.value.intent);});
onShow(async()=>{const token=++generation;try{
  await store.refresh();if(token!==generation)return;
  if(requested.value&&!ownsTarget()){resolveLink();return;}
  if(requested.value)store.select(requested.value.accountId);
  if(store.activeAccountId)await store.records();if(token!==generation)return;
  resolveLink();
}catch(e){if(token===generation)error.value=e.message;}});
onHide(()=>{generation++;mailReady.value=false;busy.value=false;store.invalidate();});

const dateFilter=ref('all'),statusFilter=ref('all'),detailId=ref('');
const dates=computed(()=>[...new Set((snapshot.value?.records||[]).map(r=>r.begin.slice(0,10)))].sort().reverse());
const filteredRecords=computed(()=>(snapshot.value?.records||[]).filter(r=>(dateFilter.value==='all'||r.begin.startsWith(dateFilter.value))&&(statusFilter.value==='all'||statusFilter.value==='confirmed'&&r.status===1027||statusFilter.value==='other'&&r.status!==1027)));
const detailRecord=computed(()=>(snapshot.value?.records||[]).find(r=>r.id===detailId.value));
const seatName=id=>store.seatMap[id]?.name||id;
watch(()=>[store.activeAccountId,store.sessionEpoch],()=>{detailId.value='';mailReady.value=false;dateFilter.value='all';statusFilter.value='all';},{flush:'sync'});
</script>
<style scoped>
.filter-bar{gap:20px;margin-bottom:24px}.filter-bar label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:10px}.snapshot-stamp{font-size:12px;color:var(--muted);margin-left:auto}.records-table-head,.school-record-row{display:grid;grid-template-columns:1.2fr 1fr 1.2fr 1.2fr 30px;gap:16px;align-items:center}.records-table-head{padding:14px 18px;border-bottom:1px solid var(--line);color:var(--muted);font-size:12px}.school-record-row{width:100%;text-align:left;padding:24px 18px;border-bottom:1px solid var(--line);font-size:14px;min-height:88px}.school-record-row:hover{background:var(--tint)}.record-detail-seat{font-size:32px;font-weight:650;margin-bottom:14px;overflow-wrap:anywhere}.record-detail-grid{display:grid;grid-template-columns:100px 1fr;gap:18px;font-size:14px;margin:30px 0}.record-detail-grid dt{color:var(--muted)}.record-detail-grid dd{margin:0;overflow-wrap:anywhere}.account-context details{width:100%}@media(max-width:599px){.page-heading .button{padding:10px 12px}.filter-bar{gap:12px}.filter-bar label{display:block;flex:1;min-width:0}.filter-bar select{display:block;width:100%;margin-top:8px;min-width:0}.snapshot-stamp{margin:0;width:100%}.records-table-head{display:none}.school-record-row{grid-template-columns:1fr auto;gap:7px 12px;padding:20px 0}.school-record-row strong{font-size:18px}.school-record-row>text:nth-child(2){font-size:12px;color:var(--muted)}.school-record-row>text:nth-child(3){grid-column:1;font-size:14px}.school-record-row .pill{grid-column:2;grid-row:2;justify-self:end}.school-record-row>.app-icon{display:none}}
</style>
