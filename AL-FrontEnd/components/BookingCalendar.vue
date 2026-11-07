<template>
  <section class="booking-calendar surface" aria-label="预约日历">
    <view class="section-head"><view><h2>预约日历</h2><p class="hint calendar-subtitle">实际使用日 · 北京时间</p></view><button class="text-button" :disabled="offline||loading||!accountId" @click="reload">{{loading?'读取中…':'刷新日历'}}</button></view>
    <select v-if="role==='root'" class="input calendar-account" :value="accountId" aria-label="选择日历账号" @change="chooseAccount($event.target.value)"><option value="">选择账号</option><option v-for="account in accounts" :key="account.id" :value="account.id">{{account.alias||account.student_id}} · {{account.student_id}}</option></select>
    <view v-if="!accountId" class="empty compact">请选择已保存的学校账号查看日历。</view>
    <view v-else-if="offline" class="notice" role="status">执行电脑在线并重新实时登录后可查看日历。已保存的计划和预约保留。</view>
    <template v-else>
      <view class="calendar-toolbar"><view class="calendar-paging"><button class="icon-button" aria-label="上一期日历" @click="move(-1)">‹</button><strong>{{title}}</strong><button class="icon-button" aria-label="下一期日历" @click="move(1)">›</button></view><view class="calendar-modes"><button class="text-button" @click="today">今天</button><button :class="{active:mode==='month'}" :aria-pressed="mode==='month'" @click="setMode('month')">月</button><button :class="{active:mode==='week'}" :aria-pressed="mode==='week'" @click="setMode('week')">周</button></view></view>
      <view v-if="error" class="notice error" role="alert">{{error}}<button class="text-button" @click="reload">重试</button></view>
      <view v-if="payload?.warnings?.length" class="calendar-warnings" role="status"><p v-for="warning in payload.warnings" :key="warning.code">{{warning.message}}</p></view>
      <template v-if="payload">
        <view class="calendar-weekdays" aria-hidden="true"><text v-for="day in weekdays" :key="day">{{day}}</text></view>
        <view class="calendar-grid" :class="mode"><button v-for="day in days" :key="day.date" class="calendar-day" :class="{selected:selected===day.date,today:day.date===referenceDate,outside:mode==='month'&&day.date.slice(0,7)!==anchor.slice(0,7)}" :aria-pressed="selected===day.date" :aria-label="dayLabel(day)" @click="selected=day.date"><text class="day-number">{{Number(day.date.slice(8))}}</text><view class="day-marks"><span v-for="state in day.states" :key="state" class="calendar-dot" :class="state"></span></view><text v-if="day.entries.length" class="day-count">{{day.entries.length}}项</text></button></view>
        <view class="calendar-legend"><span v-for="(state,key) in calendarStates" :key="key"><i class="calendar-dot" :class="state.tone"></i>{{state.label}}</span></view>
        <view class="calendar-detail"><view class="section-head"><h3>{{selected}} <small>使用安排</small></h3><button class="text-button" @click="$emit('adjust',{accountId,date:selected})">临时调整 ›</button></view><p v-if="!selectedEntries.length" class="hint">此日暂无保存的安排或回执。</p><view v-for="entry in selectedEntries" :key="entry.id" class="calendar-entry"><view class="entry-summary"><strong>{{entry.begin?entry.begin+'–'+entry.end:'全天标记'}}</strong><span class="calendar-state" :class="calendarState(entry).tone">{{calendarState(entry).label}}</span></view><p v-if="entry.seat_ids.length" class="entry-seats">{{entry.seat_ids.map(seatName).join(' → ')}}</p><p class="entry-message">{{entry.message||sourceName(entry.source)}}<span v-if="entry.exception==='override'"> · 单日调整</span></p><p v-if="entry.release_at" class="hint">放号 {{formatStamp(entry.release_at)}}</p><p v-if="entry.stale||entry.stale_plan" class="hint">{{entry.stale?'较早的学校回执缓存，请核对当前状态':'执行时的旧版计划记录'}}</p><p v-if="entry.cancellation_message" class="hint">{{entry.cancellation_message}}</p></view></view>
        <p class="hint calendar-footnote">计划仅表示已保存的安排；成功以学校有效回执为准。日历只读取本机保存的数据。{{payload.snapshot_at?'学校回执更新于 '+formatStamp(payload.snapshot_at)+'。':''}}</p>
      </template>
      <view v-else-if="!error" class="empty compact" role="status">{{loading?'正在读取日历…':'日历尚未载入'}}</view>
    </template>
  </section>
</template>
<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue';
import * as api from '@/services/local_api.js';
import {beijingDate,calendarRange,moveCalendarAnchor,calendarState,calendarStates,createCalendarLoader,parseCalendarDate} from '@/services/booking_calendar.mjs';
const props=defineProps({accounts:{type:Array,default:()=>[]},activeAccountId:{type:String,default:''},role:{type:String,default:'student'},sessionKey:Number,seatMap:{type:Object,default:()=>({})},initialDate:{type:String,default:''},visible:{type:Boolean,default:true},offline:Boolean});
defineEmits(['adjust']);
function validDate(value){try{parseCalendarDate(value);return value;}catch{return beijingDate();}}
const referenceDate=ref(validDate(props.initialDate||beijingDate())),anchor=ref(referenceDate.value),selected=ref(referenceDate.value),mode=ref('month'),chosenAccount=ref('');
const payload=ref(null),days=ref([]),loading=ref(false),error=ref(''),weekdays=['一','二','三','四','五','六','日'];
const accountId=computed(()=>props.role==='root'?(props.accounts.some(a=>a.id===chosenAccount.value)?chosenAccount.value:props.accounts.some(a=>a.id===props.activeAccountId)?props.activeAccountId:props.accounts[0]?.id||''):props.activeAccountId);
const accountRevision=computed(()=>props.accounts.find(a=>a.id===accountId.value)?.revision);
const range=computed(()=>calendarRange(anchor.value,mode.value));
const title=computed(()=>mode.value==='month'?anchor.value.slice(0,7).replace('-','年')+'月':range.value.start+' — '+range.value.end.slice(5));
const selectedEntries=computed(()=>days.value.find(day=>day.date===selected.value)?.entries||[]);
const loader=createCalendarLoader({read:(...args)=>api.getCalendar(...args),context:()=>({accountId:accountId.value,sessionKey:props.sessionKey}),apply:(result,items)=>{payload.value=result;days.value=items;},pending:value=>{loading.value=value;},failure:value=>{error.value=value;}});
let currentLoad=null,currentLoadKey='';
function reload(){
  if(!props.visible||props.offline)return;
  const key=JSON.stringify([accountId.value,props.sessionKey,range.value]);
  if(loading.value&&currentLoad&&key===currentLoadKey)return currentLoad;
  const request=loader.load(range.value);currentLoad=request;currentLoadKey=key;
  request.finally(()=>{if(currentLoad===request)currentLoad=null;});return request;
}
function chooseAccount(id){chosenAccount.value=id;}
function move(direction){anchor.value=moveCalendarAnchor(anchor.value,mode.value,direction);selected.value=anchor.value;}
function setMode(value){anchor.value=selected.value;mode.value=value;}
function refreshReferenceDate(){referenceDate.value=validDate(props.initialDate||beijingDate());}
function today(){refreshReferenceDate();anchor.value=referenceDate.value;selected.value=referenceDate.value;}
function seatName(id){return props.seatMap[id]?.name||id;}
function sourceName(source){return {plan:'已保存的计划',run:'执行记录',school_snapshot:'学校回执',today_batch:'当天预约批次',exception:'临时调整',cancellation:'取消记录',dual_node_verified:'双机核验回执',dual_node_reported:'双机执行记录'}[source]||'保存的记录';}
function formatStamp(value){return String(value||'').replace('T',' ').replace(/(?:\+08:00|Z)$/,'').slice(0,19);}
function dayLabel(day){return day.date+'，'+(day.entries.length?day.entries.map(entry=>calendarState(entry).label+(entry.begin?' '+entry.begin+'–'+entry.end:'')).join('；'):'暂无保存的安排');}
watch([accountId,()=>props.sessionKey,accountRevision,range,()=>props.visible,()=>props.offline],()=>{loader.invalidate();payload.value=null;days.value=[];error.value='';if(props.visible)refreshReferenceDate();if(selected.value<range.value.start||selected.value>range.value.end)selected.value=anchor.value;reload();},{immediate:true});
onBeforeUnmount(()=>loader.invalidate());
defineExpose({reload});
</script>
<style scoped>
.booking-calendar{margin-bottom:34px;min-width:0}.calendar-subtitle{margin:6px 0 0}.calendar-account{margin:4px 0 16px;max-width:420px}.calendar-toolbar,.calendar-paging,.calendar-modes{display:flex;align-items:center;gap:8px}.calendar-toolbar{justify-content:space-between;flex-wrap:wrap;margin:18px 0}.calendar-paging strong{font-size:17px}.calendar-paging .icon-button{font-size:24px}.calendar-modes>button{min-width:36px;min-height:40px;border-radius:8px;font-size:13px}.calendar-modes>button.active{background:var(--accent-soft,#eaf4ee);color:var(--accent,#237344);font-weight:650}.calendar-weekdays,.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}.calendar-weekdays{text-align:center;font-size:12px;color:var(--muted);margin-bottom:8px}.calendar-day{min-width:0;min-height:74px;text-align:left;padding:10px 9px;border:1px solid var(--line);border-radius:10px;position:relative;background:transparent}.calendar-day.selected{border-color:var(--accent,#237344);box-shadow:inset 0 0 0 1px var(--accent,#237344);background:var(--accent-soft,#edf5ef)}.calendar-day.today .day-number{color:var(--accent,#237344);font-weight:750}.calendar-day.outside .day-number{color:var(--muted);opacity:.65}.day-number{font-size:14px}.day-marks{display:flex;gap:3px;flex-wrap:wrap;margin-top:8px}.calendar-dot{display:inline-block;flex-shrink:0;width:6px;height:6px;border-radius:50%;background:#8e9691}.calendar-dot.planned{background:#729884}.calendar-dot.confirmed{background:#257546}.calendar-dot.failed{background:#b44d42}.calendar-dot.uncertain{background:#b28737}.calendar-dot.skipped,.calendar-dot.cancelled{background:#929b96}.calendar-dot.cancelled{border-radius:1px}.day-count{font-size:10px;color:var(--muted);position:absolute;right:7px;top:12px}.calendar-legend{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0 20px;font-size:11px;color:var(--muted)}.calendar-legend span{display:inline-flex;align-items:center;gap:5px}.calendar-detail{border-top:1px solid var(--line);padding-top:18px}.calendar-detail h3{font-size:15px}.calendar-detail small{color:var(--muted);font-weight:400;font-size:12px}.calendar-entry{padding:14px 0;border-bottom:1px solid var(--line)}.entry-summary{display:flex;gap:12px;align-items:center;justify-content:space-between}.entry-summary strong{font-size:15px}.calendar-state{font-size:11px;border-radius:5px;padding:4px 7px;background:#f0f3f1;color:#52625a;white-space:nowrap}.calendar-state.confirmed{background:#e9f3ec;color:#226c40}.calendar-state.failed{background:#faeeec;color:#9e4238}.calendar-state.uncertain{background:#f8f2e5;color:#896822}.entry-seats,.entry-message{font-size:13px;line-height:1.7;margin:6px 0 0;overflow-wrap:anywhere}.entry-message{color:var(--muted)}.calendar-footnote{margin-top:16px;line-height:1.8}.calendar-warnings{font-size:12px;line-height:1.8;color:#896822;margin:10px 0}.calendar-warnings p{margin:5px 0}@media(max-width:599px){.booking-calendar{padding:18px}.calendar-weekdays,.calendar-grid{gap:3px}.calendar-day{padding:8px 5px;min-height:62px;border-radius:7px}.day-count{display:none}.calendar-paging{gap:2px}.calendar-paging strong{font-size:14px}.calendar-paging .icon-button{min-width:30px}.calendar-toolbar{gap:6px}.calendar-modes{gap:2px}.calendar-legend{gap:9px}.calendar-detail .section-head{gap:6px}.calendar-detail h3{font-size:14px}.calendar-detail h3 small{display:block;margin-top:4px}.entry-summary strong{font-size:14px}}
</style>
