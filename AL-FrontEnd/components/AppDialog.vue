<template>
<Teleport to="body"><view v-if="open" class="dialog-backdrop" @click.self="requestClose">
  <section ref="surface" class="app-dialog" :class="{compact}" role="dialog" aria-modal="true" :aria-labelledby="titleId" tabindex="-1" data-njfu-dialog>
    <header class="dialog-header"><h2 :id="titleId">{{title}}</h2><button class="icon-button" :disabled="busy" aria-label="关闭弹窗" @click="requestClose"><AppIcon name="x"/></button></header>
    <view class="dialog-content"><slot/></view>
    <footer v-if="$slots.footer" class="dialog-footer"><slot name="footer"/></footer>
  </section>
</view></Teleport>
</template>
<script setup>
import {ref,watch,nextTick,onBeforeUnmount,getCurrentInstance} from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import {acquireScrollLock} from '@/services/dialog_lock.mjs';
const props=defineProps({open:Boolean,title:String,busy:Boolean,compact:Boolean});const emit=defineEmits(['close']);
const surface=ref(null),titleId='dialog-title-'+getCurrentInstance().uid;
let priorFocus=null,releaseScroll=null,listening=false;
const requestClose=()=>{if(!props.busy)emit('close');};
function keydown(event){
  if(!props.open||!surface.value||event.defaultPrevented)return;
  if([...document.querySelectorAll('.uni-modal')].some(el=>el.getClientRects().length))return;
  const dialogs=document.querySelectorAll('[data-njfu-dialog]');if(dialogs[dialogs.length-1]!==surface.value)return;
  if(event.key==='Escape'){event.preventDefault();requestClose();}
  if(event.key==='Tab'){
    const elements=[...surface.value.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]')].filter(el=>el.getClientRects().length);
    const first=elements[0],last=elements[elements.length-1];if(!first){event.preventDefault();surface.value.focus();}
    else if(event.shiftKey&&(!surface.value.contains(document.activeElement)||document.activeElement===first)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(!surface.value.contains(document.activeElement)||document.activeElement===last||document.activeElement===surface.value)){event.preventDefault();first.focus();}
  }
}
function detach(){
  if(typeof document==='undefined'||!listening)return;
  document.removeEventListener('keydown',keydown);releaseScroll?.();releaseScroll=null;listening=false;
  const dialogs=[...document.querySelectorAll('[data-njfu-dialog]')];
  if(dialogs.at(-1)!==surface.value)return;
  const parent=dialogs.filter(dialog=>dialog!==surface.value).at(-1);
  if(priorFocus?.isConnected&&(!parent||parent.contains(priorFocus)))priorFocus.focus();
  else parent?.focus();
}
watch(()=>props.open,async opened=>{
  if(typeof document==='undefined')return;
  if(!opened){detach();return;}
  priorFocus=document.activeElement;releaseScroll=acquireScrollLock(document.body);document.addEventListener('keydown',keydown);listening=true;
  await nextTick();if(props.open&&listening)surface.value?.focus();
},{immediate:true});
onBeforeUnmount(detach);
</script>
<style scoped>
.dialog-backdrop{position:fixed;inset:0;z-index:500;display:flex;justify-content:flex-end;background:rgba(27,48,39,.28);animation:backdrop-in 160ms ease}.app-dialog{width:min(520px,100%);height:100%;background:var(--wash);display:flex;flex-direction:column;outline:0;animation:drawer-in 220ms var(--ease);box-shadow:-8px 0 50px #213d3412}.dialog-header,.dialog-footer{padding:20px 24px;display:flex;gap:12px;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);flex:none}.dialog-header h2{font-size:20px}.dialog-content{overflow:auto;padding:24px;flex:1;min-height:0;overscroll-behavior:contain}.dialog-footer{border-top:1px solid var(--line);border-bottom:0;padding-bottom:max(20px,env(safe-area-inset-bottom))}.dialog-footer:deep(.button){flex:1}.compact{width:min(460px,100%)}@keyframes backdrop-in{from{opacity:0}}@keyframes drawer-in{from{transform:translateX(36px);opacity:.4}}@media(max-width:599px){.dialog-backdrop{align-items:flex-end}.app-dialog,.compact{width:100%;height:auto;max-height:92dvh;border-radius:20px 20px 0 0;animation:sheet-in 220ms var(--ease)}.dialog-content{padding:20px;flex:0 1 auto}.dialog-header,.dialog-footer{padding:15px 20px}.dialog-footer{padding-bottom:max(15px,env(safe-area-inset-bottom))}@keyframes sheet-in{from{transform:translateY(30px);opacity:.4}}}@media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
