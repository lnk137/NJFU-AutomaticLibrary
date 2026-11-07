<template>
  <section class="seat-library" aria-label="个人收藏与偏好分组">
    <view class="library-tabs" role="tablist" aria-label="座位目录分类"><button v-for="tab in tabs" :key="tab.key" role="tab" :aria-selected="activeTab===tab.key" :class="{selected:activeTab===tab.key}" @click="activeTab=tab.key">{{tab.label}}<text v-if="tab.key!=='all'" class="tab-count">{{tab.key==='favorites'?data.favorites.length:data.groups.length}}</text></button></view>
    <p v-if="offline" class="hint">可选用已同步的收藏；修改需执行电脑在线。</p>
    <p v-if="loading" class="hint">正在读取个人收藏…</p>
    <view v-if="error" class="notice error" role="alert">{{error}}<button class="text-button" :disabled="saving||offline" @click="reload(true)">重新读取</button></view>
    <view v-if="message" class="hint" role="status">{{message}}</view>
    <view v-if="activeTab==='all'" class="library-grid">
      <view v-for="seat in allSeats" :key="seat.id" class="library-seat">
        <button class="seat-choice" :class="{chosen:selected.includes(seat.id)}" :aria-pressed="selected.includes(seat.id)" :disabled="disabled" @click="$emit('pick',seat.id)"><strong>{{seat.name}}</strong><text>{{seat.location}}</text></button>
        <button class="favorite-button" :aria-label="(isFavorite(seat.id)?'取消收藏 ':'收藏 ')+seat.name" :aria-pressed="isFavorite(seat.id)" :disabled="metadataUnavailable" @click="toggleFavorite(seat.id)">{{isFavorite(seat.id)?'★':'☆'}}</button>
      </view>
      <p v-if="!allSeats.length" class="hint">没有匹配座位</p>
    </view>
    <view v-else-if="activeTab==='favorites'" class="library-list">
      <view v-for="seat in favorites" :key="seat.id" class="library-row"><view><strong>{{seat.name}}</strong><p>{{seat.location}}</p><view class="tag-list"><text v-for="tag in seat.tags" :key="tag">{{tag}}</text></view></view><view class="row-buttons"><button class="text-button" :disabled="disabled||!seat.available" @click="$emit('pick',seat.id)">选用</button><button class="text-button" :disabled="metadataUnavailable" @click="openTags({seat_id:seat.id,tags:seat.tags})">标签</button><button class="text-button" :disabled="metadataUnavailable" @click="toggleFavorite(seat.id)">取消收藏</button></view></view>
      <p v-if="!favorites.length" class="hint">暂无匹配收藏，点座位旁的 ☆ 收藏。</p>
    </view>
    <view v-else class="library-list">
      <button class="button secondary group-create" :disabled="metadataUnavailable||!selected.length" @click="openGroup()">把当前选座存为分组</button>
      <view v-for="group in groups" :key="group.id" class="library-row"><view><strong>{{group.name}}</strong><p>{{group.seat_ids.map(seatName).join(' → ')}}</p></view><view class="row-buttons"><button class="text-button" :disabled="disabled" @click="applyGroup(group)">使用此组</button><button class="text-button" :disabled="metadataUnavailable" @click="openGroup(group)">编辑</button><button class="text-button" :disabled="metadataUnavailable" @click="deleteGroup(group)">删除</button></view></view>
      <p v-if="!groups.length" class="hint">暂无匹配分组，选好座位后可保存。</p>
    </view>
    <view v-if="currentEditor" class="library-editor" aria-label="编辑个人收藏">
      <h3>{{currentEditor.kind==='tags'?'自定义标签 · '+seatName(currentEditor.seat_id):currentEditor.creating?'新建偏好分组':'编辑偏好分组'}}</h3>
      <template v-if="currentEditor.kind==='tags'"><input class="input" :value="currentEditor.text" aria-label="自定义标签" placeholder="用逗号分隔，最多5个标签" @input="changeEditor('text',$event.detail.value)"/></template>
      <template v-else><input class="input" :value="currentEditor.name" aria-label="分组名称" placeholder="自己填写分组名称" maxlength="80" @input="changeEditor('name',$event.detail.value)"/><view v-for="(id,index) in currentEditor.seat_ids" :key="id" class="group-seat"><strong>{{index===0?'首选':'备选'+index}} · {{seatName(id)}}</strong><view><button class="text-button" :disabled="index===0||saving" aria-label="上移分组座位" @click="moveEditorSeat(index,-1)">↑</button><button class="text-button" :disabled="index===currentEditor.seat_ids.length-1||saving" aria-label="下移分组座位" @click="moveEditorSeat(index,1)">↓</button></view></view><button class="text-button" :disabled="metadataUnavailable||!selected.length" @click="replaceEditorSeats">换成当前选座顺序</button></template>
      <view class="editor-actions"><button class="button secondary" :disabled="saving" @click="cancelEditor">取消编辑</button><button class="button" :disabled="metadataUnavailable" @click="saveEditor">{{saving?'保存中…':'保存'+(currentEditor.kind==='tags'?'标签':'分组')}}</button></view>
      <p v-if="currentEditor.dirty" class="hint" role="status">先保存或取消这次编辑，再应用选座。</p>
    </view>
    <details class="context-help"><summary>收藏与分组说明</summary><p>仅当前账号可见，标签由你填写。分组按首选、备选顺序填入选座，应用并保存后才改变计划。收藏不代表实时空位。</p></details>
  </section>
</template>
<script setup>
import {ref,computed,watch,onMounted,onBeforeUnmount} from 'vue';
import * as api from '@/services/local_api.js';
import {parseSeatTags,librarySnapshot,groupSeatSnapshot,favoriteEntries} from '@/services/seat_preferences.mjs';
const props=defineProps({account:{type:Object,required:true},seats:{type:Array,default:()=>[]},visibleSeats:{type:Array,default:null},selected:{type:Array,default:()=>[]},search:{type:String,default:''},sessionKey:Number,disabled:Boolean,offline:Boolean,editor:{type:Object,default:null}});
const emit=defineEmits(['pick','apply','saved','update:editor']);
const tabs=[{key:'all',label:'全部座位'},{key:'favorites',label:'我的收藏'},{key:'groups',label:'偏好分组'}];
const activeTab=ref(props.editor?.account_id===props.account.id?(props.editor.kind==='tags'?'favorites':'groups'):'all');
const data=ref(librarySnapshot(props.account)),loading=ref(false),saving=ref(false),error=ref(''),message=ref('');let sequence=0,mounted=false;
const currentEditor=computed(()=>props.editor?.account_id===props.account.id?props.editor:null);
const metadataUnavailable=computed(()=>props.disabled||props.offline||loading.value||saving.value||!props.account?.id);
const allSeats=computed(()=>props.visibleSeats||props.seats.filter(s=>(s.name+' '+s.location).toLowerCase().includes(props.search.toLowerCase())).slice(0,80));
const favorites=computed(()=>favoriteEntries(data.value,props.seats,props.search));
const groups=computed(()=>data.value.groups.filter(g=>(g.name+' '+g.seat_ids.map(seatName).join(' ')).toLowerCase().includes(props.search.trim().toLowerCase())));
const seatName=id=>props.seats.find(s=>s.id===id)?.name||id;
const isFavorite=id=>data.value.favorites.some(s=>s.seat_id===id);
const identity=()=>({id:props.account.id,epoch:props.sessionKey,ticket:sequence});
const current=token=>token.id===props.account.id&&token.epoch===props.sessionKey&&token.ticket===sequence;
function editingBlocked(){if(!currentEditor.value?.dirty)return false;error.value='请先保存或取消当前收藏编辑';return true;}
function reset(){sequence++;data.value=librarySnapshot(props.account);loading.value=false;saving.value=false;error.value='';message.value='';activeTab.value=currentEditor.value?(currentEditor.value.kind==='tags'?'favorites':'groups'):'all';if(mounted)reload();}
watch([()=>props.account.id,()=>props.sessionKey],reset,{flush:'sync'});
onMounted(()=>{mounted=true;reload();});onBeforeUnmount(()=>{mounted=false;sequence++;});
function checkedResponse(result,id){if(result?.account_id!==id||!Number.isInteger(result.revision)||!Array.isArray(result.favorites)||!Array.isArray(result.groups))throw new Error('收藏响应异常，请重新读取');return result;}
function rebaseEditor(result){
  const editor=currentEditor.value;if(!editor)return;
  const baseline=editor.baseline;let next={...editor,base_revision:result.revision};
  if(editor.kind==='tags'){
    const favorite=result.favorites.find(s=>s.seat_id===editor.seat_id);
    if(!favorite)throw new Error('该收藏已被取消，已保留输入，请取消编辑后重新选择');
    if(baseline&&editor.text===baseline.tags.join('，'))next.text=favorite.tags.join('，');
    next.baseline={tags:[...favorite.tags]};
  }else if(editor.creating){next.baseline={name:'',seat_ids:[...editor.seat_ids]};}
  else{
    const group=result.groups.find(g=>g.id===editor.id);
    if(!group)throw new Error('该分组已被删除，已保留输入，请取消编辑后重新选择');
    if(baseline&&editor.name===baseline.name)next.name=group.name;
    if(baseline&&JSON.stringify(editor.seat_ids)===JSON.stringify(baseline.seat_ids))next.seat_ids=[...group.seat_ids];
    next.baseline={name:group.name,seat_ids:[...group.seat_ids]};
  }
  emit('update:editor',next);message.value='已读取最新内容，保留输入，请核对后保存。';
}
async function reload(rebase=false){
  if(props.offline||saving.value||!props.account?.id)return;sequence++;const token=identity();loading.value=true;error.value='';
  try{const result=await api.getSeatPreferences(token.id);if(current(token)&&result.revision>=data.value.revision){data.value=checkedResponse(result,token.id);if(rebase)rebaseEditor(result);}}
  catch(e){if(current(token))error.value=e.message;}finally{if(current(token))loading.value=false;}
}
async function persist(candidate,revision=data.value.revision){
  if(metadataUnavailable.value)return false;sequence++;const token=identity();
  saving.value=true;error.value='';message.value='';
  try{
    const preferences=JSON.parse(JSON.stringify({favorites:candidate.favorites,groups:candidate.groups}));
    const result=await api.saveSeatPreferences(token.id,preferences,revision);if(!current(token))return false;
    data.value=checkedResponse(result,token.id);emit('saved',result);message.value='已保存。';return true;
  }catch(e){if(current(token))error.value=e.message;return false;}finally{if(current(token))saving.value=false;}
}
async function toggleFavorite(id){
  if(metadataUnavailable.value||editingBlocked())return;const candidate=JSON.parse(JSON.stringify(data.value));
  if(isFavorite(id))candidate.favorites=candidate.favorites.filter(s=>s.seat_id!==id);
  else{if(!props.seats.some(s=>s.id===id)){error.value='该座位已不在目录中';return;}candidate.favorites.push({seat_id:id,tags:[]});}
  return persist(candidate);
}
function openTags(favorite){if(metadataUnavailable.value||editingBlocked())return;activeTab.value='favorites';error.value='';emit('update:editor',{account_id:props.account.id,kind:'tags',seat_id:favorite.seat_id,text:favorite.tags.join('，'),base_revision:data.value.revision,baseline:{tags:[...favorite.tags]},dirty:false});}
function openGroup(group){
  if(metadataUnavailable.value||editingBlocked())return;error.value='';activeTab.value='groups';
  try{const ids=group?[...group.seat_ids]:groupSeatSnapshot({seat_ids:props.selected},props.seats);emit('update:editor',{account_id:props.account.id,kind:'group',id:group?.id||crypto.randomUUID(),name:group?.name||'',seat_ids:ids,creating:!group,base_revision:data.value.revision,baseline:{name:group?.name||'',seat_ids:[...ids]},dirty:false});}
  catch(e){error.value=e.message;}
}
function changeEditor(key,value){if(saving.value||!currentEditor.value)return;emit('update:editor',{...currentEditor.value,[key]:value,dirty:true});}
function moveEditorSeat(index,delta){const ids=[...(currentEditor.value?.seat_ids||[])],target=index+delta;if(target<0||target>=ids.length)return;[ids[index],ids[target]]=[ids[target],ids[index]];changeEditor('seat_ids',ids);}
function replaceEditorSeats(){try{changeEditor('seat_ids',groupSeatSnapshot({seat_ids:props.selected},props.seats));}catch(e){error.value=e.message;}}
async function saveEditor(){
  const editor=currentEditor.value;if(!editor||metadataUnavailable.value)return;
  const token=identity(),candidate=JSON.parse(JSON.stringify(data.value));
  try{
    if(!Number.isInteger(editor.base_revision)||editor.base_revision<0)throw new Error('请先重新读取收藏与分组，再核对保存');
    if(editor.kind==='tags'){
      const favorite=candidate.favorites.find(s=>s.seat_id===editor.seat_id);if(!favorite)throw new Error('该收藏已变化，请重新读取后编辑');favorite.tags=parseSeatTags(editor.text);
    }else{
      const name=editor.name.trim();if(!name||Array.from(name).length>40)throw new Error('分组名称须为1–40个字符');
      const group={id:editor.id,name,seat_ids:groupSeatSnapshot(editor,props.seats)};
      if(editor.creating)candidate.groups.push(group);
      else{const index=candidate.groups.findIndex(g=>g.id===editor.id);if(index<0)throw new Error('该分组已变化，请重新读取后编辑');candidate.groups[index]=group;}
    }
    const saved=await persist(candidate,editor.base_revision);if(saved&&token.id===props.account.id&&token.epoch===props.sessionKey)emit('update:editor',null);
  }catch(e){if(current(token))error.value=e.message;}
}
async function cancelEditor(){
  if(saving.value)return;const token=identity();if(currentEditor.value?.dirty){const discard=await new Promise(resolve=>uni.showModal({title:'有未保存的收藏编辑',content:'是否放弃当前输入？',confirmText:'放弃修改',cancelText:'继续编辑',success:r=>resolve(r.confirm)}));if(!discard||!current(token))return;}emit('update:editor',null);
}
async function deleteGroup(group){
  if(metadataUnavailable.value||editingBlocked())return;const token=identity();const confirmed=await new Promise(resolve=>uni.showModal({title:'删除分组？',content:'删除“'+group.name+'”，已保存的预约计划保持原样。',success:r=>resolve(r.confirm)}));if(!confirmed||!current(token))return;
  return persist({...data.value,groups:data.value.groups.filter(g=>g.id!==group.id)});
}
function applyGroup(group){
  if(props.disabled)return;error.value='';
  try{const own=data.value.groups.find(g=>g.id===group.id);if(!own)throw new Error('该分组已变化，请重新读取');emit('apply',groupSeatSnapshot(own,props.seats));}
  catch(e){error.value=e.message;}
}
</script>
<style scoped>
.seat-library{margin:18px 0}.library-tabs{display:flex;gap:8px;border-bottom:1px solid var(--line);margin-bottom:16px}.library-tabs button{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;padding:12px 8px;font-size:14px;color:var(--muted)}.library-tabs button.selected{color:var(--green);border-bottom-color:var(--green)}.library-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;max-height:390px;overflow:auto}.library-seat{display:flex;align-items:stretch;min-width:0;border:1px solid var(--line);border-radius:8px;overflow:hidden}.seat-choice{flex:1;min-width:0;background:white;border:0;border-radius:0;text-align:left;padding:12px;font-size:13px}.seat-choice strong,.seat-choice text{display:block}.seat-choice text{margin-top:6px;font-size:11px;color:var(--muted)}.seat-choice.chosen{background:var(--tint);color:var(--green)}.favorite-button{background:var(--wash);border:0;border-radius:0;padding:8px;font-size:21px;color:var(--green)}.library-list{max-height:360px;overflow:auto}.library-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding:14px 0;font-size:13px}.library-row p{font-size:12px;line-height:1.7;margin-top:6px;color:var(--muted)}.row-buttons{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px;flex:none}.row-buttons .text-button{font-size:12px;padding:7px}.tag-list{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.tag-list text{font-size:11px;background:var(--tint);border-radius:4px;padding:3px 6px;overflow-wrap:anywhere}.group-create{font-size:13px;padding:10px 14px;min-height:40px;margin-bottom:10px}.library-editor{border:1px solid var(--line);border-radius:8px;padding:16px;margin-top:18px;background:var(--wash)}.library-editor h3{font-size:14px;margin-bottom:14px}.editor-actions{display:flex;gap:10px;margin-top:16px}.editor-actions .button{font-size:13px;min-height:42px;padding:10px 14px;flex:1}.group-seat{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;border-bottom:1px solid var(--line);padding:9px 0}.library-editor .hint{margin-top:10px}@media(max-width:599px){.library-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.library-row{flex-wrap:wrap}.row-buttons{width:100%;justify-content:flex-start}.library-tabs{gap:3px}.library-tabs button{font-size:13px;padding:11px 7px}}
.library-tabs .tab-count{display:inline-block;margin-left:5px;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted)}.library-tabs button{min-height:44px}.library-row>view:first-child{min-width:0;overflow-wrap:anywhere}.seat-choice strong,.seat-choice text,.group-seat strong{overflow-wrap:anywhere}.favorite-button{min-width:44px;min-height:44px}.row-buttons .text-button,.group-seat .text-button{min-height:44px;min-width:44px}.library-editor .editor-actions{flex-wrap:wrap}
</style>
