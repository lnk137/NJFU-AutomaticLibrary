<template>
<section v-if="role==='root'&&(data?.configured||error)" class="panel inbox-monitor">
  <view class="section-head"><h2>配置同步 <span class="connection-state" :class="{online:data?.connected}">{{data?.connected?'已连接':'待连接'}}</span></h2><button class="text-button" @click="$emit('refresh')">刷新</button></view>
  <view v-if="error||data?.last_error||data?.export_error" class="notice error" role="alert">{{error||data.last_error||data.export_error}}</view>
  <view class="inbox-metrics"><view><text>待同步</text><b>{{data?.pending??'—'}}</b></view><view><text>同步失败</text><b :class="{'auth-error':data?.failed>0}">{{data?.failed??'—'}}</b></view><view><text>最后同步</text><b>{{time(data?.last_sync_at)}}</b></view><view><text>心跳</text><b>{{time(data?.heartbeat_at)}}</b></view></view>
  <view v-for="row in attentionAccounts" :key="row.student_id" class="inbox-account"><view><strong>{{row.name||row.student_id}}</strong><text class="row-sub">{{row.student_id}}</text></view><view><text>{{state(row).kind==='none'?(row.sync_error?'同步异常':'认证异常'):state(row).label}}</text><text class="row-sub">版本 {{row.version||0}} / 已生效 {{row.applied_version||0}}</text></view><view><text v-if="row.sync_error||row.auth_error" class="auth-error">{{row.sync_error||authLabel(row.auth_error)}}</text><text v-if="row.message" class="row-sub">{{row.message}}</text></view><button class="text-button" @click="$emit('open-account',row.account_id)">查看账号</button></view>
  <p v-if="!data?.accounts?.length" class="hint">尚无账号同步数据</p>
  <p v-if="unreadyCount" class="hint" role="status">计划未开启或需核对 {{unreadyCount}} 个，可展开同步详情查看。</p>
  <details class="sync-details"><summary>同步详情<span>{{(data?.accounts||[]).length}} 个账号</span></summary><p class="hint">放号准备期间的新配置在本轮结束后同步。配置生效与学校预约成功分别记录。</p><p class="hint">最新版本核对 {{time(data?.last_version_check_at)}}</p><view v-for="row in otherAccounts" :key="row.student_id" class="inbox-account"><view><strong>{{row.name||row.student_id}}</strong><text class="row-sub">{{row.student_id}}</text></view><view><text>云端 {{row.version||0}} / 已生效 {{row.applied_version||0}}</text><text class="row-sub">{{row.plan_ready?'配置检查通过':'计划未开启或需核对'}}</text></view><view><text>{{row.offline_eligible?'支持离线登录':'暂不可离线登录'}}</text><text v-if="row.message" class="row-sub">{{row.message}}</text></view><button class="text-button" @click="$emit('open-account',row.account_id)">查看账号</button></view></details>
</section>
</template>
<script setup>
import {computed} from 'vue';import {inboxTime,receiptState} from '@/services/inbox_status.mjs';
const props=defineProps({role:String,data:Object,error:String});defineEmits(['refresh','open-account']);
const time=inboxTime,state=receiptState;
const authLabel=code=>({login_failed:'学校拒绝登录',verification_required:'学校要求人工验证',login_incomplete:'学校认证未完成',identity_mismatch:'学校身份不一致'}[code]||code);
const needsAttention=row=>['pending','failed'].includes(state(row).kind)||row.sync_error||row.auth_error;
const attentionAccounts=computed(()=>(props.data?.accounts||[]).filter(needsAttention));
const otherAccounts=computed(()=>(props.data?.accounts||[]).filter(row=>!needsAttention(row)));
const unreadyCount=computed(()=>otherAccounts.value.filter(row=>row.plan_ready!==true).length);
</script>
<style scoped>
.inbox-monitor{margin:0 0 30px}.inbox-monitor h2{display:flex;align-items:center;gap:12px}.connection-state{font-size:12px;font-weight:400;color:var(--amber)}.connection-state.online{color:var(--green)}.inbox-metrics{display:grid;grid-template-columns:.7fr .7fr 1.5fr 1.5fr;gap:20px;margin:20px 0}.inbox-metrics>view{display:flex;flex-direction:column;gap:8px}.inbox-metrics text{font-size:12px;color:var(--muted)}.inbox-metrics b{font-size:17px;font-weight:500;overflow-wrap:anywhere}.inbox-account{display:grid;grid-template-columns:1fr 1.3fr 1.5fr auto;gap:18px;padding:16px 0;border-top:1px solid var(--line);font-size:13px;align-items:start}.auth-error{color:var(--danger)}.sync-details{border-top:1px solid var(--line)}.sync-details summary{cursor:pointer;min-height:48px;display:flex;align-items:center;gap:12px;font-size:13px}.sync-details summary span{color:var(--muted);font-size:12px}.sync-details .hint{margin:0 0 12px}@media(max-width:900px){.inbox-metrics{grid-template-columns:1fr 1fr}.inbox-account{grid-template-columns:1fr 1fr}.inbox-account>view:nth-child(3){grid-column:1/-1}}@media(max-width:599px){.inbox-monitor .section-head{align-items:flex-start;gap:8px}.inbox-monitor h2{font-size:17px}.inbox-account{grid-template-columns:1fr;gap:8px}.inbox-account>view:nth-child(3){grid-column:1}.inbox-account .text-button{justify-self:start}.inbox-metrics b{font-size:16px}}
.sync-details summary::after{content:"⌄";margin-left:auto;font-size:15px}.sync-details[open]>summary::after{transform:rotate(180deg)}
</style>
