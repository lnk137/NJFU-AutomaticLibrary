<template>
<section v-if="user?.role==='student'&&(offline||state.kind!=='none'||failure)" class="inbox-notice" :class="{ 'has-error':hasFailure }" :role="hasFailure?'alert':'status'">
  <details>
    <summary><span class="status-dot" :class="state.kind"></span><strong>{{title}}</strong><span v-if="offline&&state.kind!=='none'" class="offline-label">离线</span><span class="notice-more">说明<span class="notice-chevron">⌄</span></span></summary>
    <div class="inbox-explanation">
      <p v-if="state.kind==='pending'">电脑同步后生效，现有本机计划继续运行。放号准备期间收到的修改，留到本轮结束后同步，不补交。</p>
      <p v-else-if="state.kind==='failed'">新配置未生效，原计划不变。请按提示处理后重新保存。</p>
      <p v-else-if="state.effective">配置已同步到电脑，预约结果请查看执行记录。</p>
      <p v-if="offline">{{status.inbox?.executor_online?'电脑已连接，重新认证后可查询学校记录和当天预约。':'电脑上线后自动同步。离线时可保存配置，暂不能查询学校记录或当天预约。'}}</p>
      <small v-if="state.kind!=='none'&&receipt?.version">配置版本 {{receipt.version}}<span v-if="state.effective"> · 本机版本 {{receipt.applied_revision}}</span></small>
    </div>
  </details>
  <p v-if="failure" class="inbox-failure">{{failure}}</p>
  <button v-if="offline" class="text-button relogin-button" @click="$emit('relogin')">重新认证</button>
</section>
</template>
<script setup>
import {computed} from 'vue';import {receiptState} from '@/services/inbox_status.mjs';
const props=defineProps({user:Object,status:{type:Object,default:()=>({})}});defineEmits(['relogin']);
const offline=computed(()=>props.user?.auth_mode==='offline'||props.status.offline_mode);
const receipt=computed(()=>props.status.inbox?.accounts?.find(a=>a.student_id===props.user?.student_id));
const state=computed(()=>receiptState(receipt.value));
const failure=computed(()=>props.status.inbox?.last_error||(state.value.kind==='failed'&&receipt.value?.message)||'');
const hasFailure=computed(()=>state.value.kind==='failed'||Boolean(failure.value));
const title=computed(()=>state.value.kind!=='none'?state.value.label:failure.value?'连接异常':'离线登录');
</script>
<style scoped>
.inbox-notice{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:12px;margin-bottom:20px;border-block:1px solid var(--line);font-size:13px;color:var(--muted)}.inbox-notice details{min-width:0;grid-column:1}.inbox-notice summary{display:flex;align-items:center;gap:9px;min-height:48px;cursor:pointer;list-style:none}.inbox-notice summary::-webkit-details-marker{display:none}.inbox-notice summary:focus-visible{outline:2px solid var(--green);outline-offset:3px}.inbox-notice strong{font-size:13px;font-weight:500;color:var(--ink)}.status-dot{width:6px;height:6px;border-radius:50%;background:var(--amber);flex-shrink:0}.status-dot.applied{background:var(--green)}.has-error .status-dot{background:var(--danger)}.offline-label{font-size:11px;color:var(--muted)}.notice-more{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:12px}.notice-chevron{font-size:16px;transition:transform .15s}.inbox-notice details[open] .notice-chevron{transform:rotate(180deg)}.inbox-explanation{padding:0 0 12px 15px;max-width:680px}.inbox-explanation p,.inbox-failure{line-height:1.65;overflow-wrap:anywhere;margin:0 0 6px}.inbox-explanation small{font-size:11px}.inbox-failure{grid-column:1/-1;color:var(--danger);padding:0 0 10px 15px}.relogin-button{grid-column:2;grid-row:1;align-self:start;min-height:48px;font-size:12px;padding-inline:0}.inbox-notice summary{flex-wrap:wrap}.inbox-notice strong{overflow-wrap:anywhere}.offline-label{flex-shrink:0}.notice-more{flex-shrink:0}@media(max-width:399px){.inbox-notice strong{font-size:12px}.inbox-notice summary{gap:6px}.offline-label{display:none}}
</style>
