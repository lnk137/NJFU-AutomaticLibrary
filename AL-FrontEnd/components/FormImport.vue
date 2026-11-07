<template>
 <section v-if="role==='root'" class="panel form-import-launcher">
  <view class="section-head"><view><h2>个人表单导入</h2><p class="hint">{{state.batch?(state.batch.state==='ready'&&!state.batch.ready_count?'需核对':importStateName(state.batch.state)):'把模板发给用户，收到填写文件后导入。'}}</p></view><button class="button ghost small-button" @click="openDialog">导入预约表单</button></view>
  <AppDialog :open="open" title="导入个人预约表" @close="closeDialog">
   <a class="template-link" href="/api/local/admin/form-template" download>下载个人填写模板</a>
   <p class="hint">导入即验证，新账号通过后保存；预约仍需确认。</p>
   <view class="form-drop-zone" @dragover.prevent @drop.prevent="dropFile">
    <label for="booking-form-file">选择或拖入用户填好的 XLSX / CSV</label>
    <component :is="'input'" id="booking-form-file" type="file" accept=".xlsx,.csv" aria-label="选择表单文件" :disabled="state.busy||pending" @change="chooseFile"/>
   </view>
   <p class="hint">每份一人 · 最大32 KiB</p>
   <details class="context-help"><summary>填写与导入说明</summary><p>把空白模板发到群里，每个人填写自己的信息后私发给你。你导入、审阅后确认预约。已有账号的计划在确认前保留；这套审阅仅用于表单提交。</p></details>
   <p v-if="state.error" class="notice error" role="alert">{{state.error}} · 可刷新状态核对，无需反复导入。</p>
   <details v-if="state.recent.length>1" class="context-help recent-imports"><summary>最近导入 · {{state.recent.length}}</summary><view><button v-for="item in state.recent" :key="item.id" class="text-button" :disabled="state.busy" @click="controller.select(item.id)">{{item.filename}} · {{importStateName(item.state)}}</button></view></details>
   <FormImportReview :role="role" :batch="state.batch" :seats="seats" :busy="state.busy" @confirm="controller.confirm"/>
   <template #footer><button class="button secondary" @click="closeDialog">关闭</button><button class="button ghost" :disabled="state.busy" @click="controller.refresh">刷新状态</button></template>
  </AppDialog>
 </section>
</template>
<script setup>
import {ref,reactive,computed,watch,onMounted,onBeforeUnmount} from 'vue';
import * as api from '@/services/local_api.js';
import {createImportController,importStateName} from '@/services/form_imports.mjs';
import AppDialog from '@/components/AppDialog.vue';
import FormImportReview from '@/components/FormImportReview.vue';
const props=defineProps({role:String,sessionKey:Number,active:{type:Boolean,default:true},seats:{type:Array,default:()=>[]}});
const emit=defineEmits(['changed']);
const open=ref(false),state=reactive({batch:null,recent:[],busy:false,error:''});let previous='';
const controller=createImportController({api,identity:()=>props.role==='root'&&props.active?'root:'+props.sessionKey:null,isOpen:()=>open.value,
 changed:value=>{Object.assign(state,value);const key=value.batch?.id+':'+value.batch?.state;if(key!==previous&&['ready','done','interrupted'].includes(value.batch?.state))emit('changed');previous=key;}});
const pending=computed(()=>['validating','confirming'].includes(state.batch?.state));
function openDialog(){open.value=true;controller.refresh();}
function closeDialog(){open.value=false;controller.pause();}
function chooseFile(event){const file=event.target.files?.[0];if(file)controller.upload(file);event.target.value='';}
function dropFile(event){const file=event.dataTransfer?.files?.[0];if(file&&!state.busy&&!pending.value)controller.upload(file);}
watch([()=>props.role,()=>props.sessionKey],()=>{open.value=false;controller.stop();});
watch(()=>props.active,value=>{if(value){if(open.value)controller.refresh();}else controller.pause();});
onMounted(()=>{if(props.active)controller.loadRecent();});onBeforeUnmount(()=>props.role==='root'?controller.dispose():controller.stop());
</script>
<style scoped>
.form-drop-zone{margin-top:14px}
.form-import-launcher{margin-bottom:24px}.form-import-launcher .section-head{gap:14px}.form-import-launcher h2{font-size:17px}.form-import-launcher .hint{margin-top:8px}.template-link{display:inline-block;margin:18px 0;color:var(--green);font-size:14px}.form-drop-zone{border:1px dashed var(--green);border-radius:8px;padding:20px;background:var(--wash);font-size:14px}.form-drop-zone label{display:block;margin-bottom:14px}.form-drop-zone input{max-width:100%;font-size:13px}.recent-imports{display:flex;flex-wrap:wrap;gap:4px;margin-top:16px}.recent-imports>text{width:100%}.recent-imports .text-button{font-size:12px}@media(max-width:599px){.form-import-launcher .section-head{align-items:flex-start}.form-drop-zone{padding:14px}}
.form-import-launcher>.section-head{margin-bottom:0}.form-import-launcher>.section-head>view{min-width:0}.form-import-launcher>.section-head>button{flex:none}.recent-imports{display:block}.recent-imports>view{display:flex;flex-wrap:wrap;gap:6px}.recent-imports .text-button{min-height:44px;max-width:100%;overflow-wrap:anywhere;text-align:left}.form-drop-zone input{min-height:44px}.template-link{margin-top:4px;min-height:44px;display:inline-flex;align-items:center}.form-import-launcher .section-head h2{white-space:nowrap}@media(max-width:380px){.form-import-launcher>.section-head{flex-wrap:wrap}}
</style>
