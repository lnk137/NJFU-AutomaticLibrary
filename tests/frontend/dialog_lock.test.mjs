import test from 'node:test';
import assert from 'node:assert/strict';
import * as lock from '../../AL-FrontEnd/services/dialog_lock.mjs';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

function dialogs(){
  const listeners=new Set(),surfaces=[],document={body:{style:{overflow:'auto'}},activeElement:null,
    querySelectorAll:selector=>selector==='[data-njfu-dialog]'?surfaces:[],
    addEventListener:(type,listener)=>listeners.add(listener),removeEventListener:(type,listener)=>listeners.delete(listener)};
  const element=(name,children=[])=>({name,isConnected:true,getClientRects:()=>[{}],focus(){document.activeElement=this;},contains(node){return this===node||children.includes(node);},querySelectorAll:()=>children});
  const trigger=element('outside trigger');trigger.focus();
  const source=readFileSync(new URL('../../AL-FrontEnd/components/AppDialog.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  function dialog(name){
    const buttons=[element(name+' first'),element(name+' last')],surface=element(name,buttons),props={open:false,busy:false};let changed,unmount;
    const context={document,acquireScrollLock:lock.acquireScrollLock,defineProps:()=>props,defineEmits:()=>()=>{},ref:value=>({value}),getCurrentInstance:()=>({uid:surfaces.length}),nextTick:()=>Promise.resolve(),watch:(getter,callback)=>{changed=callback;callback(false);},onBeforeUnmount:callback=>unmount=callback};
    vm.runInNewContext(source+'\nglobalThis.surfaceRef=surface;',context);context.surfaceRef.value=surface;
    return {surface,buttons,async open(){props.open=true;surfaces.push(surface);await changed(true);},
      async close(){props.open=false;await changed(false);surfaces.splice(surfaces.indexOf(surface),1);surface.isConnected=false;for(const button of buttons)button.isConnected=false;if(surface.contains(document.activeElement))document.activeElement=document.body;},
      unmount(){unmount();}};
  }
  function tab(shiftKey=false){const event={key:'Tab',shiftKey,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};for(const listener of listeners)listener(event);return event;}
  return {document,dialog,trigger,tab};
}
test('closing a nested dialog restores its parent trigger while retaining the parent scroll lock',async()=>{
  const d=dialogs(),parent=d.dialog('parent');await parent.open();parent.buttons[1].focus();
  const child=d.dialog('child');await child.open();await child.close();
  assert.equal(d.document.activeElement,parent.buttons[1]);assert.equal(d.document.body.style.overflow,'hidden');
  await parent.close();assert.equal(d.document.activeElement,d.trigger);assert.equal(d.document.body.style.overflow,'auto');
});
for(const shift of [false,true])test((shift?'backward':'forward')+' Tab returns escaped focus to the top dialog',async()=>{
  const d=dialogs(),parent=d.dialog('parent');await parent.open();d.trigger.focus();
  assert.equal(d.tab(shift).defaultPrevented,true);assert.equal(d.document.activeElement,parent.buttons[shift?1:0]);await parent.close();
});
test('closing a covered parent cannot move focus away from its still-open child',async()=>{
  const d=dialogs(),parent=d.dialog('parent');await parent.open();parent.buttons[0].focus();
  const child=d.dialog('child');await child.open();await parent.close();
  assert.equal(d.document.activeElement,child.surface);assert.equal(d.document.body.style.overflow,'hidden');await child.close();
});
test('closing a nested dialog with a removed trigger focuses the remaining parent surface',async()=>{
  const d=dialogs(),parent=d.dialog('parent');await parent.open();parent.buttons[0].focus();
  const child=d.dialog('child');await child.open();parent.buttons[0].isConnected=false;await child.close();
  assert.equal(d.document.activeElement,parent.surface);await parent.close();
});
const acquire=lock.acquireScrollLock||(()=>()=>{});
test('overlapping dialogs keep scroll locked until the final owner releases',()=>{
  const body={style:{overflow:''}};
  const first=acquire(body),second=acquire(body);
  assert.equal(body.style.overflow,'hidden');
  first();assert.equal(body.style.overflow,'hidden');
  second();assert.equal(body.style.overflow,'');
});
test('closing out of order and repeated cleanup preserve the original overflow',()=>{
  const body={style:{overflow:'auto'}};
  const first=acquire(body),second=acquire(body);
  second();second();assert.equal(body.style.overflow,'hidden');
  first();first();assert.equal(body.style.overflow,'auto');
});
