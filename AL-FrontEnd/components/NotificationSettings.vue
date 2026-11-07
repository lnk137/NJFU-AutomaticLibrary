<template>
  <section class="notification-settings" aria-label="邮件通知">
    <view class="section-head"><h2>邮件通知</h2><text class="muted">{{summary}}</text></view>
    <p v-if="state.settings?.address_masked" class="hint">接收邮箱：{{state.settings.address_masked}} · {{state.settings.verified?'已验证':'待验证'}}</p>
    <p v-if="state.settings&&!state.settings.configured" class="hint">邮件服务尚未配置，请联系管理员。</p>
    <p v-if="offline" class="hint">执行电脑在线时可管理通知。</p>
    <view v-if="state.error||state.message" class="notice" :class="state.error?'error':'success'" :role="state.error?'alert':'status'">{{state.error||state.message}}</view>
    <view class="actions"><button class="button secondary" :disabled="unavailable||state.loading" @click="controller.edit()">通知设置</button><button class="button ghost" :disabled="unavailable||state.loading" @click="controller.reload()">{{state.loading?'读取中…':'重新读取'}}</button></view>
    <AppDialog :open="state.open" title="邮件通知" :busy="state.busy" @close="cancel">
      <template v-if="state.draft">
        <p class="hint">默认关闭。验证邮箱后可开启预约结果和使用前提醒。</p>
        <p v-if="state.settings.address_masked" class="hint">当前邮箱：{{state.settings.address_masked}}</p>
        <view class="field"><label>接收邮箱</label><input class="input" :value="state.address" type="text" inputmode="email" autocomplete="email" aria-label="通知接收邮箱" placeholder="填写或更换邮箱" :disabled="unavailable" @input="state.address=$event.detail.value"/></view>
        <button class="button ghost" :disabled="unavailable||!state.settings.configured||!state.address.trim()" @click="controller.request()">发送验证码</button>
        <button v-if="state.address||state.challenge" class="text-button" :disabled="unavailable" @click="controller.cancelVerification()">取消邮箱验证</button>
        <view v-if="state.challenge" class="field"><label>邮箱验证码</label><view class="code-row"><input class="input" :value="state.code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" aria-label="邮箱验证码" placeholder="6 位数字" :disabled="unavailable" @input="state.code=$event.detail.value"/><button class="button secondary" :disabled="unavailable||state.code.length!==6" @click="controller.confirm()">验证邮箱</button></view></view>
        <view class="toggle-row"><label for="notification-results">预约结果通知</label><button id="notification-results" class="notification-toggle" :aria-pressed="state.draft.results_enabled" :disabled="unavailable||!state.settings.verified||(!state.settings.configured&&!state.draft.results_enabled)" aria-label="预约结果通知" @click="state.draft.results_enabled=!state.draft.results_enabled">{{state.draft.results_enabled?'已开启':'已关闭'}}</button></view>
        <p class="hint">预约成功、明确失败、待确认和取消成功会自动通知。</p>
        <view class="toggle-row"><label for="notification-reminders">使用前提醒</label><button id="notification-reminders" class="notification-toggle" :aria-pressed="state.draft.reminders_enabled" :disabled="unavailable||!state.settings.verified||(!state.settings.configured&&!state.draft.reminders_enabled)" aria-label="使用前提醒" @click="state.draft.reminders_enabled=!state.draft.reminders_enabled">{{state.draft.reminders_enabled?'已开启':'已关闭'}}</button></view>
        <view class="field"><label>提前多少分钟（默认 30）</label><input class="input" type="number" :value="String(state.draft.reminder_minutes)" aria-label="提前提醒分钟数" :disabled="unavailable" @input="state.draft.reminder_minutes=$event.detail.value"/><p class="hint">可设 1–1440 分钟。只提醒已取得学校有效回执的时段；邮件可打开对应预约查看或取消。</p></view>
        <view v-if="state.error||state.message" class="notice" :class="state.error?'error':'success'" :role="state.error?'alert':'status'">{{state.error||state.message}}</view>
      </template>
      <template #footer><button class="button ghost" :disabled="state.busy" @click="cancel">取消</button><button class="button primary" :disabled="unavailable||!state.draft" @click="controller.save()">{{state.busy?'处理中…':'保存设置'}}</button></template>
    </AppDialog>
  </section>
</template>
<script setup>
import {computed,onBeforeUnmount,reactive,watch} from 'vue';
import AppDialog from './AppDialog.vue';
import * as api from '../services/local_api.js';
import {createNotificationController,newNotificationState} from '../services/notification_settings.mjs';
const props=defineProps({accountId:{type:String,default:''},sessionKey:{type:String,default:''},disabled:Boolean,offline:Boolean});
const state=reactive(newNotificationState()),controller=createNotificationController(api,state);
const unavailable=computed(()=>props.disabled||props.offline||state.busy||!props.accountId);
const summary=computed(()=>state.loading?'读取中…':state.settings?.results_enabled||state.settings?.reminders_enabled?'已开启':'未开启');
watch([()=>props.accountId,()=>props.sessionKey],()=>{controller.switchAccount(props.accountId,props.sessionKey);if(props.accountId&&!props.offline)controller.reload();},{immediate:true,flush:'sync'});
watch(()=>props.offline,offline=>{if(!offline&&props.accountId&&!state.open)controller.reload();});
onBeforeUnmount(()=>controller.close());
async function cancel(){
  if(state.busy)return false;
  if(controller.dirty()){
    const id=props.accountId,session=props.sessionKey;
    const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的通知设置',content:'是否放弃当前编辑？',cancelText:'继续编辑',confirmText:'放弃修改',success:r=>resolve(r.confirm)}));
    if(!discard||id!==props.accountId||session!==props.sessionKey)return false;
  }
  return controller.cancel();
}
async function beforeLeave(){if(state.busy)return false;if(state.open&&controller.dirty())return cancel();return true;}
defineExpose({beforeLeave});
</script>
<style scoped>
.notification-toggle{min-width:76px;padding:8px 12px;border:1px solid var(--line);border-radius:20px;color:var(--muted)}.notification-toggle[aria-pressed="true"]{background:var(--tint);border-color:var(--green);color:var(--green)}.notification-settings{border-top:1px solid var(--line);padding:24px 0;margin-top:8px}.section-head h2{font-size:17px}.section-head>text{font-size:12px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}.field{margin:18px 0}.toggle-row{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:22px}.toggle-row label{font-size:14px}.code-row{display:flex;align-items:center;gap:10px}.code-row .input{flex:1;min-width:0}.code-row button{flex:none}.button{min-height:44px}.hint{overflow-wrap:anywhere}
</style>
