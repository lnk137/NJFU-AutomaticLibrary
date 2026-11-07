<template>
<AppShell active="overview"><view class="page-heading"><h1>{{store.user?.role==='root'?'执行记录':'我的预约'}}</h1><button class="button ghost" :disabled="loading" @click="refresh"><AppIcon name="refresh-cw"/>{{loading?'刷新中…':'刷新'}}</button></view>
<SchedulerStatus v-if="store.status.clock_ok===false||!store.status.allow_live||store.status.scheduler_warning||store.status.last_error" :status="store.status"/>
<view v-if="error" class="notice error" role="alert">{{error}}<button class="text-button" @click="refresh">重试</button></view>
<view v-if="store.user?.role==='root'" class="account-context"><strong>管理员查看全部执行记录</strong><button class="text-button" @click="goAdmin">返回运行看板</button></view>
<view v-if="store.user?.role!=='root'" class="overview-grid">
  <section class="surface"><view class="section-head"><h2>学校已确认</h2><button class="text-button" @click="go('reservation_info')">学校记录<AppIcon name="chevron-right"/></button></view><template v-if="confirmedRecord"><view class="large-seat">{{seatName(confirmedRecord.seat_id)}}</view><p class="reservation-time">{{confirmedRecord.begin.slice(11,16)}}–{{confirmedRecord.end.slice(11,16)}}</p><p class="muted">{{confirmedRecord.begin.slice(0,10)}}</p></template><view v-else class="empty"><AppIcon name="calendar"/><h3>缓存中暂无已确认预约</h3></view><p class="hint">学校回执时间：{{snapshotStamp}}。首页刷新读取后台保存的回执；学校最新状态请到“学校记录”查询。</p><p v-if="recordsError||snapshotStale" class="notice" role="status">{{recordsError?'回执刷新失败：'+recordsError+'。保留上次结果。':'回执已超过 5 分钟或尚未查询，请核对学校最新状态。'}}</p><view class="surface-actions"><button class="button secondary" :disabled="!store.activeAccount" @click="goToday">当天预约</button><button class="text-button" @click="go('reservation_info')">查看全部</button></view></section>
  <section class="surface"><view class="section-head"><h2>预约计划</h2><text class="pill" :class="store.activeAccount?.plan?.enabled?'green':''">{{store.activeAccount?.plan?.enabled?'已开启':'未开启'}}</text></view><template v-if="store.activeAccount?.plan"><dl class="plan-glance"><dt>下次首选</dt><dd>{{seatName(planWindow.seat_ids?.[0])}}</dd><dt>下次备选</dt><dd>{{(planWindow.seat_ids||[]).slice(1).map(seatName).join(' → ')||'未添加'}}</dd><dt>下次放号</dt><dd>{{planWindow.release_date||'—'}} {{store.activeAccount.plan.trigger}}</dd><dt>实际使用</dt><dd>{{planWindow.date||'—'}}<span class="row-sub">{{planWindow.begin||'—'}}–{{planWindow.end||'—'}}</span></dd></dl><p v-if="!planWindow.available" class="inline-error">{{planWindow.message}}</p></template><view v-else class="empty compact">尚未保存预约计划</view><button class="button full" @click="go('account_info')">{{store.activeAccount?.plan?'编辑预约计划':'设置预约计划'}}</button><p class="hint">{{store.activeAccount?.plan?.enabled?'放号时自动执行。':'点击“保存并开启”启用定时。'}}</p></section>
</view>
<BookingCalendar ref="calendarRef" :accounts="store.accounts" :active-account-id="store.activeAccountId" :role="store.user?.role||'student'" :session-key="store.sessionEpoch" :seat-map="store.seatMap" :visible="calendarVisible" :offline="store.user?.auth_mode==='offline'" @adjust="openCalendarAdjust"/>
<p v-if="store.runRefreshError" class="notice" role="status">{{store.runRefreshError}}</p>
<section class="result-section"><view class="section-head"><h2>{{store.user?.role==='root'?'全部执行结果':'执行结果'}}</h2><text class="muted small">共 {{store.counts.total||0}} 条</text></view><p v-if="store.runCache?.cache_only" class="notice" role="status">离线历史仅包含已同步的 {{Math.min(store.runCache.cached_total??0,store.runCache.cache_limit||100)}} 条记录（缓存上限 {{store.runCache.cache_limit||100}} 条）；筛选和分页只针对该缓存。{{store.runCache.cache_truncated?'更早记录请实时登录后查看。':''}} 缓存更新于 {{historyCacheStamp}}。</p><p v-if="historyExpanded" class="hint">正在浏览较早记录；刷新会保留已加载页面，并优先更新打开的详情和轮流更新执行中的记录。重新筛选可回到第一页。</p><view class="filters"><select v-if="store.user?.role==='root'" class="filter" v-model="filterAccount" @change="loadRuns" aria-label="筛选账号"><option v-for="(label,i) in filterLabels" :key="i" :value="i">{{label}}</option></select><input class="filter date-filter" v-model="filterDate" placeholder="使用日期 YYYY-MM-DD" @confirm="loadRuns"/><button class="button secondary" :disabled="loading" @click="loadRuns">筛选</button></view>
<view v-if="!store.runs.length" class="empty"><AppIcon name="file-text"/><h3>还没有执行记录</h3></view><template v-else><view class="table-head"><text>账号与使用日</text><text>座位与时段</text><text>执行结果</text></view><view v-for="r in store.runs" :key="r.id" class="run-row"><view><strong>{{accountName(r.account_id)}}</strong><text class="row-sub">{{r.target_date}} · {{r.spec?.kind==='manual'?'当天手动':'定时计划'}}</text></view><view>{{(r.spec?.seat_ids||[]).map(seatName).join(' → ')}}<text class="row-sub">{{r.spec?.begin}}–{{r.spec?.end}}</text></view><view><button class="result-button" :aria-label="'查看'+r.target_date+'执行详情'" @click="detailId=r.id"><RunStatus :status="r.status"/><AppIcon name="chevron-right"/></button><text class="row-sub">{{r.message}}</text><button v-if="r.status==='uncertain'" class="text-button" :disabled="loading" @click="reconcile(r.id)">核对学校记录</button></view></view><button v-if="store.nextCursor" class="button ghost" style="margin-top:22px" @click="loadMore">加载更多</button></template></section>
<AppDialog :open="!!detail" title="执行详情" :busy="loading" @close="detailId='' "><template v-if="detail"><RunStatus :status="detail.status"/><p class="detail-message">{{detail.message}}</p><dl class="plan-glance"><dt>使用日期</dt><dd>{{detail.target_date}}</dd><dt>时段</dt><dd>{{detail.spec?.begin}}–{{detail.spec?.end}}</dd><dt>座位顺序</dt><dd>{{(detail.spec?.seat_ids||[]).map(seatName).join(' → ')}}</dd><dt>任务类型</dt><dd>{{detail.spec?.kind==='manual'?'当天手动':'定时计划'}}</dd></dl><details class="phase-details"><summary>执行阶段与请求计时</summary><view v-for="stage in ['prepare','warm','submit']" :key="stage" class="phase-line"><strong>{{phaseLabels[stage]}}</strong><text>总耗时 {{formatMs(detail.scheduler_timings?.[stage]?.elapsed_ms)}}</text><text>线程排队 {{formatMs(detail.scheduler_timings?.[stage]?.queue_ms)}}</text><text>账号锁等待 {{formatMs(detail.scheduler_timings?.[stage]?.account_lock_wait_ms)}}</text></view></details><p class="hint">成功以学校确认记录为准；缺失计时显示为无数据。</p></template><template #footer><button v-if="detail?.status==='uncertain'" class="button secondary" :disabled="loading" @click="reconcile(detail.id)">核对学校记录</button><button class="button ghost" @click="openDetailRecords">学校记录</button></template></AppDialog>
</AppShell>
</template>
<script setup>
import {ref,computed} from 'vue';import {reconcileRun} from '@/services/local_api.js';import {onShow,onHide,onUnload} from '@dcloudio/uni-app';
import SchedulerStatus from '@/components/SchedulerStatus.vue';
import BookingCalendar from '@/components/BookingCalendar.vue';
import {useWorkspace} from '@/stores/workspace.js';import AccountSelector from '@/components/AccountSelector.vue';import SessionBar from '@/components/SessionBar.vue';import RunStatus from '@/components/RunStatus.vue';
import AppShell from '@/components/AppShell.vue';import AppIcon from '@/components/AppIcon.vue';import AppDialog from '@/components/AppDialog.vue';import {nextPlanWindow,todayDefaults} from '@/services/drafts.mjs';import {formatMs} from '@/services/admin_dashboard.mjs';
const store=useWorkspace(),error=ref(''),loading=ref(false),filterAccount=ref(0),filterDate=ref(''),recordsError=ref(''),snapshotTick=ref(Date.now()),historyExpanded=ref(false);let timer=null,pageGeneration=0,refreshSequence=0,lastCalendarRefresh=-Infinity,lastSnapshotRefresh=-Infinity;
const calendarVisible=ref(true),calendarRef=ref(null);
const filterLabels=computed(()=>[store.user?.role==='root'?'全部账号':'我的账号',...store.accounts.map(a=>a.alias+' · '+a.student_id)]);
const filters=()=>({account_id:store.accounts[Number(filterAccount.value)-1]?.id||'',date:filterDate.value.trim()});
const seatName=id=>store.seatMap[id]?.name||id||'尚未选择';const accountName=id=>store.accounts.find(a=>a.id===id)?.alias||id;
const go=name=>uni.navigateTo({url:'/pages/'+name+'/'+name});
const select=id=>{store.select(id);if(!id)go('account_info');};
async function loadRuns(preserve=false){try{const keep=preserve===true;await store.loadRuns(filters(),false,keep,detailId.value);if(!keep)historyExpanded.value=false;error.value='';}catch(e){error.value=e.message;}}
async function reconcile(id){if(loading.value)return;loading.value=true;try{await reconcileRun(id);await loadRuns(true);}catch(e){error.value=e.message;}finally{loading.value=false;}}
async function loadMore(){if(loading.value)return;loading.value=true;try{await store.loadRuns({...filters(),cursor:store.nextCursor},true);historyExpanded.value=true;error.value='';}catch(e){error.value=e.code==='history_cache_changed'?'离线缓存已更新，请点击页面刷新重新读取；已显示记录保留。':e.message;}finally{loading.value=false;}}
async function refresh(refreshCalendar=true){
  if(loading.value||!calendarVisible.value)return;
  const token={page:pageGeneration,account:store.activeAccountId,session:store.sessionEpoch},ticket=++refreshSequence;
  const current=()=>calendarVisible.value&&token.page===pageGeneration&&token.account===store.activeAccountId&&token.session===store.sessionEpoch;
  loading.value=true;
  try{
    await store.refresh();if(!current())return;
    await loadRuns(historyExpanded.value||!!detailId.value);if(!current())return;
    snapshotTick.value=Date.now();
    if(store.activeAccountId&&(refreshCalendar||!store.snapshots[store.activeAccountId]||Date.now()-lastSnapshotRefresh>=15000)){
      lastSnapshotRefresh=Date.now();try{await store.records();if(current())recordsError.value='';}catch(e){if(current())recordsError.value=e.message;}
    }
    if(!current())return;
    if(refreshCalendar||Date.now()-lastCalendarRefresh>=15000){lastCalendarRefresh=Date.now();await calendarRef.value?.reload();}
  }catch(e){if(current())error.value=e.message;}finally{if(ticket===refreshSequence)loading.value=false;}
}
function stopOverview(){calendarVisible.value=false;pageGeneration++;clearInterval(timer);timer=null;store.invalidate();}
onShow(()=>{pageGeneration++;calendarVisible.value=true;loading.value=false;clearInterval(timer);refresh();timer=setInterval(()=>refresh(false),5000);});onHide(stopOverview);onUnload(stopOverview);

const phaseLabels={prepare:'提前登录',warm:'预热',submit:'提交'};
const detailId=ref(''),detail=computed(()=>store.runs.find(r=>r.id===detailId.value));
const planWindow=computed(()=>nextPlanWindow(store.activeAccount?.plan,store.policy,new Date(),store.activeAccount?.date_exceptions));
const savedRecords=computed(()=>store.snapshots[store.activeAccountId]?.records||[]);
const snapshotStamp=computed(()=>store.snapshots[store.activeAccountId]?.checked_at?.replace('T',' ').slice(0,19)||'尚未查询');
const snapshotStale=computed(()=>{const at=Date.parse(store.snapshots[store.activeAccountId]?.checked_at||'');return !Number.isFinite(at)||snapshotTick.value-at>300000;});
const historyCacheStamp=computed(()=>{const at=store.runCache?.cached_at;return Number.isFinite(at)?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).format(new Date(at*1000)):'未知';});
const confirmedRecord=computed(()=>{const day=store.policy?todayDefaults(store.policy).date:'';return savedRecords.value.filter(r=>r.status===1027&&r.begin.slice(0,10)>=day).sort((a,b)=>a.begin.localeCompare(b.begin))[0]||null;});
function openDetailRecords(){const id=detail.value?.account_id;if(id&&store.accounts.some(a=>a.id===id))store.select(id);detailId.value='';go('reservation_info');}
const goAdmin=()=>uni.reLaunch({url:'/pages/admin/admin'});
function goToday(){uni.navigateTo({url:'/pages/account_info/account_info?scope=today'});}
function openCalendarAdjust({accountId,date}){if(store.accounts.some(account=>account.id===accountId))uni.navigateTo({url:'/pages/account_info/account_info?account_id='+encodeURIComponent(accountId)+'&adjust_date='+encodeURIComponent(date)});}
</script>
<style scoped>
.overview-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:30px;margin-bottom:44px}.large-seat{font-size:36px;font-weight:650;letter-spacing:-.7px;margin-top:28px;overflow-wrap:anywhere}.reservation-time{font-size:23px;margin:8px 0}.surface-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:26px}.plan-glance{display:grid;grid-template-columns:90px 1fr;gap:18px;font-size:14px;margin:24px 0 30px}.plan-glance dt{color:var(--muted)}.plan-glance dd{margin:0;font-weight:550;overflow-wrap:anywhere}.date-filter{max-width:240px}.result-section{padding-top:12px}.result-button{display:inline-flex;align-items:center;gap:8px;padding:4px 0;min-height:44px}.result-button>.app-icon{width:16px;height:16px}.inline-error{font-size:13px;color:var(--danger);margin-bottom:20px}.detail-message{font-size:15px;line-height:1.8;margin-top:22px}.phase-details{border-top:1px solid var(--line);padding-top:18px}.phase-details summary{font-weight:600;min-height:44px;cursor:pointer}.phase-line{display:flex;flex-direction:column;gap:6px;padding:16px 0;border-top:1px solid var(--line);font-size:13px}.phase-line text{color:var(--muted)}@media(max-width:799px){.overview-grid{gap:20px}}@media(max-width:599px){.overview-grid{grid-template-columns:1fr;margin-bottom:30px}.large-seat{font-size:30px;margin-top:20px}.reservation-time{font-size:20px}.plan-glance{grid-template-columns:80px 1fr;gap:16px}.date-filter{min-width:0;flex:1}.filters .button{padding:10px 12px}}
</style>
