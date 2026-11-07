import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
import * as status from '../../AL-FrontEnd/services/scheduler_status.mjs';

test('expired background requests cannot rebuild an already-open login form',()=>{
  let app,expired,resets=0,navigations=0;
  const source=readFileSync(new URL('../../AL-FrontEnd/App.vue',import.meta.url),'utf8').split('<script>')[1].split('</script>')[0].replace(/^import .*\n/gm,'').replace('export default','app=');
  const context={app:null,useWorkspace:()=>({clearSession:()=>resets++}),uni:{$on:(_,f)=>expired=f,reLaunch:()=>navigations++},getCurrentPages:()=>[{route:'pages/login/login'}]};
  vm.runInNewContext(source+'\napp.onLaunch();',context);expired();expired();
  assert.equal(navigations,0);assert.equal(resets,2);
});

test('an expired session still clears private state and redirects protected pages',()=>{
  let expired,resets=0,navigations=0;
  const source=readFileSync(new URL('../../AL-FrontEnd/App.vue',import.meta.url),'utf8').split('<script>')[1].split('</script>')[0].replace(/^import .*\n/gm,'').replace('export default','app=');
  const context={app:null,useWorkspace:()=>({clearSession:()=>resets++}),uni:{$on:(_,f)=>expired=f,reLaunch:()=>navigations++},getCurrentPages:()=>[{route:'pages/admin/admin'}]};
  vm.runInNewContext(source+'\napp.onLaunch();',context);expired();
  assert.equal(navigations,1);assert.equal(resets,1);
});

test('repeated overview show and hide leave no hidden authentication poller',()=>{
  const hooks={},timers=new Map();let next=0;
  const store={user:null,status:{},accounts:[],seatMap:{},policy:null,activeAccountId:'',snapshots:{},runs:[],sessionEpoch:1,async refresh(){},async loadRuns(){},invalidate(){}};
  const ref=value=>({value}),computed=fn=>({get value(){return fn();}});
  const source=readFileSync(new URL('../../AL-FrontEnd/pages/index/index.vue',import.meta.url),'utf8').split('<script setup>')[1].split('</script>')[0].replace(/import\s[\s\S]*?;/g,'');
  const context={ref,computed,useWorkspace:()=>store,onShow:f=>hooks.show=f,onHide:f=>hooks.hide=f,onUnload:f=>hooks.unload=f,
    setInterval:f=>{const id=++next;timers.set(id,f);return id;},clearInterval:id=>timers.delete(id),...status,uni:{}};
  vm.runInNewContext(source,context);hooks.show();hooks.show();hooks.hide();
  assert.equal(timers.size,0);
});
