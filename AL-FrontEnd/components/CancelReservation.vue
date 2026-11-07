<template>
<button v-if="!standalone&&record?.status===1027" class="button ghost" :disabled="disabled" @click="controller.open()">取消本段预约</button>
<AppDialog :open="state.open" title="取消本段预约" :busy="state.busy" @close="close">
  <template v-if="state.result"><p class="notice" :class="state.result.state==='success'?'success':'error'" role="status">{{state.result.message}}</p><p v-if="state.result.state==='uncertain'" class="hint">{{state.result.can_reconcile===true?'可点击核对取消结果，重新查询学校记录。':'系统当前不能自动核对取消结果，请打开学校预约页面核对。'}}</p></template>
  <template v-else-if="state.preview"><strong>{{seatName(state.preview.record.seat_id)}}</strong><p>{{state.preview.record.begin.slice(0,10)}} · {{state.preview.record.begin.slice(11,16)}}–{{state.preview.record.end.slice(11,16)}}</p><p class="hint">确认后取消以上这一段，其他时段与定时计划保留。</p></template>
  <template v-else><p v-if="record?.begin">{{record.begin.slice(0,10)}} · {{record.begin.slice(11,16)}}–{{record.end?.slice(11,16)}}</p><p class="hint">{{state.busy?'正在核对学校最新预约…':'先查询学校最新记录，再确认要取消的具体时段。'}}</p></template>
  <p v-if="state.error" class="notice error" role="alert">{{state.error}}</p>
  <a href="https://libseat.njfu.edu.cn/#/ic/userinfo" target="_blank" rel="noopener noreferrer">打开学校预约页面 ↗</a>
  <template #footer><button class="button secondary" :disabled="state.busy" @click="close">关闭</button><template v-if="state.pending"><button v-if="controller.readAction()" class="button secondary" :disabled="state.busy||disabled" @click="refreshResult">{{controller.readAction()==='reconcile'?'核对取消结果':'读取取消状态'}}</button></template><button v-else-if="state.preview" class="button" :disabled="state.busy||disabled||!allowLive" @click="confirm">{{state.busy?'正在取消…':'确认取消本段'}}</button><button v-else class="button" :disabled="state.busy||disabled" @click="controller.prepare()">{{state.busy?'核对中…':'核对学校预约'}}</button></template>
</AppDialog>
</template>
<script setup>
import {reactive,watch,onBeforeUnmount} from 'vue';
import AppDialog from './AppDialog.vue';
import * as api from '@/services/local_api.js';
import {createCancellationController,newCancellationState} from '@/services/cancellation.mjs';
const props=defineProps({accountId:String,revision:Number,record:Object,sessionKey:Number,seatMap:Object,disabled:Boolean,allowLive:Boolean,autoPreview:Boolean,standalone:Boolean,targetKey:Number});
const emit=defineEmits(['cancelled','closed']);const state=reactive(newCancellationState());
const controller=createCancellationController({api,state,context:()=>({accountId:props.accountId,recordId:props.record?.id,revision:props.revision,session:props.sessionKey})});
watch(()=>[props.accountId,props.record?.id,props.revision,props.sessionKey,props.targetKey,props.autoPreview],()=>{controller.reset();if(props.autoPreview){controller.open({preview:!props.disabled});if(props.disabled)state.error='请重新实时登录后核对学校预约。';}},{immediate:true});
const seatName=id=>props.seatMap?.[id]?.name||id;
function close(){if(controller.close())emit('closed');}
async function confirm(){const result=await controller.confirm();if(result?.state==='success')emit('cancelled',result);}
async function refreshResult(){const result=await controller.read(controller.readAction()==='reconcile');if(result?.state==='success')emit('cancelled',result);}
onBeforeUnmount(()=>controller.dispose());
</script>
