<template>
  <view class="charts">
    <view class="panel chart-card">
      <h2>每日预约结果</h2><p class="muted small">点击日期查看数值</p>
      <svg viewBox="0 0 640 200" role="img" aria-label="每日成功、失败、跳过和未结束任务堆叠图">
        <line x1="24" y1="170" x2="616" y2="170" stroke="#dce6df"/>
        <g v-for="(row,i) in trend" :key="row.date">
          <rect v-for="(s,j) in series" :key="s.key" :x="30+i*584/Math.max(trend.length,1)" :y="stackY(row,j)" :width="Math.max(3,584/Math.max(trend.length,1)-5)" :height="(row[s.key]||0)/maxCount*140" :fill="s.color"/>
        </g>
      </svg>
      <view class="legend"><text v-for="s in series" :key="s.key"><i :style="{background:s.color}"></i>{{s.label}}</text></view>
      <view class="date-strip"><button v-for="(row,i) in trend" :key="row.date" :class="{chosen:i===selectedIndex}" @click="picked=i">{{row.date.slice(5)}}</button></view>
      <view class="readout" v-if="selected">{{selected.date}} · {{selected.total?'共 '+selected.total+' 条':'无执行记录'}}<text v-for="s in series" :key="s.key">{{s.label}} {{selected[s.key]||0}}</text></view>
    </view>
    <view class="panel chart-card">
      <h2>首次请求发起延迟</h2><p class="muted small">从放号到本机发起请求 · 毫秒</p>
      <view v-if="!delays.length" class="empty compact">{{date||'所选放号日'}}无有效首发计时 · 缺失 {{missing}} 条</view>
      <view v-else class="delay-chart" role="group" aria-label="各账号首次请求发起延迟，单位毫秒">
        <button v-for="row in delays.slice(0,20)" :key="row.id" class="delay-row" @click="delayId=row.id">
          <text class="delay-name">{{row.name}} · {{row.student_id.slice(-4)}}</text>
          <view class="track"><view class="zero" :style="{left:zero+'%'}"></view><view class="bar" :class="{early:row.value_ms<0}" :style="delayStyle(row.value_ms)"></view></view>
          <text>{{formatMs(row.value_ms)}}{{row.value_ms<0?' · 提前':''}}</text>
        </button>
        <view v-if="delayDetail" class="readout">{{delayDetail.name}} · {{delayDetail.student_id}} · {{formatMs(delayDetail.value_ms)}}</view>
        <p class="muted small">有效 {{delayCount}} 条 · 缺失 {{missing}} 条{{delayCount>20?' · 图中展示前20条':''}}</p>
      </view>
    </view>
    <view class="panel chart-card wide">
      <h2>请求往返耗时趋势</h2><p class="muted small">含网络与学校处理耗时</p>
      <view v-if="!hasTimings" class="empty compact">范围内暂无请求耗时数据</view>
      <template v-else>
        <svg viewBox="0 0 640 200" role="img" aria-label="请求往返耗时中位数和P95趋势，缺失日期断开">
          <line x1="24" y1="170" x2="616" y2="170" stroke="#dce6df"/>
          <g v-for="metric in metrics" :key="metric.key">
            <polyline v-for="(points,i) in lineSegments(trend,metric.key,maxMs)" :key="i" :points="points" fill="none" :stroke="metric.color" stroke-width="2.5"/>
            <template v-for="(row,i) in trend" :key="row.date"><circle v-if="Number.isFinite(row.request?.[metric.key])" :cx="24+i*592/Math.max(trend.length-1,1)" :cy="170-row.request[metric.key]/maxMs*140" r="4" :fill="metric.color"/></template>
          </g>
        </svg>
        <view class="legend"><text v-for="m in metrics" :key="m.key"><i :style="{background:m.color}"></i>{{m.label}}</text><text>纵轴上限 {{formatMs(maxMs)}}</text></view>
        <view class="readout" v-if="selected">{{selected.date}} · 有效请求 {{selected.request.count}} 次 · 中位数 {{formatMs(selected.request.p50)}} · P95 {{formatMs(selected.request.p95)}}</view>
      </template>
      <details class="chart-method"><summary>计时说明</summary><p>首次请求延迟以本机发起时间计，非学校接收时间。仅统计有效请求；缺失计时不画成0，小样本P95可能等于最大值。</p></details>
    </view>
  </view>
</template>
<script setup>
import {computed,ref} from 'vue';
import {formatMs,lineSegments} from '@/services/admin_dashboard.mjs';
const props=defineProps({date:String,trend:{type:Array,default:()=>[]},delays:{type:Array,default:()=>[]},missing:{type:Number,default:0},delayCount:{type:Number,default:0}});
const picked=ref(-1),delayId=ref(null);
const delayDetail=computed(()=>props.delays.find(row=>row.id===delayId.value)||null);
const series=[{key:'success',label:'已核实成功',color:'#2b7960'},{key:'failed',label:'失败',color:'#bc5950'},{key:'skipped',label:'跳过',color:'#d5a44d'},{key:'uncertain',label:'待核对',color:'#8b76aa'},{key:'pending',label:'未结束',color:'#a5b4bc'}];
const metrics=[{key:'p50',label:'中位数',color:'#2b7960'},{key:'p95',label:'P95',color:'#b3863c'}];
const maxCount=computed(()=>Math.max(1,...props.trend.map(r=>r.total)));
const maxMs=computed(()=>Math.max(1,...props.trend.map(r=>r.request?.p95||0)));
const hasTimings=computed(()=>props.trend.some(r=>r.request?.count));
const selectedIndex=computed(()=>picked.value<0?props.trend.length-1:Math.min(picked.value,props.trend.length-1));
const selected=computed(()=>props.trend[selectedIndex.value]);
const stackY=(row,j)=>170-series.slice(0,j+1).reduce((n,s)=>n+(row[s.key]||0),0)/maxCount.value*140;
const low=computed(()=>Math.min(0,...props.delays.map(r=>r.value_ms)));
const high=computed(()=>Math.max(1,...props.delays.map(r=>r.value_ms)));
const zero=computed(()=>-low.value/(high.value-low.value)*100);
const delayStyle=value=>({left:(Math.min(0,value)-low.value)/(high.value-low.value)*100+'%',width:Math.max(.6,Math.abs(value)/(high.value-low.value)*100)+'%'});
</script>
<style scoped>
.chart-method{font-size:12px;color:var(--muted);margin-top:12px;border-top:1px solid var(--line)}.chart-method summary{min-height:44px;display:flex;align-items:center;cursor:pointer}.chart-method p{line-height:1.7}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:18px}.chart-card{min-width:0}.chart-card p{margin-top:8px;line-height:1.6}.wide{grid-column:1/-1}svg{display:block;width:100%;max-height:235px;margin:15px 0 0}.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:var(--muted)}.legend i{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:4px}.date-strip{display:flex;overflow-x:auto;gap:3px;margin-top:12px}.date-strip button{min-width:49px;min-height:44px;border:0;border-radius:7px;background:var(--wash);font-size:11px;padding:5px;cursor:pointer}.date-strip button.chosen{background:var(--green);color:white}.readout{font-size:12px;line-height:1.8;padding-top:12px;overflow-wrap:anywhere}.readout text{margin-left:8px}.delay-row{display:grid;grid-template-columns:100px 1fr 82px;align-items:center;gap:8px;width:100%;background:none;border:0;padding:12px 0;font-size:11px;text-align:left;min-height:44px;cursor:pointer}.delay-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{position:relative;height:10px;background:#eef3ee;border-radius:4px}.zero{position:absolute;height:18px;top:-4px;border-left:1px solid #9dac9e}.bar{position:absolute;height:10px;background:#41866c;border-radius:3px;max-width:100%}.bar.early{background:#bd5b51}.delay-row:hover{background:var(--wash)}@media(max-width:800px){.charts{grid-template-columns:1fr}.wide{grid-column:auto}}@media(prefers-reduced-motion:reduce){*{transition:none!important}}
.chart-method summary::after{content:"⌄";margin-left:auto;font-size:15px}.chart-method[open]>summary::after{transform:rotate(180deg)}
</style>
