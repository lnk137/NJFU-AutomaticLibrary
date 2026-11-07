<template>
<section id="account-history" class="account-history" aria-label="预约历史">
  <view class="history-heading"><view><h2>预约历史</h2><text v-if="history.loaded" class="history-count">共 {{history.counts?.total??0}} 条 · 成功 {{history.counts?.success??0}} · 失败 {{history.counts?.failed??0}} · 待确认 {{history.counts?.uncertain??0}}</text></view><button class="button ghost" :disabled="history.loading" @click="loader.refresh()">{{history.loading?'读取中…':'刷新'}}</button></view>
  <view v-if="history.error" class="notice error" role="alert">{{history.error}}<text v-if="history.stale"> · 显示上次结果</text><button class="text-button" :disabled="history.loading" @click="loader.refresh()">重试</button></view>
  <view v-if="history.loading&&!history.loaded" class="history-empty">正在读取预约历史…</view>
  <view v-else-if="history.loaded&&!history.items.length" class="history-empty">暂无执行记录</view>
  <view v-else-if="history.loaded" class="history-list">
    <view v-for="run in history.items" :key="run.id" class="history-row">
      <view class="history-date"><strong>{{run.target_date||'日期未知'}}</strong><text>{{run.spec?.begin||'—'}}–{{run.spec?.end||'—'}} · {{run.spec?.kind==='manual'?'当天手动':'定时计划'}}</text></view>
      <view class="history-result"><RunStatus :status="run.status"/><text>{{run.message||'暂无结果说明'}}</text></view>
      <view class="history-start"><text>首次发起</text><strong>{{firstRequestTime(run)}}</strong></view>
    </view>
    <button v-if="history.nextCursor" class="button ghost history-more" :disabled="history.loading" @click="loader.more()">{{history.loading?'读取中…':'加载更多'}}</button>
  </view>
</section>
</template>
<script setup>
import {reactive,watch,onBeforeUnmount} from 'vue';
import {listRuns} from '@/services/local_api.js';
import {createAccountHistoryLoader,firstRequestTime} from '@/services/account_history.mjs';
import RunStatus from '@/components/RunStatus.vue';
const props=defineProps({accountId:{type:String,required:true},sessionKey:{type:Number,required:true}});
const history=reactive({items:[],counts:null,nextCursor:null,loading:false,loaded:false,error:'',stale:false});
const loader=createAccountHistoryLoader({fetch:listRuns,changed:value=>Object.assign(history,value)});
watch(()=>[props.accountId,props.sessionKey],([id,key])=>loader.select(id,key),{immediate:true});
onBeforeUnmount(()=>loader.stop());
</script>
<style scoped>
.account-history{margin-top:36px;padding-top:24px;border-top:1px solid var(--line)}.history-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.history-heading h2{margin:0 0 8px}.history-count{font-size:13px;color:var(--muted)}.history-heading .button{flex:none}.history-empty{padding:24px 0;color:var(--muted);font-size:14px}.history-list{margin-top:12px}.history-row{display:grid;grid-template-columns:1fr 1.4fr 120px;gap:20px;align-items:start;padding:18px 0;border-bottom:1px solid var(--line);font-size:14px}.history-date,.history-result,.history-start{display:flex;flex-direction:column;align-items:flex-start;gap:7px;min-width:0}.history-date text,.history-result text:not(.pill),.history-start text{color:var(--muted);font-size:12px;overflow-wrap:anywhere}.history-start strong{font-variant-numeric:tabular-nums}.history-more{margin-top:18px}@media(max-width:599px){.account-history{margin-top:26px}.history-heading .button{padding:10px 12px}.history-row{grid-template-columns:1fr auto;gap:12px}.history-result{grid-column:1/-1;grid-row:2}.history-start{align-items:flex-end}.history-result text:not(.pill){line-height:1.5}}
</style>
