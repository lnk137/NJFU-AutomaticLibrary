<template>
  <section class="date-exceptions" aria-label="临时日期调整">
    <view class="section-head"><h2>临时调整</h2><text class="muted">{{rows.length?rows.length+' 项调整':'暂无调整'}}</text></view>
    <view class="date-actions"><button class="button secondary" :disabled="unavailable" @click="skipNext">跳过下次</button><button class="button ghost" :disabled="unavailable" @click="openEditor('skip_range')">请假几天</button><button class="button ghost" :disabled="unavailable" @click="openEditor('override')">只改某天</button></view>
    <p v-if="account.plan&&!account.plan.enabled" class="hint" role="status">计划已暂停，开启后才执行日期调整。</p>
    <p v-if="offline" class="hint">日期调整需要执行电脑在线。</p>
    <view v-if="message||error" class="notice" :class="error?'error':'success'" :role="error?'alert':'status'">{{error||message}}</view>
    <view v-if="rows.length" class="exception-list">
      <view v-for="row in rows" :key="row.start_date" class="exception-row">
        <view><strong>{{row.start_date}}{{row.end_date!==row.start_date?' 至 '+row.end_date:''}}</strong><p v-if="row.action==='skip'">不预约</p><template v-else><p v-for="segment in row.segments" :key="segment.id">{{segment.begin}}–{{segment.end}} · {{segment.seat_ids.map(seatName).join(' → ')}}</p></template></view>
        <button class="text-button" :disabled="unavailable" @click="restore(row)">恢复原计划</button>
      </view>
    </view>
    <details class="context-help"><summary>调整说明</summary><p>日期填写实际使用日。调整只影响指定日期，之后恢复每天／每周安排；保存前请先保存基本计划。</p></details>
    <AppDialog :open="!!modelValue" :title="modelValue?.action==='override'?'只改某天':'请假几天'" :busy="busy" @close="closeEditor">
      <template v-if="modelValue">
        <p class="hint">填写实际使用日期。</p>
        <template v-if="modelValue.action==='skip_range'"><view class="fields-two"><view class="field"><label>从哪天开始</label><input class="input" type="date" :value="modelValue.start_date" aria-label="请假开始日期" @input="change('start_date',$event.detail.value)"/></view><view class="field"><label>到哪天结束</label><input class="input" type="date" :value="modelValue.end_date" aria-label="请假结束日期" @input="change('end_date',$event.detail.value)"/></view></view></template>
        <template v-else>
          <view class="field"><label>使用日期</label><input class="input" type="date" :value="modelValue.date" aria-label="单日使用日期" @input="change('date',$event.detail.value)"/></view>
          <p class="hint">可选原本不预约的日期；基本计划需保持开启。本次调整会替换这一天的全部定时时段，其他日期不变。</p>
          <p v-if="!multiAllowed" class="hint" role="status">此账号使用双机执行，目前支持单段日期调整；多段内容不会被截断保存。</p>
          <view v-for="(segment,index) in editorSegments" :key="segment.id" class="date-segment">
            <view class="section-head"><strong>时段 {{index+1}}</strong><button class="text-button" :disabled="busy||editorSegments.length===1" :aria-label="'删除单日时段'+(index+1)" @click="removeSegment(index)">删除</button></view>
            <view class="fields-two"><view class="field"><label>开始时间</label><input class="input" :value="segment.begin" :disabled="busy" placeholder="HH:MM" :aria-label="'单日时段'+(index+1)+'开始时间'" @input="editSegment(index,'begin',$event.detail.value)"/></view><view class="field"><label>结束时间</label><input class="input" :value="segment.end" :disabled="busy" placeholder="HH:MM" :aria-label="'单日时段'+(index+1)+'结束时间'" @input="editSegment(index,'end',$event.detail.value)"/></view></view>
            <button class="text-button" :disabled="busy" :aria-label="'选择单日时段'+(index+1)+'座位'" @click="selectSegment(index)">{{segment.seat_ids.map(seatName).join(' → ')||'选择首选与备选座位'}}</button>
            <template v-if="selectedSegment===index">
              <view class="single-seats"><view v-for="(id,rank) in segment.seat_ids" :key="id" class="single-seat"><strong>{{rank===0?'首选':'备选'+rank}} · {{seatName(id)}}</strong><view><button class="text-button" :disabled="rank===0||busy" aria-label="上移单日座位" @click="moveSeat(rank,-1)">↑</button><button class="text-button" :disabled="rank===segment.seat_ids.length-1||busy" aria-label="下移单日座位" @click="moveSeat(rank,1)">↓</button><button class="text-button" :disabled="busy" aria-label="移除单日座位" @click="removeSeat(rank)">移除</button></view></view></view>
              <input class="input" :value="modelValue.search" placeholder="搜索其他座位" aria-label="搜索单日座位" @input="change('search',$event.detail.value,false)"/>
              <SeatLibrary :account="account" :seats="seats" :visible-seats="choices" :selected="segment.seat_ids" :search="modelValue.search" :session-key="sessionKey" :disabled="busy||disabled" :offline="offline" :editor="modelValue.libraryEditor||null" @update:editor="changeLibraryEditor" @pick="pickSeat" @apply="editSegment(index,'seat_ids',$event)" @saved="preferencesSaved"/>
            </template>
          </view>
          <button class="text-button" :disabled="busy||!multiAllowed||editorSegments.length>=7" @click="addSegment">＋ 添加单日时段</button>
          <p class="hint">每段1–5个座位，按顺序尝试；每天1–7段。时段不能重叠，周五最晚20:00，每段至少2小时。</p>
        </template>
        <view v-if="error||staleDraft||validationError" class="notice error" role="alert">{{error||staleDraft||validationError}}<button v-if="staleDraft||conflict" class="text-button" :disabled="busy" @click="reloadEditor">重新读取此日期</button></view>
      </template>
      <template #footer><button class="button secondary" :disabled="busy" @click="closeEditor">取消</button><button class="button" :disabled="unavailable||!!staleDraft||!!validationError||modelValue?.libraryEditor?.dirty" @click="saveEditor">{{busy?'保存中…':'保存本次调整'}}</button></template>
    </AppDialog>
  </section>
</template>
<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue';
import * as api from '@/services/local_api.js';
import {nextPlanWindow} from '@/services/drafts.mjs';
import {dateExceptionRows,newDateEditor,calendarDateEditor,exceptionSegments,dateEditorError} from '@/services/date_exceptions.mjs';
import AppDialog from '@/components/AppDialog.vue';
import SeatLibrary from '@/components/SeatLibrary.vue';
const props=defineProps({account:{type:Object,required:true},policy:{type:Object,required:true},seats:{type:Array,default:()=>[]},sessionKey:Number,disabled:Boolean,offline:Boolean,modelValue:{type:Object,default:null}});
const emit=defineEmits(['update:modelValue','saved','busy','preferences-saved','refreshed']);
const busy=ref(false),error=ref(''),message=ref(''),conflict=ref(false);let sequence=0;
const unavailable=computed(()=>busy.value||props.disabled||props.offline||!props.account?.plan);
const rows=computed(()=>dateExceptionRows(props.account?.date_exceptions));
const multiAllowed=computed(()=>props.account?.capabilities?.date_override_segments!==false);
const editorSegments=computed(()=>props.modelValue?exceptionSegments(props.modelValue):[]);
const selectedSegment=computed(()=>Math.min(props.modelValue?.seat_index||0,Math.max(0,editorSegments.value.length-1)));
const activeSegment=computed(()=>editorSegments.value[selectedSegment.value]);
const staleDraft=computed(()=>props.modelValue&&(props.modelValue.account_id!==props.account.id||props.modelValue.base_revision!==props.account.revision)?'账号配置已变化，当前日期草稿已保留；重新读取后再编辑，避免覆盖其他修改。':'');
const validationError=computed(()=>props.modelValue?.action==='override'?(!multiAllowed.value&&editorSegments.value.length>1?'此账号的双机执行暂不支持多段日期调整。':dateEditorError({...props.modelValue,segments:editorSegments.value},props.policy)):'');
const choices=computed(()=>props.seats.filter(s=>!activeSegment.value?.seat_ids.includes(s.id)&&(s.name+' '+s.location).toLowerCase().includes((props.modelValue?.search||'').toLowerCase())).slice(0,20));
const seatName=id=>props.seats.find(s=>s.id===id)?.name||id;
function identity(){return {id:props.account.id,epoch:props.sessionKey,ticket:sequence};}
function current(token){return token.id===props.account.id&&token.epoch===props.sessionKey&&token.ticket===sequence;}
function clear(){const ownedBusy=busy.value;sequence++;busy.value=false;error.value='';message.value='';conflict.value=false;if(ownedBusy)emit('busy',false);}
watch([()=>props.account?.id,()=>props.sessionKey],clear,{flush:'sync'});
onBeforeUnmount(clear);
function openEditor(action){if(unavailable.value)return;error.value='';message.value='';const next=nextPlanWindow(props.account?.plan,props.policy,new Date(),props.account?.date_exceptions);emit('update:modelValue',newDateEditor(action,props.account,props.policy,next));}
function change(key,value,dirty=true){if(busy.value||!props.modelValue)return;if(['begin','end','seat_ids'].includes(key))return editSegment(selectedSegment.value,key,value);emit('update:modelValue',{...props.modelValue,[key]:value,dirty:props.modelValue.dirty||dirty});}
function changeLibraryEditor(editor){if(busy.value||!props.modelValue)return;emit('update:modelValue',{...props.modelValue,libraryEditor:editor});}
function preferencesSaved(result){if(result.account_id===props.account.id)emit('preferences-saved',result);}
function updateSegments(segments,fields={}){if(busy.value||!props.modelValue)return;emit('update:modelValue',{...props.modelValue,...segments[0],segments,dirty:true,...fields});}
function editSegment(index,key,value){const segments=editorSegments.value;if(!segments[index])return;segments[index][key]=Array.isArray(value)?[...value]:value;updateSegments(segments);}
async function changeSegmentContext(action){
  if(busy.value)return;const token=identity(),draft=props.modelValue;
  if(draft?.libraryEditor?.dirty){const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的收藏编辑',content:'是否放弃当前收藏编辑？',cancelText:'继续编辑',confirmText:'放弃修改',success:r=>resolve(r.confirm)}));if(!discard||!current(token)||props.modelValue!==draft)return;}
  action();
}
function selectSegment(index){return changeSegmentContext(()=>emit('update:modelValue',{...props.modelValue,seat_index:index,libraryEditor:null,search:''}));}
function removeSegment(index){if(editorSegments.value.length<=1)return;return changeSegmentContext(()=>updateSegments(editorSegments.value.filter((_,i)=>i!==index),{seat_index:0,libraryEditor:null,search:''}));}
function addSegment(){
  if(!multiAllowed.value||editorSegments.value.length>=7)return;
  const rows=editorSegments.value,last=rows.at(-1),begin=last?.end||props.policy.open;
  const end=String(Number(begin.slice(0,2))+2).padStart(2,'0')+begin.slice(2);
  updateSegments([...rows,{id:'date-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,7),begin,end,seat_ids:[...(last?.seat_ids||[])]}],{segment_payload:true});
}
function pickSeat(id){const selected=activeSegment.value?.seat_ids||[];if(selected.length<5&&!selected.includes(id))change('seat_ids',[...selected,id]);}
function moveSeat(index,delta){const selected=[...(activeSegment.value?.seat_ids||[])],target=index+delta;if(target<0||target>=selected.length)return;[selected[index],selected[target]]=[selected[target],selected[index]];change('seat_ids',selected);}
function removeSeat(index){change('seat_ids',activeSegment.value.seat_ids.filter((id,i)=>i!==index));}
async function reloadEditor(){
  if(busy.value||!props.modelValue)return;const token=identity(),draft=props.modelValue;
  const accepted=await new Promise(resolve=>uni.showModal({title:'重新读取日期调整',content:'将放弃本次日期草稿，读取最新保存的安排。其他未保存的基本计划保持不变。',cancelText:'保留草稿',confirmText:'重新读取',success:r=>resolve(r.confirm)}));
  if(!accepted||!current(token)||props.modelValue!==draft)return;
  busy.value=true;emit('busy',true);error.value='';
  try{const response=await api.listAccounts();if(!current(token))return;const account=response.accounts?.find(row=>row.id===token.id);if(!account)throw new Error('账号已变化，请重新登录');
    const editor=draft.action==='override'?calendarDateEditor(draft.date,account,props.policy):{...newDateEditor(draft.action,account,props.policy,{date:draft.start_date}),end_date:draft.end_date};
    emit('refreshed',account);emit('update:modelValue',editor);conflict.value=false;
  }catch(e){if(current(token))error.value=e.message;}finally{if(current(token)){busy.value=false;emit('busy',false);}}
}
async function closeEditor(){
  if(busy.value)return;const token=identity();
  if(props.modelValue?.dirty||props.modelValue?.libraryEditor?.dirty){const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的日期调整',content:'是否放弃当前编辑？',cancelText:'继续编辑',confirmText:'放弃修改',success:r=>resolve(r.confirm)}));if(!discard||!current(token))return;}
  emit('update:modelValue',null);error.value='';
}
async function persist(data,revision=props.account.revision){
  if(unavailable.value)return;sequence++;const token=identity();
  busy.value=true;emit('busy',true);error.value='';message.value='';
  try{
    const result=await api.updateDateExceptions(token.id,{...data,expected_revision:revision});
    if(!current(token))return;
    if(result.account?.id!==token.id)throw new Error('账号响应不一致，请重新载入');
    emit('saved',result.account);emit('update:modelValue',null);message.value='日期调整已保存。';
  }catch(e){if(current(token)){error.value=e.message;conflict.value=e.code==='conflict';}}
  finally{if(current(token)){busy.value=false;emit('busy',false);}}
}
async function skipNext(){return persist({action:'skip_next'});}
async function restore(row){return persist({action:'remove',start_date:row.start_date,end_date:row.end_date});}
async function saveEditor(){
  const d=props.modelValue;if(!d)return;
  if(staleDraft.value){error.value=staleDraft.value;return;}
  if(validationError.value){error.value=validationError.value;return;}
  if(d.libraryEditor?.dirty){error.value='请先保存或取消收藏编辑';return;}
  const segments=editorSegments.value;
  const interval=(!multiAllowed.value||segments.length===1&&!d.segment_payload)?{begin:segments[0].begin,end:segments[0].end,seat_ids:[...segments[0].seat_ids]}:{segments};
  return persist(d.action==='override'?{action:'override',date:d.date,...interval}:{action:'skip_range',start_date:d.start_date,end_date:d.end_date},d.base_revision);
}
</script>
<style scoped>
.date-exceptions{border-top:1px solid var(--line);padding:24px 0;margin-top:8px}.section-head h2{font-size:17px}.section-head>text{font-size:12px}.date-actions{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 12px}.date-actions .button{min-height:42px;padding:10px 14px;font-size:14px}.exception-list{max-height:300px;overflow:auto;margin-top:16px}.exception-row{display:flex;align-items:center;justify-content:space-between;gap:14px;border-bottom:1px solid var(--line);padding:15px 0;font-size:13px}.exception-row p{font-size:12px;color:var(--muted);margin-top:7px;line-height:1.7}.exception-row>button{flex:none}.single-seats{margin:18px 0}.single-seat{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13px;border-bottom:1px solid var(--line);padding:10px 0}.single-seat>view{display:flex;flex:none}.single-choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;max-height:180px;overflow:auto;margin:12px 0}.single-choices .button{font-size:12px;padding:10px}.field{margin:14px 0}@media(max-width:599px){.date-actions{gap:7px}.date-actions .button{padding:10px 11px;font-size:13px}.exception-row{align-items:flex-start}.single-seat{align-items:flex-start;flex-wrap:wrap}.single-seat>view{margin-left:auto}}
.date-segment{border-bottom:1px solid var(--line);padding:14px 0}.date-actions .button{min-height:44px}.exception-row>view{min-width:0;overflow-wrap:anywhere}.exception-row .text-button,.single-seat .text-button{min-height:44px}.single-seat strong{min-width:0;overflow-wrap:anywhere}
</style>
