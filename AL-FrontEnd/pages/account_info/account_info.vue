<template>
<AppShell active="plan" :before-leave="beforeSessionLeave">
  <view class="page-heading"><view class="title-row"><h1 class="plan-page-title">预约计划</h1><text class="pill" :class="activationState.tone">{{activationState.label}}</text><text v-if="dirty()" class="draft-label">未保存</text></view><button v-if="store.user?.role==='root'" class="text-button" @click="back">返回管理后台</button></view>
  <view v-if="store.user?.role==='root'&&pageReady" class="account-context"><strong>管理账号 · {{store.activeAccount?.alias||'新增账号'}}</strong><text>{{store.activeAccount?.student_id}}</text><button class="text-button" @click="openModal('account')">账号信息</button><button class="text-button" @click="records">学校记录</button><button v-if="store.activeAccount" class="text-button" @click="showHistory">预约历史</button><details><summary>切换账号</summary><AccountSelector can-manage :accounts="store.accounts" :active="store.activeAccountId" @select="switchAccount"/></details></view>
  <view v-if="message" class="notice" :class="error?'error':'success'" role="status">{{message}}<button v-if="conflict||latestReceipt?.state==='failed'" class="text-button" @click="reload">重新载入已生效配置</button></view>
  <view v-if="pageReady&&!store.activeAccount" class="notice">请先保存学校账号。<button class="text-button" @click="openModal('account')">验证并保存账号</button></view>
  <view v-if="store.policy&&pageReady" class="plan-layout">
    <view class="editor"><view class="segmented" aria-label="预约频率"><button :class="{active:plan.mode==='day'}" :disabled="busy" @click="setBookingMode(1)">每天</button><button :class="{active:plan.mode==='week'}" :disabled="busy" @click="setBookingMode(2)">每周</button></view>
      <view v-if="!isSegmentedPlan(plan)" class="setting-row"><AppIcon name="grid"/><view class="setting-value"><text class="muted">常规首选</text><strong>{{selectedNames[0]||'尚未选择'}}</strong></view><button class="row-action" :disabled="busy" aria-label="修改首选座位" @click="openSeats('preferred')">修改<AppIcon name="chevron-right"/></button></view>
      <view v-if="!isSegmentedPlan(plan)" class="setting-row"><AppIcon name="layers"/><view class="setting-value"><text class="muted">常规备选</text><strong>{{selectedNames.slice(1).join(' → ')||'未添加'}}</strong></view><button class="row-action" :disabled="busy" aria-label="添加或排序备选座位" @click="openSeats()">{{selectedNames.length>1?'排序':'添加'}}<AppIcon name="chevron-right"/></button></view>
      <view v-if="!isSegmentedPlan(plan)" class="desktop-seat-search"><button class="seat-search-trigger" :disabled="busy" @click="openSeats()"><AppIcon name="search"/>搜索座位编号</button><button class="text-button" :disabled="busy" @click="openSeats()"><AppIcon name="plus"/>添加备选</button></view>
      <view v-if="!isSegmentedPlan(plan)" class="setting-row time-setting"><AppIcon name="clock"/><view class="setting-value"><text class="muted">时段</text><text class="time-mode-label">{{plan.mode==='week'?'每周独立时段':plan.time_mode==='full_day'?'学校全天':'自定义时段'}}</text><strong>{{plan.mode==='week'?'每周 '+Object.values(week).filter(v=>v?.[0]&&v?.[1]).length+' 天':(plan.begin||'—')+'–'+(plan.end||'—')}}</strong><text class="small-note">周五至{{store.policy.friday_close}}</text></view><button class="row-action" :disabled="busy" aria-label="修改使用时段" @click="openTime">修改<AppIcon name="chevron-right"/></button></view>
      <button v-if="!segmentAccessMessage" class="setting-row scheduled-entry" :disabled="busy||pendingSync||!store.activeAccount" @click="openScheduled"><AppIcon name="clock"/><view class="setting-value"><text class="muted">{{isSegmentedPlan(plan)?'独立时段与座位':'多个时段'}}</text><strong>{{isSegmentedPlan(plan)?scheduledSummary:'添加每天或每周时段'}}</strong></view><AppIcon name="chevron-right"/></button>
      <view v-else class="notice" role="status">{{segmentAccessMessage}}<template v-if="managedAccount&&isSegmentedPlan(plan)">当前保存的是分段计划，需恢复为普通计划后才能开启。<button class="text-button" :disabled="busy||pendingSync" @click="restoreOrdinary">恢复普通计划草稿</button></template></view>
      <view v-if="segmentRecovery" class="notice" role="status">已生成暂停的普通计划草稿，请核对时段和座位后保存。<button class="text-button" :disabled="busy" @click="undoOrdinary">撤销恢复，保留原分段草稿</button></view>
      <p v-if="managedAccount" class="hint" role="status">{{credentialSyncMessage}}</p>
      <DateExceptions v-if="store.activeAccount" v-model="dateEditor" :account="store.activeAccount" :policy="store.policy" :seats="store.seats" :session-key="store.sessionEpoch" :disabled="busy||pendingSync" :offline="store.user?.auth_mode==='offline'" @saved="exceptionsSaved" @refreshed="exceptionsRefreshed" @preferences-saved="preferencesSaved" @busy="busy=$event"/>
      <button class="setting-row advanced-row" :disabled="busy" @click="openAdvanced"><AppIcon name="sliders"/><text>高级设置</text><AppIcon name="chevron-right"/></button>
      <button class="text-button today-entry" :disabled="busy||!store.activeAccount" @click="setBookingMode(0)">{{store.user?.role==='root'?'当天预约 · 手动预检':'当天分段预约'}}<AppIcon name="chevron-right"/></button>
      <view v-if="plan.day_offset!==1" class="notice error">原计划不是次日预约，需按学校规则调整。<button class="text-button" @click="plan.day_offset=1">改为次日</button></view>
    </view>
    <aside class="plan-summary" aria-label="下次执行预览"><h2>下次执行</h2><view class="setting-row"><AppIcon name="calendar"/><view class="setting-value"><text class="muted">放号</text><strong>{{nextWindow.release_date||'—'}} <small>{{plan.trigger||'—'}}</small></strong></view></view><view class="setting-row"><AppIcon name="calendar"/><view class="setting-value"><text class="muted">使用日期</text><strong>{{nextWindow.date||'—'}}</strong><template v-if="nextWindow.segments"><text v-for="segment in nextWindow.segments" :key="segment.id" class="summary-time">{{segment.begin}}–{{segment.end}} · {{segment.seat_ids.map(seatName).join(' → ')}}</text></template><text v-else class="summary-time">{{nextWindow.begin||'—'}}–{{nextWindow.end||'—'}}</text></view></view><p v-if="!nextWindow.available" class="inline-error">{{nextWindow.message}}</p><p v-else-if="nextWindow.adjusted" class="hint">已按周五闭馆时间调整</p><view class="plan-actions"><button class="button" :disabled="busy||pendingSync||!store.activeAccount||!store.status.allow_live&&!plan.enabled" @click="latestReceipt?.state==='failed'?save():toggle()">{{busy?'保存中…':latestReceipt?.state==='failed'?'重新提交当前配置':plan.enabled?'暂停定时计划':'保存并开启定时'}}</button><button class="button ghost" :disabled="busy||pendingSync||!store.activeAccount" @click="save">仅保存</button></view><p v-if="!store.status.allow_live" class="hint">真实预约未开启，可保存和预检。</p></aside>
  </view>
  <view v-else class="empty">{{pageLoading?'正在读取预约配置…':'预约配置未能载入'}}<button v-if="!pageLoading" class="text-button" @click="showPage">重试</button></view>
  <AccountHistory v-if="store.user?.role==='root'&&pageReady&&store.activeAccount" :key="store.activeAccountId+':'+store.sessionEpoch+':'+historyRefresh" :account-id="store.activeAccountId" :session-key="store.sessionEpoch"/>
  <AppDialog :open="modal==='seats'" title="选择座位" :busy="busy" @close="closeModal"><view class="seat-selection"><view v-for="(id,i) in pendingSeats" :key="id" class="selected-seat"><strong>{{i===0?'首选':'备选'+i}} · {{seatName(id)}}</strong><view><button class="icon-button" :disabled="i===0" aria-label="上移座位" @click="movePending(i,-1)"><AppIcon name="arrow-up"/></button><button class="icon-button" :disabled="i===pendingSeats.length-1" aria-label="下移座位" @click="movePending(i,1)"><AppIcon name="arrow-down"/></button><button class="icon-button" aria-label="移除座位" @click="pendingSeats.splice(i,1)"><AppIcon name="x"/></button></view></view></view><input class="input" v-model="search" placeholder="搜索座位、分组或标签" aria-label="搜索座位"/><select class="input" v-model="seatFloor" aria-label="筛选楼层" style="margin-top:12px"><option value="all">全部楼层</option><option v-for="f in floors" :key="f" :value="f">{{f}}</option></select><p class="hint">最多5个，按顺序尝试。空位以学校为准。</p><view v-if="modalError" class="notice error">{{modalError}}</view><SeatLibrary v-if="store.activeAccount" v-model:editor="libraryEditor" :account="store.activeAccount" :seats="store.seats" :visible-seats="dialogSeats" :selected="pendingSeats" :search="search" :session-key="store.sessionEpoch" :disabled="busy" :offline="store.user?.auth_mode==='offline'" @pick="pickSeat" @apply="applySeatGroup" @saved="preferencesSaved"/><view v-else class="seat-grid"><button v-for="s in dialogSeats" :key="s.id" :class="{chosen:pendingSeats.includes(s.id)}" @click="pickSeat(s.id)"><strong>{{s.name}}</strong><text>{{s.location}}</text></button></view><template #footer><button class="button secondary" @click="closeModal">取消</button><button class="button" :disabled="busy||libraryEditor?.dirty" @click="applySeats">应用</button></template></AppDialog>
  <AppDialog :open="modal==='time'" title="使用时段" :busy="busy" @close="closeModal"><view class="segmented"><button :class="{active:timeDraft.time_mode==='full_day'}" @click="timePreset('full_day')">学校全天</button><button :class="{active:timeDraft.time_mode!=='full_day'}" @click="timePreset('custom')">自定义</button></view><view class="time-presets"><button v-for="(label,key) in {morning:'上午',afternoon:'下午',evening:'晚间'}" :key="key" class="button ghost" @click="timePreset(key)">{{label}}</button></view><template v-if="timeDraft.mode==='week'"><view v-for="(day,i) in days" :key="day" class="weekly-edit-row"><button class="weekday-toggle" :aria-pressed="!!(timeWeek[i+1]?.[0]||timeWeek[i+1]?.[1])" @click="toggleDay(i+1)">{{timeWeek[i+1]?.[0]||timeWeek[i+1]?.[1]?'✓ ':''}}{{day}}</button><input class="input" :value="timeWeek[i+1]?.[0]||''" :disabled="!timeWeek[i+1]?.[0]&&!timeWeek[i+1]?.[1]" :aria-label="day+'开始时间'" placeholder="开始" @input="setTimeWeek(i+1,0,$event.detail.value)"/><input class="input" :value="timeWeek[i+1]?.[1]||''" :disabled="!timeWeek[i+1]?.[0]&&!timeWeek[i+1]?.[1]" :aria-label="day+'结束时间'" placeholder="结束" @input="setTimeWeek(i+1,1,$event.detail.value)"/></view></template><view v-else class="fields-two" style="margin-top:28px"><view class="field"><label>开始</label><input class="input" v-model="timeDraft.begin" :disabled="timeDraft.time_mode==='full_day'" placeholder="HH:MM"/></view><view class="field"><label>结束</label><input class="input" v-model="timeDraft.end" :disabled="timeDraft.time_mode==='full_day'" placeholder="HH:MM"/></view></view><p class="hint">2–15小时 · 周五最晚20:00。每天自定义也需保证周五截短后至少2小时。</p><view v-if="modalError||timeError" class="notice error" style="margin-top:18px">{{modalError||timeError}}</view><template #footer><button class="button secondary" @click="closeModal">取消</button><button class="button" :disabled="!!timeError" @click="applyTime">应用</button></template></AppDialog>
  <AppDialog :open="modal==='advanced'" title="高级设置" @close="closeModal"><view class="field"><label>学校放号提交时间 · 北京时间</label><input class="input" v-model="triggerDraft" placeholder="07:00:00" maxlength="8" @confirm="applyAdvanced"/><p class="hint">学校07:00开放次日预约。</p></view><button class="button secondary" @click="triggerDraft=store.policy.booking_open">恢复07:00</button><view v-if="modalError" class="notice error">{{modalError}}</view><template #footer><button class="button secondary" @click="closeModal">取消</button><button class="button" @click="applyAdvanced">应用</button></template></AppDialog>
  <AppDialog :open="modal==='account'" title="学校账号" :busy="busy" @close="closeModal"><view class="field"><label>学号</label><input class="input" v-model="student" :disabled="!!store.activeAccount||busy" placeholder="请输入学号"/></view><view class="field"><label>姓名或备注</label><input class="input" v-model="alias" :disabled="busy" maxlength="80" placeholder="姓名或备注"/></view><view class="field"><label>学校统一身份认证密码</label><input class="input" v-model="password" password :disabled="busy" placeholder="留空保留原密码"/></view><p class="hint">密码仅在预约电脑加密保存。</p><view v-if="message" class="notice" :class="error?'error':'success'">{{message}}</view><button v-if="store.activeAccount" class="button ghost full" :disabled="busy" @click="check">重新检查登录</button><template #footer><button class="button secondary" :disabled="busy" @click="closeModal">关闭</button><button class="button" :disabled="busy" @click="verify">{{busy?'验证中…':'验证并保存账号'}}</button></template></AppDialog>
  <ScheduledSegments v-if="modal==='scheduled'&&pageReady&&store.activeAccount" :key="store.activeAccountId+':'+store.sessionEpoch" v-model="scheduledDraft" :account="store.activeAccount" :policy="store.policy" :seats="store.seats" :session-key="store.sessionEpoch" :disabled="busy" :blocked-reason="segmentAccessMessage" :offline="store.user?.auth_mode==='offline'" @close="closeModal" @apply="applyScheduled" @preferences-saved="preferencesSaved"/>
  <TodaySegments v-if="bookingScope==='today'&&pageReady&&store.activeAccount&&store.policy" :key="store.activeAccountId+':'+store.sessionEpoch" v-model:editor="todayEditor" :account="store.activeAccount" :revision="revision" :policy="store.policy" :seats="store.seats" :session-key="store.sessionEpoch" :offline="store.user?.auth_mode==='offline'" :allow-live="!!store.status.allow_live" :disabled="busy" @close="closeToday" @busy="busy=$event" @submitted="refreshTodayResults" @preferences-saved="preferencesSaved"/>
</AppShell>
</template>
<script setup>
import {ref,computed,watch,onMounted,onBeforeUnmount} from 'vue';
import {onShow,onHide,onUnload,onLoad} from '@dcloudio/uni-app';
import {useWorkspace} from '@/stores/workspace.js';
import * as api from '@/services/local_api.js';
import {planActivation} from '@/services/inbox_status.mjs';
import DateExceptions from '@/components/DateExceptions.vue';
import AccountHistory from '@/components/AccountHistory.vue';
import SeatLibrary from '@/components/SeatLibrary.vue';
import TodaySegments from '@/components/TodaySegments.vue';
import ScheduledSegments from '@/components/ScheduledSegments.vue';
import {isSegmentedPlan,segmentedPlan,ordinaryPlanRecovery} from '@/services/scheduled_segments.mjs';
import {calendarDateEditor} from '@/services/date_exceptions.mjs';
import {mergeLibraryAccount} from '@/services/seat_preferences.mjs';
import {createDraft,isDirty,applySavedAccount,applySavedPlan,persistableDraft,withSchoolDefaults,withWeeklyDefaults,todayDefaults,withTimePreset,nextPlanWindow,planWindowError} from '@/services/drafts.mjs';
import AccountSelector from '@/components/AccountSelector.vue';import SessionBar from '@/components/SessionBar.vue';
import AppShell from '@/components/AppShell.vue';import AppIcon from '@/components/AppIcon.vue';import AppDialog from '@/components/AppDialog.vue';
const store=useWorkspace(),student=ref(''),password=ref(''),alias=ref(''),plan=ref({}),week=ref({}),search=ref(''),busy=ref(false),message=ref(''),error=ref(false),conflict=ref(false),revision=ref(null),pageReady=ref(false),pageLoading=ref(true);
const dateEditor=ref(null);
const libraryEditor=ref(null),scheduledDraft=ref(null);
const segmentRecovery=ref(null),managedAccount=computed(()=>store.activeAccount?.capabilities?.execution_mode==='dual_node');
const segmentAccessMessage=computed(()=>managedAccount.value?'双机执行仅支持普通单段计划。':store.user?.auth_mode==='offline'&&store.activeAccount?.capabilities?.execution_mode!=='local'?'离线缓存未确认执行方式，请重新实时登录后编辑分段计划；原计划和草稿保留。':'');
const credentialSyncMessage=computed(()=>store.activeAccount?.capabilities?.credential_sync_state==='maintenance_required'?'双机凭据待维护：请联系管理员同步两台执行电脑，完成前无法保证定时执行。':store.activeAccount?.capabilities?.credential_sync_state==='matched'?'凭据版本与托管登记一致；节点是否可执行仍以放号前检查为准。':'双机凭据同步状态尚未确认，请联系管理员核对；保存密码不代表两台电脑已同步。');
const scheduledSummary=computed(()=>plan.value.mode==='week'?'每周 '+Object.keys(plan.value.week_segments||{}).length+' 天 · 各日独立时段':'每天 '+(plan.value.segments?.length||0)+' 段');
const historyRefresh=ref(0);
const latestReceipt=computed(()=>store.status.inbox?.accounts?.find(a=>a.student_id===store.user?.student_id));
const pendingSync=computed(()=>['pending','leased'].includes(latestReceipt.value?.state));let syncTicker=null,lastReceiptState='';
const activationState=computed(()=>planActivation(store.activeAccount?.plan,latestReceipt.value));
let accountBaseline='',planBaseline='',discardLeaving=false,pageGeneration=0,pageVisible=true,pageEpoch=store.sessionEpoch,viewAccountId=store.activeAccountId;
const requestContext=()=>({generation:pageGeneration,epoch:store.sessionEpoch,accountId:store.activeAccountId});
const samePage=token=>pageVisible&&token.generation===pageGeneration&&token.epoch===store.sessionEpoch;
const currentRequest=token=>samePage(token)&&token.accountId===store.activeAccountId;
const bookingScope=ref('scheduled'),todayEditor=ref(null);
const days=['周一','周二','周三','周四','周五','周六','周日'];
const draftKey=()=>store.activeAccountId||'new';
const readDraft=()=>({student:student.value,alias:alias.value,password:password.value,plan:plan.value,week:week.value,revision:revision.value,accountBaseline,planBaseline,segmentRecovery:segmentRecovery.value,todayEditor:todayEditor.value,bookingScope:bookingScope.value,editor:editorDraft(),dateEditor:dateEditor.value});
function useDraft(d){
  student.value=d.student;alias.value=d.alias;password.value=d.password;plan.value=d.plan;week.value=d.week;revision.value=d.revision;accountBaseline=d.accountBaseline;planBaseline=d.planBaseline;
  bookingScope.value=d.bookingScope||'scheduled';
  segmentRecovery.value=d.segmentRecovery||null;
  todayEditor.value=d.todayEditor?.account_id===store.activeAccountId&&d.todayEditor.date===todayDefaults(store.policy).date?d.todayEditor:null;
  restoreEditor(d.editor);
}
function load(ignoreCache=false){viewAccountId=store.activeAccountId;search.value='';const cached=store.drafts[draftKey()];const d=!ignoreCache&&cached&&(isDirty(cached)||cached.bookingScope==='today'||cached.editor?.name||cached.todayEditor?.pending)?cached:createDraft(store.activeAccount,store.policy);dateEditor.value=d.dateEditor||null;useDraft(d);}
const activeSeats=computed(()=>plan.value.seat_ids||[]);
const nextWindow=computed(()=>nextPlanWindow({...plan.value,week_times:week.value},store.policy,new Date(nowTick.value),store.activeAccount?.date_exceptions));
function exceptionsSaved(account){
  if(!pageVisible||account.id!==store.activeAccountId||account.id!==viewAccountId)return;
  const previous=store.activeAccount;if(!previous||account.revision<=previous.revision)return;
  if(revision.value===previous.revision)revision.value=account.revision;
  store.accounts=store.accounts.map(a=>a.id===account.id?account:a);
  dateEditor.value=null;
}
function exceptionsRefreshed(account){
  if(pageVisible&&account.id===store.activeAccountId&&account.id===viewAccountId&&account.revision>=store.activeAccount.revision)store.accounts=store.accounts.map(row=>row.id===account.id?account:row);
}
function preferencesSaved(result){
  if(!pageVisible||result.account_id!==store.activeAccountId||result.account_id!==viewAccountId)return;
  store.accounts=store.accounts.map(account=>account.id===result.account_id?mergeLibraryAccount(account,result):account);
}
function selectTimePreset(kind){plan.value=withTimePreset(plan.value,store.policy,kind);week.value=JSON.parse(JSON.stringify(plan.value.week_times||{}));message.value='时段已填入，请保存后生效。';error.value=false;}
function resetWeeklyTimes(){const trigger=plan.value.trigger;plan.value={...withWeeklyDefaults(plan.value,store.policy),trigger};week.value=JSON.parse(JSON.stringify(plan.value.week_times));message.value='已填入一周全天时段，周五至20:00，请保存后生效。';error.value=false;}
function setBookingMode(value){
  if(value===0){bookingScope.value='today';return;}
  bookingScope.value='scheduled';
  if(isSegmentedPlan(plan.value)){plan.value=segmentedPlan(plan.value,value===2?'week':'day');return;}
  if(value===2&&plan.value.mode!=='week'){plan.value=withWeeklyDefaults(plan.value,store.policy);week.value=JSON.parse(JSON.stringify(plan.value.week_times));}
  else if(value===1&&plan.value.mode!=='day'){plan.value=withSchoolDefaults(plan.value,store.policy);week.value={};}
}
function restoreSchoolDefaults(){plan.value=plan.value.mode==='week'?withWeeklyDefaults(plan.value,store.policy):withSchoolDefaults(plan.value,store.policy);week.value=JSON.parse(JSON.stringify(plan.value.week_times));message.value='已恢复当前预约方式的学校默认时间，请保存后生效。';error.value=false;}
const dirty=()=>isDirty(readDraft());
const choices=computed(()=>store.seats.filter(s=>(s.name+' '+s.location).toLowerCase().includes(search.value.toLowerCase())&&!activeSeats.value.includes(s.id)).slice(0,25));
function addSeat(id){if(busy.value)return;if(activeSeats.value.length>=5){message.value='最多选择 5 个座位';error.value=true;return;}activeSeats.value.push(id);search.value='';}
function removeSeat(id){if(busy.value)return;plan.value.seat_ids=plan.value.seat_ids.filter(x=>x!==id);}
function setWeek(day,side,value){const t=[...(week.value[day]||['',''])];t[side]=value;week.value[day]=t;}
function fail(e){message.value=e.message;error.value=true;conflict.value=e.status===409&&e.code==='conflict';}
async function refreshTodayResults(){
  const token=requestContext();
  try{await store.refresh();if(currentRequest(token))historyRefresh.value++;}catch(e){if(currentRequest(token))fail(e);}
}
async function verify(){
  if(busy.value)return;const token=requestContext(),sent=JSON.parse(JSON.stringify(readDraft())),oldKey=draftKey();
  if(managedAccount.value&&sent.password){const accepted=await new Promise(resolve=>uni.showModal({title:'更新双机账号密码',content:'如果学校密码已变化，本次仅更新主后台。两台执行电脑及托管登记需由管理员另行同步；完成前该账号可能无法定时预约。是否继续验证并保存？',success:r=>resolve(r.confirm)}));if(!accepted||!currentRequest(token))return;}
  const managed=managedAccount.value,oldCredential=store.activeAccount?.credential_revision;busy.value=true;message.value='';error.value=false;
  try{const r=await api.verifyAccount({student_id:sent.student,password:sent.password,alias:sent.alias,expected_revision:sent.revision});if(!currentRequest(token))return;
    const changed=oldCredential!=null&&r.account?.credential_revision!==oldCredential;
    if(changed&&store.user?.role!=='root'){await store.logout(managed?'密码已在主后台保存，请重新登录；双机凭据仍需管理员同步，完成前该账号无法定时执行。':'密码已保存，请使用当前学校密码重新登录。');uni.reLaunch({url:'/pages/login/login'});return;}
    const merged=applySavedAccount(readDraft(),r.account,sent);await store.refresh();if(!currentRequest(token))return;store.select(r.account.id);viewAccountId=r.account.id;delete store.drafts[oldKey];useDraft(merged);
    message.value=managed&&changed?'学校身份已核实，密码已在主后台保存。请同步两台执行电脑及托管登记；完成前该账号无法定时执行。':'学校身份已核实，账号已加密保存。';conflict.value=false;if(modal.value==='account')modalBaseline=modalValue();
  }catch(e){if(currentRequest(token))fail(e);}finally{if(samePage(token)){if(password.value===sent.password)password.value='';busy.value=false;}}
}
async function check(){if(busy.value||!store.activeAccount)return;const token=requestContext();busy.value=true;try{await api.checkAccount(token.accountId);if(currentRequest(token)){message.value='学校登录验证正常。';error.value=false;}}catch(e){if(currentRequest(token))fail(e);}finally{if(samePage(token))busy.value=false;}}
async function save(){
  if(busy.value||!store.activeAccount)return false;if(segmentAccessMessage.value&&isSegmentedPlan(plan.value)){fail(new Error(segmentAccessMessage.value+(managedAccount.value?'请先恢复普通计划草稿后保存':'')));return false;}const windowError=planWindowError({...plan.value,week_times:week.value},store.policy);if(windowError){fail(new Error(windowError));return false;}const token=requestContext(),sent=JSON.parse(JSON.stringify(readDraft()));busy.value=true;
  try{const weekTimes=Object.fromEntries(Object.entries(sent.week).filter(([,v])=>v[0]||v[1]));const r=await api.savePlan(token.accountId,{...sent.plan,week_times:weekTimes},sent.revision);if(!currentRequest(token))return false;
    if(r.pending){planBaseline=JSON.stringify([sent.plan,sent.week]);message.value='已保存到云端，待同步，尚未生效。';}
    else{
      useDraft(applySavedPlan(readDraft(),r.account,sent));segmentRecovery.value=null;
      store.accounts=store.accounts.map(account=>account.id===r.account.id&&account.revision<=r.account.revision?r.account:account);
      message.value='预约计划已保存。'+(store.status.allow_live?'':'真实预约执行仍未开启。');
    }
    error.value=false;conflict.value=false;
    try{await store.refresh();}catch(e){if(currentRequest(token)){message.value+='最新状态刷新失败：'+e.message+'，可稍后刷新核对。';error.value=true;}}
    return true;
  }catch(e){if(currentRequest(token))fail(e);return false;}finally{if(samePage(token))busy.value=false;}
}
async function toggle(){if(busy.value)return;const token=requestContext(),old=plan.value.enabled;const confirmed=await new Promise(resolve=>uni.showModal({title:old?'暂停未来定时计划？':'保存并开启定时？',content:old?'学校已有预约和历史保留。':`${nextWindow.value.release_date||'—'} ${plan.value.trigger}放号；使用 ${nextWindow.value.date||'—'} ${nextWindow.value.segments?.map(s=>s.begin+'–'+s.end+' '+s.seat_ids.map(seatName).join(' → ')).join('；')||(nextWindow.value.begin||'—')+'–'+(nextWindow.value.end||'—')}；${(nextWindow.value.seat_ids||[]).map(seatName).join(' → ')}。学校回执决定是否成功。`,confirmText:old?'暂停计划':'保存并开启',success:r=>resolve(r.confirm)}));if(!confirmed||!currentRequest(token))return;plan.value.enabled=!old;if(!await save()&&currentRequest(token))plan.value.enabled=old;}
function confirmDiscard(){return new Promise(resolve=>uni.showModal({title:'有未保存的修改',content:'是否放弃当前修改？',confirmText:'放弃修改',cancelText:'继续编辑',success:r=>resolve(r.confirm)}));}
async function switchAccount(id){if(busy.value)return;if(dirty()&&!await confirmDiscard())return;retainPendingToday();store.select(id);load();message.value='';conflict.value=false;}
async function reload(){
  if(dirty()&&!await confirmDiscard())return;const token=requestContext(),pending=todayEditor.value?.pending?JSON.parse(JSON.stringify(todayEditor.value)):null;
  delete store.drafts[draftKey()];await store.refresh();if(!currentRequest(token))return;load(true);
  if(pending?.account_id===store.activeAccountId&&pending.date===todayDefaults(store.policy).date)todayEditor.value={...pending,dirty:false};
  message.value='已重新载入最新配置';error.value=false;conflict.value=false;
}
async function beforeSessionLeave(){if(busy.value)return false;if(dirty()&&!await confirmDiscard())return false;discardLeaving=true;retainPendingToday();return true;}
async function leave(url){if(await beforeSessionLeave())uni.reLaunch({url});}
const back=()=>leave(store.user?.role==='root'?'/pages/admin/admin':'/pages/index/index'),records=()=>leave('/pages/reservation_info/reservation_info');
const showHistory=()=>uni.pageScrollTo({selector:'#account-history',duration:250});
onLoad(query=>{requestedToday=query.scope==='today';requestedAccount=query.account_id||'';requestedNew=query.new_account==='1';requestedAdjustDate=query.adjust_date||'';});
function openRequestedDate(){
  const date=requestedAdjustDate;requestedAdjustDate='';if(!date)return;
  if(requestedAccount&&requestedAccount!==store.activeAccountId){fail(new Error('这条日期链接不属于当前账号'));return;}
  if(store.user?.auth_mode==='offline'||pendingSync.value){message.value='日期调整需要执行电脑在线且配置已同步，请稍后重新打开日历。';error.value=false;return;}
  if(dirty()||modal.value||dateEditor.value||bookingScope.value==='today'){message.value='已保留当前编辑，请先处理已有草稿，再从日历打开日期调整。';error.value=false;return;}
  try{dateEditor.value=calendarDateEditor(date,store.activeAccount,store.policy,new Date());message.value='已填入 '+date+'，核对时段和座位后再保存本次调整。';error.value=false;}catch(e){fail(e);}
}
async function showPage(){pageReady.value=false;pageLoading.value=true;pageVisible=true;pageGeneration++;pageEpoch=store.sessionEpoch;const token=requestContext();discardLeaving=false;clearInterval(ticker);try{await store.refresh();if(!samePage(token))return;if(requestedNew){store.select('');requestedNew=false;}else if(requestedAccount&&store.accounts.some(a=>a.id===requestedAccount))store.select(requestedAccount);load();if(requestedToday){setBookingMode(0);requestedToday=false;}openRequestedDate();ticker=setInterval(()=>nowTick.value=Date.now(),1000);pageReady.value=true;}catch(e){if(samePage(token))fail(e);}finally{if(samePage(token))pageLoading.value=false;}}
async function pollInbox(){if(!pageVisible||busy.value||store.user?.auth_mode!=='offline')return;const token=requestContext();try{const status=await api.getStatus();if(!currentRequest(token))return;store.status=status;const receipt=latestReceipt.value;if(receipt?.state==='applied'&&lastReceiptState!=='applied'){const clean=!dirty();await store.refresh();if(!currentRequest(token))return;if(clean)load(true);message.value='配置已生效。';error.value=false;}else if(receipt?.state==='failed'){message.value='同步失败，原配置不变。'+(receipt.message||'请核对后重新保存。');error.value=true;}lastReceiptState=receipt?.state||'';}catch(e){if(currentRequest(token))fail(e);}}
onShow(async()=>{clearInterval(syncTicker);await showPage();if(pageVisible&&store.user?.auth_mode==='offline')syncTicker=setInterval(pollInbox,5000);});
function hidePage(){clearInterval(syncTicker);if(!pageVisible)return;clearInterval(ticker);if(pageReady.value&&store.user&&!discardLeaving&&pageEpoch===store.sessionEpoch&&viewAccountId===store.activeAccountId)store.drafts[draftKey()]=persistableDraft(readDraft());pageReady.value=false;pageVisible=false;pageGeneration++;busy.value=false;modal.value='';bookingScope.value='scheduled';password.value='';store.invalidate();}
onHide(hidePage);onUnload(hidePage);

const modal=ref(''),modalError=ref(''),pendingSeats=ref([]),seatFloor=ref('all'),seatMode=ref('all'),timeDraft=ref({}),timeWeek=ref({}),triggerDraft=ref(''),nowTick=ref(new Date().getTime());
let requestedToday=false,requestedNew=false,requestedAccount='',requestedAdjustDate='',ticker=null,modalBaseline='';
const seatName=id=>store.seatMap[id]?.name||id||'尚未选择';
const selectedNames=computed(()=>(plan.value.seat_ids||[]).map(seatName));
const floors=computed(()=>[...new Set(store.seats.map(s=>s.location))]);
const dialogSeats=computed(()=>store.seats.filter(s=>(seatFloor.value==='all'||s.location===seatFloor.value)&&(s.name+' '+s.location).toLowerCase().includes(search.value.trim().toLowerCase())).slice(0,80));
const timeError=computed(()=>planWindowError({...timeDraft.value,week_times:timeWeek.value},store.policy));
const modalValue=()=>JSON.stringify(modal.value==='scheduled'?[scheduledDraft.value?.plan,!!scheduledDraft.value?.library_editor?.dirty]:modal.value==='seats'?pendingSeats.value:modal.value==='time'?[timeDraft.value,timeWeek.value]:modal.value==='advanced'?triggerDraft.value:modal.value==='account'?[student.value,alias.value,password.value]:'');
function editorDraft(){
  if(!modal.value)return null;
  let value={},baseline=modalBaseline;
  if(modal.value==='account')baseline=JSON.stringify([...JSON.parse(modalBaseline).slice(0,2),'']);
  if(modal.value==='scheduled')value=scheduledDraft.value;
  if(modal.value==='seats')value={seats:[...pendingSeats.value],mode:seatMode.value,search:search.value,floor:seatFloor.value,libraryEditor:libraryEditor.value};
  if(modal.value==='time')value={plan:timeDraft.value,week:timeWeek.value};
  if(modal.value==='advanced')value={trigger:triggerDraft.value};
  return {name:modal.value,baseline,value,dirty:modalValue()!==modalBaseline||(modal.value==='seats'&&!!libraryEditor.value?.dirty)||(modal.value==='scheduled'&&!!scheduledDraft.value?.library_editor?.dirty)};
}
function restoreEditor(editor){
  modalError.value='';modal.value='';libraryEditor.value=null;
  if(!editor||!['account','seats','time','advanced','scheduled'].includes(editor.name))return;
  const value=JSON.parse(JSON.stringify(editor.value));
  if(editor.name==='seats'){pendingSeats.value=value.seats;seatMode.value=value.mode;search.value=value.search;seatFloor.value=value.floor;libraryEditor.value=value.libraryEditor?.account_id===store.activeAccountId?value.libraryEditor:null;}
  if(editor.name==='time'){timeDraft.value=value.plan;timeWeek.value=value.week;}
  if(editor.name==='advanced')triggerDraft.value=value.trigger;
  if(editor.name==='scheduled')scheduledDraft.value=value;
  modalBaseline=editor.baseline;modal.value=editor.name;
}
function openModal(name){if(busy.value)return;modalError.value='';modal.value=name;modalBaseline=modalValue();}
function openSeats(mode='all'){pendingSeats.value=[...activeSeats.value];seatMode.value=mode;search.value='';seatFloor.value='all';libraryEditor.value=null;openModal('seats');}
function applySeatGroup(ids){if(busy.value)return;pendingSeats.value=[...ids];modalError.value='';}
function pickSeat(id){
  modalError.value='';
  if(seatMode.value==='preferred'){pendingSeats.value=[id,...pendingSeats.value.slice(1).filter(s=>s!==id)];return;}
  if(pendingSeats.value.includes(id)){pendingSeats.value=pendingSeats.value.filter(s=>s!==id);return;}
  if(pendingSeats.value.length===5){modalError.value='最多选择5个座位';return;}pendingSeats.value.push(id);
}
function movePending(index,delta){const next=index+delta;if(next<0||next>=pendingSeats.value.length)return;const copy=[...pendingSeats.value];[copy[index],copy[next]]=[copy[next],copy[index]];pendingSeats.value=copy;}
function applySeats(){if(busy.value)return;if(libraryEditor.value?.dirty){modalError.value='请先保存或取消收藏编辑';return;}if(!pendingSeats.value.length){modalError.value='请选择至少一个座位';return;}plan.value.seat_ids=[...pendingSeats.value];modal.value='';libraryEditor.value=null;}
function openScheduled(){if(busy.value)return;if(segmentAccessMessage.value){message.value=segmentAccessMessage.value;return;}scheduledDraft.value={plan:segmentedPlan(plan.value),day:'1',seat_index:null,library_editor:null,search:''};openModal('scheduled');}
function applyScheduled(value){if(busy.value||segmentAccessMessage.value)return;plan.value=JSON.parse(JSON.stringify(value));modal.value='';scheduledDraft.value=null;}
async function restoreOrdinary(){
  if(busy.value||pendingSync.value||!managedAccount.value||!isSegmentedPlan(plan.value))return;
  const token=requestContext(),original=JSON.parse(JSON.stringify({plan:plan.value,week:week.value}));let recovery;
  try{recovery=ordinaryPlanRecovery(original.plan);}catch(e){fail(e);return;}
  const intervals=recovery.intervals.map(row=>(row.day==='daily'?'每天':days[Number(row.day)-1])+' '+row.begin+'–'+row.end).join('；');
  const accepted=await new Promise(resolve=>uni.showModal({title:'恢复普通单段计划草稿？',content:intervals+'。统一座位：'+recovery.plan.seat_ids.map(seatName).join(' → ')+'。将移除 '+recovery.removed+' 个额外时段'+(recovery.seatsChanged?'，其他使用日的座位改为上述顺序':'')+'；计划保持暂停，日期调整保留。仅修改草稿，可撤销；核对并保存后才生效。',confirmText:'生成草稿',cancelText:'保留分段',success:r=>resolve(r.confirm)}));
  if(!accepted||!currentRequest(token)||JSON.stringify({plan:plan.value,week:week.value})!==JSON.stringify(original))return;
  segmentRecovery.value=original;plan.value=recovery.plan;week.value=JSON.parse(JSON.stringify(recovery.plan.week_times));message.value='已生成普通计划草稿，尚未保存。';error.value=false;
}
async function undoOrdinary(){
  if(busy.value||!segmentRecovery.value)return;const token=requestContext(),original=segmentRecovery.value;
  const accepted=await new Promise(resolve=>uni.showModal({title:'恢复原分段草稿？',content:'这会放弃生成普通计划后所做的时段、座位等草稿修改，恢复转换前的分段草稿。正式配置保持不变。',confirmText:'恢复分段草稿',cancelText:'保留当前编辑',success:r=>resolve(r.confirm)}));
  if(!accepted||!currentRequest(token)||segmentRecovery.value!==original)return;
  plan.value=original.plan;week.value=original.week;segmentRecovery.value=null;message.value='已恢复原分段草稿，正式配置未改变。';error.value=false;
}
function openTime(){timeDraft.value=JSON.parse(JSON.stringify(plan.value));timeWeek.value=JSON.parse(JSON.stringify(week.value));openModal('time');}
function timePreset(kind){const trigger=timeDraft.value.trigger;if(kind==='full_day'&&timeDraft.value.mode==='week')timeDraft.value=withWeeklyDefaults(timeDraft.value,store.policy);else timeDraft.value=withTimePreset(timeDraft.value,store.policy,kind);timeDraft.value.trigger=trigger;timeWeek.value=JSON.parse(JSON.stringify(timeDraft.value.week_times||{}));}
function setTimeWeek(day,side,value){const row=[...(timeWeek.value[day]||['',''])];row[side]=value;timeWeek.value[day]=row;}
function toggleDay(day){timeWeek.value[day]=timeWeek.value[day]?.some(Boolean)?['','']:[store.policy.open,day===5?store.policy.friday_close:store.policy.close];}
function applyTime(){if(timeError.value){modalError.value=timeError.value;return;}plan.value={...timeDraft.value,week_times:JSON.parse(JSON.stringify(timeWeek.value))};week.value=JSON.parse(JSON.stringify(timeWeek.value));modal.value='';}
function openAdvanced(){triggerDraft.value=plan.value.trigger||store.policy?.booking_open||'07:00:00';openModal('advanced');}
function applyAdvanced(){if(!/^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(triggerDraft.value)){modalError.value='请填写 HH:MM:SS';return;}plan.value.trigger=triggerDraft.value;modal.value='';}
async function closeModal(){if(busy.value)return;const token=requestContext(),changed=modalValue()!==modalBaseline||(modal.value==='seats'&&!!libraryEditor.value?.dirty)||(modal.value==='scheduled'&&!!scheduledDraft.value?.library_editor?.dirty);if(changed&&!await confirmDiscard())return;if(!currentRequest(token))return;if(changed&&modal.value==='account'){const original=JSON.parse(modalBaseline);student.value=original[0];alias.value=original[1];}modal.value='';modalError.value='';password.value='';libraryEditor.value=null;}
function retainPendingToday(){
  const key=draftKey();
  if(todayEditor.value?.pending)store.drafts[key]={...createDraft(store.activeAccount,store.policy),todayEditor:JSON.parse(JSON.stringify(todayEditor.value)),bookingScope:'scheduled'};
  else delete store.drafts[key];
}
async function closeToday(){
  if(busy.value)return;const token=requestContext();
  if(todayEditor.value?.dirty&&!await confirmDiscard())return;
  if(!currentRequest(token))return;
  if(todayEditor.value?.pending)todayEditor.value={...todayEditor.value,dirty:false,baseline:JSON.stringify({date:todayEditor.value.date,segments:todayEditor.value.segments}),seat_index:null,library_editor:null};
  else todayEditor.value=null;
  bookingScope.value='scheduled';
}
function browserLeave(event){if(busy.value||dirty()||modal.value&&modalValue()!==modalBaseline){event.preventDefault();event.returnValue='';}}
onMounted(()=>{if(typeof window!=='undefined')window.addEventListener('beforeunload',browserLeave);});
onBeforeUnmount(()=>{hidePage();clearInterval(ticker);if(typeof window!=='undefined')window.removeEventListener('beforeunload',browserLeave);});
</script>
<style scoped>
.scheduled-entry{color:var(--ink);text-align:left}.scheduled-entry>.app-icon:last-child{margin-left:auto}.draft-label{font-size:12px;color:var(--amber)}.plan-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:40px;align-items:start}.editor{min-width:0}.editor>.segmented{margin-bottom:14px}.setting-row{display:flex;align-items:center;gap:20px;width:100%;padding:25px 0;border-bottom:1px solid var(--line);min-height:100px;text-align:left}.setting-row>:deep(.app-icon){width:27px;height:27px}.setting-value{display:flex;flex-direction:column;gap:4px;flex:1;min-width:0;overflow-wrap:anywhere}.setting-value strong{font-size:20px;line-height:1.5;font-weight:650;letter-spacing:-.3px}.row-action{display:flex;align-items:center;gap:10px;justify-content:flex-end;min-height:44px;flex:none;font-size:14px;color:var(--green);padding:5px}.row-action :deep(.app-icon){width:18px;height:18px}.desktop-seat-search{display:flex;align-items:center;gap:12px;padding:20px 0 24px 47px;border-bottom:1px solid var(--line)}.seat-search-trigger{display:flex;gap:10px;align-items:center;flex:1;min-width:0;background:white;border:1px solid var(--line);border-radius:7px;color:var(--muted);font-size:14px;padding:12px;min-height:46px}.time-setting{min-height:164px}.time-mode-label{font-size:13px;color:var(--green);margin:5px 0}.small-note{font-size:12px;align-self:flex-start;padding:4px 7px;background:#e9efea;border-radius:5px;color:var(--muted)}.advanced-row{min-height:88px;font-size:16px}.advanced-row>.app-icon:last-child{margin-left:auto}.today-entry{margin-top:16px}.plan-summary{border-left:1px solid var(--line);padding:12px 0 0 32px;min-height:500px;position:sticky;top:24px}.plan-summary>h2{font-size:21px;margin-bottom:14px}.plan-summary .setting-row{align-items:flex-start;gap:16px;min-height:115px}.plan-summary strong{font-size:18px}.plan-summary small{font-size:15px;display:inline-block;margin-top:5px}.summary-time{font-size:17px;color:var(--muted)}.plan-actions{display:flex;flex-direction:column;gap:12px;margin-top:28px}.plan-actions .button{min-height:50px;width:100%}.inline-error{color:var(--danger);font-size:13px;line-height:1.7;margin-top:12px}.selected-seat{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}.selected-seat>view{display:flex;flex:none}.seat-selection{margin-bottom:20px}.seat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:20px}.seat-grid>button{padding:12px 6px;border:1px solid var(--line);border-radius:8px;min-height:64px;background:white;text-align:left}.seat-grid>button.chosen{border-color:var(--green);background:var(--tint)}.seat-grid strong,.seat-grid text{display:block;font-size:13px;overflow-wrap:anywhere}.seat-grid text{font-size:11px;color:var(--muted)}.time-presets{display:flex;gap:8px;margin:20px 0}.time-presets .button{flex:1;padding:10px}.weekly-edit-row{display:grid;grid-template-columns:70px 1fr 1fr;gap:8px;align-items:center;margin:12px 0}.weekday-toggle{min-height:46px;font-size:14px;text-align:left}.account-context details{width:100%}.account-context summary{cursor:pointer;min-height:44px;line-height:44px}@media(min-width:600px) and (max-width:999px){.plan-layout{grid-template-columns:1fr;gap:28px}.plan-summary{position:static;border-left:0;border-top:1px solid var(--line);padding:24px 0 0;min-height:0;display:grid;grid-template-columns:1fr 1fr;gap:20px}.plan-summary>h2{grid-column:1/-1}.plan-summary .setting-row{padding:8px 0;border-bottom:0}.plan-actions{grid-column:1/-1;flex-direction:row;margin-top:8px}.plan-actions .button{flex:1}}@media(max-width:599px){.page-heading{margin-bottom:14px}.plan-layout{display:block}.setting-row{gap:16px;min-height:94px;padding:20px 0}.setting-value strong{font-size:19px}.desktop-seat-search{display:none}.time-setting{min-height:124px}.time-mode-label{font-size:12px}.plan-summary{position:static;border:0;padding:0;min-height:0;display:contents}.plan-summary>h2{display:none}.plan-summary .setting-row{padding:18px 0;min-height:86px}.plan-summary strong{font-size:17px}.plan-summary .summary-time{font-size:14px}.plan-summary .plan-actions{position:sticky;z-index:10;bottom:70px;background:var(--wash);padding:14px 0 8px;border-top:1px solid var(--line);margin-top:20px}.today-entry{margin:10px 0}.advanced-row{min-height:76px}.seat-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.selected-seat strong{font-size:13px}.selected-seat .icon-button{width:44px;min-width:44px}.weekly-edit-row{grid-template-columns:62px minmax(0,1fr) minmax(0,1fr);gap:6px}.weekly-edit-row .input{padding:8px}.draft-label{display:none}}@media(prefers-reduced-motion:reduce){*{transition:none!important}}
@media(max-width:599px){
.page-heading{min-height:30px;margin-bottom:10px}.plan-page-title{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}.plan-layout{display:flex;flex-direction:column;gap:0}.editor,.plan-summary{display:contents}.editor>.segmented{order:0;margin-bottom:8px}.editor>.setting-row{order:1}.editor>.time-setting{order:3;min-height:96px}.plan-summary>.setting-row{order:4;min-height:70px;padding:8px 0}.editor>.advanced-row{order:5;min-height:52px}.editor>.today-entry,.editor>.notice{order:6}.plan-summary>.plan-actions{order:7}.plan-summary>.hint,.plan-summary>.inline-error{order:8}.setting-row{padding:8px 0;min-height:72px}.time-mode-label{display:none}.plan-summary .setting-value{display:grid;grid-template-columns:auto 1fr;gap:4px 7px;align-items:baseline}.plan-summary .setting-value>.muted{grid-column:1/-1}.plan-summary .setting-value strong{font-size:16px}.plan-summary small{font-size:14px;margin-top:0}.plan-summary .summary-time{font-size:12px;margin:0}.plan-summary .plan-actions{margin-top:16px;padding:10px 0 8px;gap:8px}.plan-actions{width:100%}.plan-actions .button{min-height:46px}
}
</style>
