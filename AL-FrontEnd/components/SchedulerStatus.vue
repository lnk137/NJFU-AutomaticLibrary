<template>
  <section class="scheduler-status" :class="{error:notice.blocked,warning:status.scheduler_warning}" :role="notice.blocked?'alert':'status'" aria-live="polite">
    <template v-if="notice.blocked||status.scheduler_warning"><strong>{{notice.title}}</strong><p>{{notice.detail}}</p><details v-if="notice.hint"><summary>处理说明</summary><p>{{notice.hint}}</p></details></template>
    <details v-else><summary><span class="scheduler-dot"></span><strong>{{notice.title}}</strong><span class="scheduler-more">详情</span></summary><p>{{notice.detail}}</p><p v-if="notice.hint">{{notice.hint}}</p></details>
  </section>
</template>
<script setup>
import {computed} from 'vue';
import {schedulerNotice} from '@/services/scheduler_status.mjs';
const props=defineProps({status:{type:Object,default:()=>({})}});
const notice=computed(()=>schedulerNotice(props.status));
</script>
<style scoped>
.scheduler-status{margin-bottom:22px;border-block:1px solid var(--line);color:var(--muted);font-size:13px}.scheduler-status summary{display:flex;align-items:center;gap:8px;list-style:none;cursor:pointer;min-height:48px}.scheduler-status summary::-webkit-details-marker{display:none}.scheduler-status strong{font-weight:500;color:var(--ink)}.scheduler-status p{line-height:1.65;margin:0 0 12px}.scheduler-dot{width:6px;height:6px;border-radius:50%;background:var(--green)}.scheduler-more{margin-left:auto;font-size:12px}.scheduler-status.error,.scheduler-status.warning{padding:12px 14px;border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:7px;background:var(--paper)}.scheduler-status.error{border-left-color:var(--danger);color:var(--danger)}.scheduler-status.error>strong,.scheduler-status.warning>strong{display:block;margin-bottom:6px}.scheduler-status.error p,.scheduler-status.warning p{margin-bottom:0}.scheduler-status.error summary,.scheduler-status.warning summary{font-size:12px;min-height:44px}
</style>
