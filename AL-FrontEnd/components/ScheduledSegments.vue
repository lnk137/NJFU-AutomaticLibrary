<template>
<AppDialog :open="true" title="定时预约时段" :busy="disabled" @close="$emit('close')">
  <p class="hint">每段独立选择座位；保存后在放号时依次预约。</p>
  <p v-if="blockedReason" class="notice" role="status">{{blockedReason}} 当前分段草稿保留，可取消关闭。</p>
  <view v-if="value.plan.mode==='week'" class="days"><button v-for="(label,i) in days" :key="label" :class="{active:day===String(i+1)}" @click="changeDay(String(i+1))">{{label}}<small>{{value.plan.week_segments?.[i+1]?.length||0}}段</small></button></view>
  <view v-if="value.plan.mode==='week'" class="day-actions"><strong>{{days[Number(day)-1]}}</strong><button class="text-button" :disabled="disabled" @click="toggleDay">{{rows.length?'本日不预约':'添加本日时段'}}</button></view>
  <view v-for="(row,i) in rows" :key="row.id" class="segment">
    <view class="row-head"><strong>时段 {{i+1}}</strong><button class="text-button" :disabled="disabled||rows.length===1&&value.plan.mode==='day'" @click="remove(i)">删除</button></view>
    <view class="fields-two"><view class="field"><label :for="'scheduled-begin-'+i">开始</label><input :id="'scheduled-begin-'+i" class="input" :value="row.begin" placeholder="HH:MM" :disabled="disabled" @input="edit(i,'begin',$event.detail.value)"/></view><view class="field"><label :for="'scheduled-end-'+i">结束</label><input :id="'scheduled-end-'+i" class="input" :value="row.end" placeholder="HH:MM" :disabled="disabled" @input="edit(i,'end',$event.detail.value)"/></view></view>
    <button class="seats-button" :disabled="disabled" @click="toggleSeats(i)"><span>{{row.seat_ids.map(seatName).join(' → ')||'选择首选与备选座位'}}</span><AppIcon name="chevron-right"/></button>
    <view v-if="value.seat_index===i" class="seat-editor">
      <view v-for="(id,rank) in row.seat_ids" :key="id" class="selected-seat"><text>{{rank===0?'首选':'备选'+rank}} · {{seatName(id)}}</text><view><button class="icon-button" :disabled="disabled||rank===0" aria-label="上移座位" @click="move(i,rank,-1)"><AppIcon name="arrow-up"/></button><button class="icon-button" :disabled="disabled||rank===row.seat_ids.length-1" aria-label="下移座位" @click="move(i,rank,1)"><AppIcon name="arrow-down"/></button><button class="icon-button" :disabled="disabled" aria-label="移除座位" @click="edit(i,'seat_ids',row.seat_ids.filter(s=>s!==id))"><AppIcon name="x"/></button></view></view>
      <input class="input" :value="value.search||''" placeholder="搜索座位、分组或标签" aria-label="搜索本时段座位" @input="set({search:$event.detail.value})"/>
      <SeatLibrary :editor="value.library_editor" :account="account" :seats="seats" :visible-seats="visibleSeats" :selected="row.seat_ids" :search="value.search||''" :session-key="sessionKey" :disabled="disabled" :offline="offline" @update:editor="set({library_editor:$event})" @pick="pick(i,$event)" @apply="edit(i,'seat_ids',[...$event])" @saved="$emit('preferences-saved',$event)"/>
    </view>
  </view>
  <button v-if="rows.length&&rows.length<7" class="text-button" :disabled="disabled" @click="add">＋ 添加时段</button>
  <p class="hint">每段2–15小时，时段可相接；周五最晚20:00。</p><view v-if="error||localError" class="notice error" role="alert">{{localError||error}}</view>
  <template #footer><button class="button secondary" :disabled="disabled" @click="$emit('close')">取消</button><button class="button" :disabled="disabled||!!blockedReason||!!error||value.library_editor?.dirty" @click="$emit('apply',value.plan)">应用时段</button></template>
</AppDialog>
</template>
<script setup>
import {computed,ref} from 'vue';
import AppDialog from './AppDialog.vue';import AppIcon from './AppIcon.vue';import SeatLibrary from './SeatLibrary.vue';
import {scheduledSegmentsError} from '@/services/scheduled_segments.mjs';
const props=defineProps({modelValue:Object,account:Object,policy:Object,seats:Array,sessionKey:[String,Number],disabled:Boolean,offline:Boolean,blockedReason:{type:String,default:''}});
const emit=defineEmits(['update:modelValue','close','apply','preferences-saved']);
const copy=value=>JSON.parse(JSON.stringify(value)),value=computed(()=>props.modelValue),day=computed(()=>value.value.day||'1'),days=['周一','周二','周三','周四','周五','周六','周日'],localError=ref('');
const rows=computed(()=>value.value.plan.mode==='week'?value.value.plan.week_segments?.[day.value]||[]:value.value.plan.segments);
const error=computed(()=>scheduledSegmentsError(value.value.plan,props.policy));
const seatName=id=>props.seats.find(s=>s.id===id)?.name||id;
const visibleSeats=computed(()=>props.seats.filter(s=>(s.name+' '+s.location).toLowerCase().includes((value.value.search||'').trim().toLowerCase())).slice(0,80));
function set(fields){if(props.disabled)return;emit('update:modelValue',{...copy(value.value),...fields});}
function update(rows,fields={}){if(props.disabled)return;const next={...copy(value.value),...fields};if(next.plan.mode==='week'){if(rows.length)next.plan.week_segments[day.value]=rows;else delete next.plan.week_segments[day.value];}else next.plan.segments=rows;emit('update:modelValue',next);}
function edit(i,key,item){if(props.disabled)return;localError.value='';const next=copy(rows.value);next[i][key]=item;update(next);}
function add(){if(props.disabled)return;const previous=rows.value.at(-1),begin=previous?.end||props.policy.open;const hours=Number(begin.slice(0,2))+2,end=`${String(hours).padStart(2,'0')}:${begin.slice(3)}`;update([...copy(rows.value),{id:'segment-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,6),begin,end,seat_ids:[...(previous?.seat_ids||value.value.plan.seat_ids||[])]}]);}
async function contextChange(action){
  if(props.disabled)return false;
  const context=()=>JSON.stringify([props.account?.id,props.sessionKey,value.value.plan.mode,day.value,value.value.seat_index,rows.value.map(row=>row.id),value.value.library_editor]);
  const token=context();
  if(value.value.library_editor?.dirty){
    const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的收藏编辑',content:'是否放弃当前收藏与分组编辑？',cancelText:'继续编辑',confirmText:'放弃修改',success:r=>resolve(r.confirm)}));
    if(!discard||props.disabled||context()!==token)return false;
  }
  action();return true;
}
const changeDay=day=>contextChange(()=>set({day,seat_index:null,library_editor:null,search:''}));
const toggleSeats=i=>contextChange(()=>set({seat_index:value.value.seat_index===i?null:i,search:'',library_editor:null}));
const remove=i=>contextChange(()=>update(rows.value.filter((_,index)=>index!==i),{seat_index:null,library_editor:null}));
const toggleDay=()=>contextChange(()=>{if(rows.value.length)update([],{seat_index:null,library_editor:null});else add();});
function pick(i,id){const seats=rows.value[i].seat_ids;if(seats.includes(id))edit(i,'seat_ids',seats.filter(s=>s!==id));else if(seats.length<5)edit(i,'seat_ids',[...seats,id]);else localError.value='每段最多选择5个座位';}
function move(i,rank,delta){const selected=[...rows.value[i].seat_ids];[selected[rank],selected[rank+delta]]=[selected[rank+delta],selected[rank]];edit(i,'seat_ids',selected);}
</script>
<style scoped>
.days{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0}.days button{border:1px solid var(--line);border-radius:6px;padding:8px;min-height:44px;flex:1}.days .active{color:var(--green);background:var(--tint);border-color:var(--green)}.days small{display:block;font-size:11px}.day-actions,.row-head,.selected-seat{display:flex;align-items:center;justify-content:space-between;gap:8px}.segment{border-bottom:1px solid var(--line);padding:15px 0}.seats-button{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:48px;width:100%;text-align:left;color:var(--green);overflow-wrap:anywhere}.seats-button span{min-width:0}.seat-editor{padding:12px 0}.selected-seat{font-size:13px}.selected-seat>view{display:flex;flex:none}.seat-editor>.input{margin:12px 0}@media(max-width:599px){.days{gap:4px}.days button{padding:6px 3px;font-size:12px}.selected-seat{align-items:flex-start}.selected-seat>text{padding-top:12px;overflow-wrap:anywhere}}
</style>
