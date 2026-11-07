<template>
<view class="login-page"><header class="login-header"><text class="wordmark">NJFU-AUTO-LIB</text><button class="icon-button" aria-label="登录帮助" @click="help=true"><AppIcon name="help-circle"/></button></header>
<main class="login-card"><h1>{{kind==='root'?'管理员登录':'登录'}}</h1><p class="muted">{{kind==='root'?'仅限本机管理员入口':executorOnline===false?'电脑离线，可登录并保存配置':'学校统一身份认证'}}</p>
<view class="segmented login-tabs" v-if="allowAdmin"><button :class="{active:kind==='student'}" :disabled="busy" @click="switchKind('student')">学生</button><button :class="{active:kind==='root'}" :disabled="busy" @click="switchKind('root')">管理员</button></view>
<view v-if="error" class="notice error" role="alert">{{error}}</view>
<view v-if="store.logoutNotice" class="notice" role="status">{{store.logoutNotice}}</view>
<view class="field"><label for="login-user">{{kind==='student'?'学号':'管理员账号'}}</label><input id="login-user" class="input" v-model="username" :disabled="busy||kind==='root'" :placeholder="kind==='student'?'请输入学号':'root'" maxlength="80" @confirm="submit"/></view>
<view class="field"><label for="login-password">{{kind==='student'?'学校密码':'管理员密码'}}</label><view class="password-field"><input id="login-password" class="input" v-model="password" :password="!passwordVisible" :disabled="busy" placeholder="请输入密码" maxlength="1024" @confirm="submit"/><button class="icon-button" :aria-label="passwordVisible?'隐藏密码':'显示密码'" :disabled="busy" @click="passwordVisible=!passwordVisible"><AppIcon :name="passwordVisible?'eye-off':'eye'"/></button></view></view>
<button class="button full" :disabled="busy||!username.trim()||!password" @click="submit">{{busy?'正在验证…':kind==='student'?(executorOnline===false?'离线登录':'登录'):'进入管理后台'}}</button>
</main>
<footer v-if="kind==='student'" class="login-footer">密码仅在预约电脑加密保存</footer>
<AppDialog :open="help" title="登录帮助" @close="help=false"><view class="help-copy"><h3>学生登录</h3><p>输入学号和学校统一身份认证密码。首次使用需先由管理员授权学号；无需 VPN 密码。</p><h3>离线登录</h3><p>仅限已授权、曾通过学校认证且电脑已保存密码的账号，使用上次认证成功的密码登录。学校明确拒绝时不能离线放行。配置须等电脑同步后生效。</p><h3>定时执行</h3><p>退出或关闭网页不会关闭已开启计划。放号时执行电脑需保持唤醒并连接校园网。</p><h3>遇到验证失败</h3><p>保留页面错误提示，先核对学校密码与网络；若学校要求验证码，需按学校要求完成人工验证。</p></view></AppDialog>
</view>
</template>
<script setup>
import {ref} from 'vue';import {takeReservationLink} from '@/services/reservation_link.mjs';import {onShow,onHide} from '@dcloudio/uni-app';import {useWorkspace} from '@/stores/workspace.js';import {bootstrap} from '@/services/local_api.js';import AppIcon from '@/components/AppIcon.vue';import AppDialog from '@/components/AppDialog.vue';
const store=useWorkspace(),kind=ref('student'),username=ref(''),password=ref(''),busy=ref(false),error=ref(''),allowAdmin=ref(false),passwordVisible=ref(false),help=ref(false);
const executorOnline=ref(null);
function switchKind(value){kind.value=value;username.value=value==='root'?'root':'';password.value='';passwordVisible.value=false;error.value='';}
async function submit(){if(busy.value)return;busy.value=true;error.value='';try{await store.login({kind:kind.value,username:username.value.trim(),password:password.value});uni.reLaunch({url:takeReservationLink(store.user)||(store.user.role==='root'?'/pages/admin/admin':'/pages/index/index')});}catch(e){error.value=e.message;}finally{password.value='';busy.value=false;}}
onShow(async()=>{try{const result=await bootstrap();executorOnline.value=typeof result.executor_online==='boolean'?result.executor_online:null;allowAdmin.value=result.admin_login===true;if(!allowAdmin.value&&kind.value==='root')switchKind('student');if(result.user&&!store.suppressAutoLogin){store.user=result.user;uni.reLaunch({url:takeReservationLink(result.user)||(result.user.role==='root'?'/pages/admin/admin':'/pages/index/index')});}}catch(e){error.value=e.message;}});onHide(()=>{password.value='';});
</script>
<style scoped>
.login-page{min-height:100dvh}.login-header{height:88px;padding:16px max(32px,calc((100vw - 1120px)/2));display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line)}.wordmark{font-size:23px;font-weight:760;letter-spacing:-.6px}.login-card{max-width:410px;margin:64px auto 32px;padding:0 16px}.login-card h1{font-size:26px;margin-bottom:10px}.login-card>p{font-size:14px;margin-bottom:28px}.login-tabs{margin:28px 0}.password-field{position:relative}.password-field .input{padding-right:52px}.password-field button{position:absolute;right:3px;top:1px}.login-footer{text-align:center;font-size:12px;color:var(--muted);padding:0 16px 32px}.help-copy p{color:var(--muted);line-height:1.9;margin:12px 0 28px}@media(max-width:599px){.login-header{height:68px;padding:12px 16px}.wordmark{font-size:18px}.login-card{margin:48px auto 30px;padding:0 24px}.login-card h1{font-size:24px}}
</style>
