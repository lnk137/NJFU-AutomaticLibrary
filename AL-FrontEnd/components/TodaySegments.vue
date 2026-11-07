<template>
<AppDialog :open="true" :title="seatIndex===null?'当天分段预约':'选择座位 · 时段 '+(seatIndex+1)" :busy="requestBusy" @close="requestClose">
  <template v-if="seatIndex!==null">
    <view class="selected-list">
      <view v-for="(id,index) in pendingSeats" :key="id" class="selected-item">
        <strong>{{index?'备选 '+index:'首选'}} · {{seatName(id)}}</strong>
        <view class="seat-tools">
          <button class="icon-button" :disabled="index===0" aria-label="上移座位" @click="moveSeat(index,-1)"><AppIcon name="arrow-up"/></button>
          <button class="icon-button" :disabled="index===pendingSeats.length-1" aria-label="下移座位" @click="moveSeat(index,1)"><AppIcon name="arrow-down"/></button>
          <button class="icon-button" aria-label="移除座位" @click="pendingSeats.splice(index,1);saveEditor()"><AppIcon name="x"/></button>
        </view>
      </view>
    </view>
    <input class="input" v-model="search" placeholder="搜索座位、分组或标签" aria-label="搜索座位"/>
    <select class="input floor-filter" v-model="floor" aria-label="筛选楼层"><option value="all">全部楼层</option><option v-for="name in floors" :key="name" :value="name">{{name}}</option></select>
    <p class="hint">每段最多 5 个座位，按首选、备选顺序尝试。</p>
    <view v-if="state.error" class="notice error" role="status">{{state.error}}</view>
    <view v-if="controller.needsReconcile()" class="notice">仍有时段待核实，请先核对学校记录；本次选座草稿已保留。</view>
    <view v-if="seatError" class="notice error" role="status">{{seatError}}</view>
    <SeatLibrary v-model:editor="libraryEditor" :account="account" :seats="seats" :visible-seats="visibleSeats" :selected="pendingSeats" :search="search" :session-key="sessionKey" :disabled="requestBusy" :offline="offline" @pick="pickSeat" @apply="applyGroup" @saved="emit('preferences-saved',$event)"/>
  </template>
  <template v-else>
    <view class="today-heading"><strong>{{state.draft.date}}</strong><text class="muted">原定时计划保留</text></view>
    <view v-if="offline" class="notice error">请重新实时登录后使用当天预约。</view>
    <view v-else-if="!allowLive" class="notice">真实预约未开启，可以检查时段。</view>
    <view v-if="state.error" class="notice error" role="status">{{state.error}}</view>
    <view v-if="!bookingAvailability.available" class="notice" role="status">{{bookingAvailability.message}}</view>
    <template v-if="state.confirmedId">
      <view class="batch-heading"><strong>{{batchTitle}}</strong><button class="text-button" :disabled="requestBusy||offline||disabled" @click="controller.poll()">刷新本地进度</button></view>
      <p v-if="state.batch?.message" class="hint">{{state.batch.message}}</p>
      <article v-for="(row,index) in resultRows" :key="index" class="interval-card result-card">
        <view class="card-heading"><strong>{{row.begin}}–{{row.end}}</strong><text class="pill" :class="statusTone(row.status)">{{statusName(row.status)}}</text></view>
        <p class="seat-summary">{{resultSeat(row)||names(row.seat_ids)}}</p>
        <p v-if="row.message||row.result?.message" class="result-message">{{row.message||row.result.message}}</p>
      </article>
      <p v-if="state.batch?.state==='stopped'" class="hint">已成功的预约保留，未提交的时段不会自动补交。</p>
      <p v-if="controller.needsReconcile()" class="notice">仍有时段待核实，请先核对学校记录，再预约其他时段。</p>
      <p v-if="controller.editor().dirty" class="notice">另有一份未提交的时段草稿，已保留时间和座位，可继续编辑。</p>
    </template>
    <template v-else-if="state.preview">
      <view class="preview-heading"><strong>请核对全部时段</strong><text class="pill" :class="previewExpired?'red':'green'">{{previewExpired?'预检已过期':'检查通过'}}</text></view>
      <article v-for="(row,index) in state.preview.segments" :key="index" class="interval-card">
        <strong>{{row.begin}}–{{row.end}}</strong><p class="seat-summary">{{(row.seats||[]).map(s=>s.name||s.id).join(' → ')}}</p>
      </article>
      <p class="confirm-impact">确认后依次预约以上 {{state.preview.segments.length}} 段。每段分别显示结果，结果待核实时停止后续提交。</p>
    </template>
    <template v-else>
      <article v-for="(row,index) in state.draft.segments" :key="index" class="interval-card">
        <view class="card-heading"><strong>时段 {{index+1}}</strong><button v-if="state.draft.segments.length>1" class="text-button" :disabled="editingDisabled" :aria-label="'移除时段 '+(index+1)" @click="removeSegment(index)">移除</button></view>
        <view class="fields-two segment-times">
          <view class="field"><label>开始</label><input class="input" :value="row.begin" :aria-label="'第 '+(index+1)+' 段开始'" :disabled="editingDisabled" placeholder="HH:MM" maxlength="5" @input="changeTime(index,'begin',$event.detail?.value??$event.target?.value)"/></view>
          <view class="field"><label>结束</label><input class="input" :value="row.end" :aria-label="'第 '+(index+1)+' 段结束'" :disabled="editingDisabled" placeholder="HH:MM" maxlength="5" @input="changeTime(index,'end',$event.detail?.value??$event.target?.value)"/></view>
        </view>
        <button class="segment-seat" :disabled="editingDisabled" :aria-label="'选择时段 '+(index+1)+' 的座位'" @click="openSeats(index)"><view><text class="muted">首选及备选</text><strong>{{names(row.seat_ids)||'请选择座位'}}</strong></view><AppIcon name="chevron-right"/></button>
      </article>
      <button class="button ghost full add-segment" :disabled="editingDisabled||state.draft.segments.length>=7" @click="addSegment"><AppIcon name="plus"/>添加时段</button>
      <details class="context-help"><summary>使用规则</summary><p>时段可首尾相接，不能重叠。每段至少 2 小时，选择尚未开始的整点或半点；周五最晚 20:00。检查通过后确认一次，再依次预约。</p></details>
    </template>
  </template>
  <template #footer>
    <template v-if="seatIndex!==null"><button class="button secondary" :disabled="requestBusy" @click="closeSeats">取消</button><button v-if="controller.needsReconcile()" class="button" :disabled="requestBusy||offline||disabled||controller.active()" @click="controller.reconcile()">{{state.reconciling?'核对中…':'核对学校记录'}}</button><button v-else class="button" :disabled="requestBusy||disabled||!!libraryEditor?.dirty" @click="applySeats">应用座位</button></template>
    <template v-else-if="state.confirmedId"><button class="button secondary" :disabled="requestBusy" @click="requestClose">关闭</button><button v-if="controller.needsReconcile()" class="button" :disabled="requestBusy||offline||disabled||controller.active()" @click="controller.reconcile()">{{state.reconciling?'核对中…':'核对学校记录'}}</button><button v-else-if="controller.active()" class="button" :disabled="requestBusy||offline||disabled" @click="controller.poll()">刷新本地进度</button><button v-else class="button" :disabled="requestBusy||disabled||!bookingAvailability.available" @click="controller.startNew()">{{controller.editor().dirty?'继续编辑草稿':'再预约其他时段'}}</button></template>
    <template v-else-if="state.preview"><button class="button secondary" :disabled="requestBusy" @click="controller.setDraft(state.draft)">返回修改</button><button class="button" :disabled="requestBusy||disabled||previewExpired||!allowLive||offline||!bookingAvailability.available" @click="confirmBatch">确认提交这 {{state.preview.segments.length}} 段预约</button></template>
    <template v-else><button class="button secondary" :disabled="requestBusy" @click="requestClose">关闭</button><button class="button" :disabled="requestBusy||disabled||offline||!bookingAvailability.available" @click="controller.prepare()">{{state.loading?'检查中…':'检查全部时段'}}</button></template>
  </template>
</AppDialog>
</template>

<script setup>
import {ref,reactive,computed,watch,onMounted,onBeforeUnmount} from 'vue';
import AppDialog from '@/components/AppDialog.vue';
import AppIcon from '@/components/AppIcon.vue';
import SeatLibrary from '@/components/SeatLibrary.vue';
import * as api from '@/services/local_api.js';
import {createTodayBatchController,appendTodaySegment,freshTodaySegments} from '@/services/today_segments.mjs';

const props=defineProps({account:{type:Object,required:true},revision:Number,policy:{type:Object,required:true},seats:{type:Array,default:()=>[]},editor:{type:Object,default:null},sessionKey:Number,offline:Boolean,allowLive:Boolean,disabled:Boolean});
const emit=defineEmits(['update:editor','close','busy','submitted','preferences-saved']);
const state=reactive({}),seatIndex=ref(null),pendingSeats=ref([]),search=ref(''),floor=ref('all'),libraryEditor=ref(null),seatError=ref(''),tick=ref(Date.now());
let seatBaseline='',timer=null,mounted=false;
const seatDraftDirty=()=>seatIndex.value!==null&&(JSON.stringify(pendingSeats.value)!==seatBaseline||!!libraryEditor.value?.dirty);
function saveEditor(editor){
  const value=editor||controller.editor();
  emit('update:editor',{...value,dirty:value.dirty||seatDraftDirty(),seat_index:seatIndex.value,pending_seats:[...pendingSeats.value],seat_baseline:seatBaseline,search:search.value,floor:floor.value,library_editor:libraryEditor.value});
}
function restoreSeats(saved){
  const own=saved?.account_id===props.account.id&&saved.date===freshTodaySegments(props.policy,[],Date.now()).date&&Number.isInteger(saved.seat_index)&&saved.seat_index>=0&&saved.seat_index<(saved.segments?.length||0);
  seatIndex.value=own?saved.seat_index:null;pendingSeats.value=own?[...(saved.pending_seats||[])]:[];
  seatBaseline=own?saved.seat_baseline||JSON.stringify(pendingSeats.value):'';
  search.value=own?saved.search||'':'';floor.value=own?saved.floor||'all':'all';
  libraryEditor.value=own?saved.library_editor||null:null;seatError.value='';
}
restoreSeats(props.editor);
const controller=createTodayBatchController({state,context:()=>({accountId:props.account.id,revision:props.revision,sessionKey:props.sessionKey,allowLive:props.allowLive,offline:props.offline,disabled:props.disabled}),policy:()=>props.policy,seatIds:()=>props.account.plan?.seat_ids||[],api,initialEditor:props.editor,onDraft:saveEditor,onResult:result=>emit('submitted',result)});
const requestBusy=computed(()=>state.loading||state.submitting||state.restoring||state.reconciling);
const bookingAvailability=computed(()=>controller.availability(tick.value));
const editingDisabled=computed(()=>controller.locked()||props.disabled||!bookingAvailability.value.available);
const previewExpired=computed(()=>!!state.preview&&tick.value>=Date.parse(state.preview.expires_at));
const floors=computed(()=>[...new Set(props.seats.map(s=>s.location).filter(Boolean))]);
const visibleSeats=computed(()=>props.seats.filter(s=>(floor.value==='all'||s.location===floor.value)&&(s.name+' '+s.location).toLowerCase().includes(search.value.trim().toLowerCase())).slice(0,80));
const names=ids=>(ids||[]).map(seatName).join(' → ');
function seatName(id){return props.seats.find(s=>s.id===id)?.name||id;}
const resultRows=computed(()=>state.batch?.segments||state.draft.segments.map(row=>({...row,status:'pending'})));
const batchTitle=computed(()=>state.confirmationUnknown?'正在核对确认结果':state.batch?.state==='completed'?'本次预约结果':state.batch?.state==='stopped'?'本次已停止':'正在依次预约');
const statusName=status=>({pending:'等待执行',running:'正在预约',success:'已预约',failed:'未成功',uncertain:'待核实',skipped:'已停止',not_submitted:'未提交'}[status]||'待核对');
const statusTone=status=>status==='success'?'green':status==='failed'?'red':['uncertain','skipped','not_submitted'].includes(status)?'amber':'';
function resultSeat(row){
  const record=(row.result?.records||[]).find(r=>r.begin===`${state.draft.date} ${row.begin}:00`&&r.end===`${state.draft.date} ${row.end}:00`&&row.seat_ids?.includes(r.seat_id)&&r.status===1027);
  return row.status==='success'&&record?'已预约 · '+seatName(record.seat_id):'';
}
function changeTime(index,key,value){if(editingDisabled.value)return;const draft=JSON.parse(JSON.stringify(state.draft));draft.segments[index][key]=value;controller.setDraft(draft);}
function addSegment(){if(editingDisabled.value)return;try{controller.setDraft(appendTodaySegment(state.draft,props.policy));}catch(error){state.error=error.message;}}
function removeSegment(index){if(editingDisabled.value||state.draft.segments.length===1)return;const draft=JSON.parse(JSON.stringify(state.draft));draft.segments.splice(index,1);controller.setDraft(draft);}
function openSeats(index){if(editingDisabled.value)return;seatIndex.value=index;pendingSeats.value=[...state.draft.segments[index].seat_ids];seatBaseline=JSON.stringify(pendingSeats.value);search.value='';floor.value='all';libraryEditor.value=null;seatError.value='';saveEditor();}
function pickSeat(id){
  if(requestBusy.value)return;seatError.value='';
  if(pendingSeats.value.includes(id))pendingSeats.value=pendingSeats.value.filter(value=>value!==id);
  else if(pendingSeats.value.length<5)pendingSeats.value.push(id);
  else seatError.value='每段最多选择 5 个座位';
  saveEditor();
}
function applyGroup(ids){pendingSeats.value=[...ids];seatError.value='';saveEditor();}
function moveSeat(index,delta){const next=index+delta;if(next<0||next>=pendingSeats.value.length)return;const ids=[...pendingSeats.value];[ids[index],ids[next]]=[ids[next],ids[index]];pendingSeats.value=ids;saveEditor();}
function applySeats(){
  if(requestBusy.value||seatIndex.value===null)return;
  if(libraryEditor.value?.dirty){seatError.value='请先保存或取消收藏编辑';return;}
  if(!pendingSeats.value.length){seatError.value='请选择至少一个座位';return;}
  if(controller.locked()){
    const terminal=['completed','stopped'].includes(state.batch?.state);
    if(!terminal||!controller.startNew({preserveDraft:true})){seatError.value='当前批次仍在执行或核对，已保留座位草稿，请稍后再应用';return;}
  }
  if(!Number.isInteger(seatIndex.value)||!state.draft.segments[seatIndex.value]){seatError.value='原时段已变化，已保留座位草稿，请关闭选座后重新选择时段';return;}
  const draft=JSON.parse(JSON.stringify(state.draft));draft.segments[seatIndex.value].seat_ids=[...pendingSeats.value];
  if(!controller.setDraft(draft)){seatError.value='当前批次状态已变化，已保留座位草稿';return;}
  seatIndex.value=null;pendingSeats.value=[];libraryEditor.value=null;saveEditor();
}
async function closeSeats(){
  if(requestBusy.value)return;
  if(seatDraftDirty()){
    const confirm=await new Promise(resolve=>uni.showModal({title:'有未应用的座位修改',content:'是否放弃这次修改？',confirmText:'放弃修改',cancelText:'继续编辑',success:result=>resolve(result.confirm)}));
    if(!confirm||!mounted)return;
  }
  seatIndex.value=null;pendingSeats.value=[];libraryEditor.value=null;seatError.value='';saveEditor();
}
function requestClose(){if(requestBusy.value)return;if(seatIndex.value!==null)closeSeats();else emit('close');}
async function confirmBatch(){await controller.confirm();if(mounted&&controller.active())await controller.poll();}
watch(requestBusy,value=>emit('busy',value),{immediate:true});
watch([libraryEditor,search,floor],()=>saveEditor(),{deep:true});
watch([()=>props.account.id,()=>props.revision,()=>props.sessionKey],()=>{restoreSeats(props.editor);controller.reset(props.editor);if(mounted&&!props.offline)controller.restore();});
onMounted(()=>{mounted=true;if(!props.offline)controller.restore();timer=setInterval(()=>{tick.value=Date.now();if(controller.active())controller.poll();},1500);});
onBeforeUnmount(()=>{mounted=false;clearInterval(timer);controller.dispose();emit('busy',false);});
</script>

<style scoped>
.today-heading,.card-heading,.batch-heading,.preview-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.today-heading{margin-bottom:20px;font-size:15px}.today-heading>.muted{font-size:12px}.interval-card{padding:18px;margin:14px 0;border:1px solid var(--line);border-radius:14px;background:var(--panel,var(--wash))}.card-heading{min-height:28px}.segment-times{margin-top:15px;gap:12px}.segment-times .field{margin-bottom:0;min-width:0}.segment-times .input{width:100%;min-width:0}.segment-seat{display:flex;width:100%;align-items:center;justify-content:space-between;gap:12px;text-align:left;padding:15px 0 0;margin-top:15px;border-top:1px solid var(--line);background:none;border-radius:0}.segment-seat>view{min-width:0;display:grid;gap:7px}.segment-seat .muted{font-size:11px}.segment-seat strong,.seat-summary{overflow-wrap:anywhere;font-size:14px;line-height:1.6}.segment-seat:after{border:0}.add-segment{display:flex;align-items:center;justify-content:center;gap:8px;margin:18px 0}.floor-filter{margin-top:12px}.selected-list{margin-bottom:20px}.selected-item{display:flex;justify-content:space-between;align-items:center;gap:12px;border-bottom:1px solid var(--line);padding:10px 0}.selected-item strong{font-size:14px;overflow-wrap:anywhere;min-width:0}.seat-tools{display:flex;flex:none}.seat-summary{margin:10px 0 0}.result-message,.confirm-impact{font-size:13px;line-height:1.7;overflow-wrap:anywhere}.result-message{color:var(--muted);margin:9px 0 0}.confirm-impact{margin-top:20px}.preview-heading,.batch-heading{margin:20px 0 10px}.result-card .pill{flex:none}.card-heading strong{min-width:0}.context-help{margin-top:18px}@media(max-width:599px){.interval-card{padding:15px}.today-heading{align-items:flex-start}.today-heading>.muted{max-width:120px;text-align:right}.segment-times{gap:10px}.selected-item{align-items:flex-start}.seat-tools .icon-button{padding:6px}}
</style>
