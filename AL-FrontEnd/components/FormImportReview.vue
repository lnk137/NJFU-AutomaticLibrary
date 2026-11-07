<template>
 <section v-if="role==='root'&&batch" aria-label="表单审阅" class="form-review">
  <view class="section-head"><h3>{{batch.filename}}</h3><text class="pill">{{batch.state==='ready'&&!batch.ready_count?'需核对':importStateName(batch.state)}}</text></view>
  <view v-if="batch.rows.length>1" class="review-counts"><text>共 {{batch.rows.length}} 项</text><text v-if="counts.ready">{{batch.state==='ready'?'待确认':'待处理'}} {{counts.ready}}</text><text v-if="counts.done">已处理 {{counts.done}}</text><text v-if="counts.error" class="inline-error">需核对 {{counts.error}}</text></view>
  <p v-if="batch.message" class="notice" :class="{'error':batch.state==='interrupted'}" :role="batch.state==='interrupted'?'alert':'status'">{{batch.message}}</p>
  <p v-if="batch.state==='validating'" class="hint" role="status">正在验证账号，预约等待整份确认。</p>
  <p v-if="batch.state==='confirming'" class="hint" role="status">正在执行，无需再次提交。</p>
  <view v-for="row in batch.rows" :key="row.line" class="form-review-row">
   <view class="section-head"><strong>{{batch.rows.length>1?'第'+row.line+'行 · ':''}}{{row.alias||row.student_id}}</strong><text class="pill" :class="row.status==='error'?'red':'green'">{{row.status==='ready'?'验证通过':row.status==='done'?'已处理':'需核对'}}</text></view>
   <p>{{row.student_id}} · {{row.action==='scheduled'?'生成定时计划':row.action==='today'?'当天一次预约':'内容需修正'}}<text v-if="row.account_saved"> · 账号已保存</text></p>
   <p v-if="formPlanSummary(row)">{{formPlanSummary(row)}}</p>
   <p v-if="row.plan||row.today">{{(row.plan?.seat_ids||row.today?.seat_ids||[]).map((id,index)=>(index?'备选'+index:'首选')+' '+seatName(id)).join(' → ')}}</p>
   <p v-if="row.status==='error'" class="inline-error" role="alert">{{row.message}}</p>
   <details v-else-if="row.message" class="context-help row-detail"><summary>{{row.status==='done'?'处理详情':'验证详情'}}</summary><p>{{row.message}}</p></details>
  </view>
  <template v-if="batch.state==='ready'&&batch.ready_count">
   <p class="confirm-impact">{{batch.rows.length===1?(batch.rows[0].action==='today'?'确认后立即提交本次预约，原定时计划保留。':'确认后保存并开启这份定时计划。'):'确认后：定时计划生效，当天预约立即提交。只处理验证通过的项目。'}}</p>
   <button class="button full" :disabled="busy" @click="$emit('confirm')">{{batch.rows.length===1?'确认这份预约':'确认执行通过的 '+batch.ready_count+' 项'}}</button>
  </template>
 </section>
</template>
<script setup>
import {computed} from 'vue';
import {formPlanSummary,importStateName} from '@/services/form_imports.mjs';
const props=defineProps({role:String,batch:Object,seats:{type:Array,default:()=>[]},busy:Boolean});
const counts=computed(()=>Object.fromEntries(['ready','done','error'].map(status=>[status,(props.batch?.rows||[]).filter(row=>row.status===status).length])));
defineEmits(['confirm']);
const seatName=id=>props.seats.find(s=>s.id===id)?.name||id;
</script>
<style scoped>
.form-review{margin-top:24px}.form-review h3{font-size:15px;overflow-wrap:anywhere}.form-review-row{border-top:1px solid var(--line);padding:16px 0;font-size:13px;line-height:1.9}.form-review-row .section-head{gap:12px}.form-review-row p{margin-top:6px;overflow-wrap:anywhere}.form-review>.hint{margin:14px 0}.form-review .button{margin-top:14px}
.form-review>.section-head{align-items:flex-start;margin-bottom:10px}.form-review h3{min-width:0;flex:1}.form-review>.section-head>.pill,.form-review-row .pill{flex:none}.review-counts{display:flex;gap:8px 16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:18px;font-variant-numeric:tabular-nums}.form-review-row .section-head{margin-bottom:6px;align-items:flex-start}.form-review-row .section-head>strong{min-width:0;overflow-wrap:anywhere}.row-detail{margin-top:2px}.row-detail>summary{min-height:44px}.confirm-impact{font-size:13px;line-height:1.8;margin-top:16px}.form-review-row .inline-error{margin-top:8px}
</style>
