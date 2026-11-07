<template><view v-if="store.user" class="session-control"><button class="session-trigger icon-button" :aria-label="store.user.role==='root'?'管理员会话':'我的账号'" @click="opened=true"><AppIcon :name="store.user.role==='root'?'shield':'user'"/></button><AppDialog :open="opened" title="当前会话" :busy="busy" @close="opened=false"><h3>{{store.user.role==='root'?'管理员':store.user.name}}</h3><p class="muted" style="margin-top:10px">{{store.user.role==='root'?'root':store.user.student_id}}</p><view v-if="error" class="notice error">{{error}}</view><button class="button secondary full" style="margin-top:24px" @click="profile">{{store.user.role==='root'?'管理员信息':'我的账号'}}</button><button v-if="store.user.role==='root'" class="button ghost full" style="margin-top:12px" @click="manage">管理后台</button><p class="hint" style="margin-top:24px">退出只结束当前网页会话，已开启的定时计划继续由后台执行。</p><template #footer><button class="button ghost" :disabled="busy" @click="logout">{{busy?'退出中…':'退出登录'}}</button></template></AppDialog></view></template>
<script setup>
import {ref} from 'vue';import {useWorkspace} from '@/stores/workspace.js';
import AppDialog from '@/components/AppDialog.vue';import AppIcon from '@/components/AppIcon.vue';
const props=defineProps({beforeLeave:Function});const store=useWorkspace(),busy=ref(false),error=ref(''),opened=ref(false);
async function allowed(){return !props.beforeLeave||await props.beforeLeave();}
async function manage(){if(await allowed())uni.reLaunch({url:'/pages/admin/admin'});}
async function profile(){if(await allowed())uni.reLaunch({url:'/pages/profile/profile'});}
async function logout(){if(busy.value||!await allowed())return;busy.value=true;try{await store.logout();uni.reLaunch({url:'/pages/login/login'});}catch(e){error.value=e.message;}finally{busy.value=false;}}
</script>
<style scoped>.session-control{display:flex;align-items:center}.session-trigger{background:var(--tint);border-radius:50%;width:44px;height:44px;min-width:44px;padding:9px}</style>
