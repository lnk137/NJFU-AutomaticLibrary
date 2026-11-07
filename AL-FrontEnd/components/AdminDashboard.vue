<template>
  <view v-if="role==='root'" class="admin-dashboard">
    <view class="dashboard-head"><view><h1>运行看板</h1><p class="muted small">按放号日统计 · 北京时间</p></view>
      <view class="dashboard-filters">
        <component :is="'input'" class="filter dashboard-date" type="date" :value="filters.date" aria-label="统计放号日期" @change="$emit('filters',{date:$event.target.value})"/>
        <select class="filter" aria-label="统计范围" :value="filters.days||14" @change="$emit('filters',{days:Number($event.target.value)})"><option :value="7">近7天</option><option :value="14">近14天</option><option :value="30">近30天</option></select>
        <select class="filter" aria-label="任务类型" :value="filters.kind||'scheduled'" @change="$emit('filters',{kind:$event.target.value})"><option value="scheduled">定时计划</option><option value="all">全部任务</option><option value="manual">当天手动</option></select>
        <button class="button ghost small-button" :disabled="loading" @click="$emit('refresh')">{{loading?'读取中…':'刷新统计'}}</button>
      </view>
    </view>
    <slot name="monitor"/>
    <view v-if="error" class="notice error" role="alert">{{error}}<text v-if="data"> · 以下保留上次结果：{{time(data.generated_at)}}</text></view>
    <view v-else-if="stale&&data" class="notice" role="status">正在更新，以下为上次结果：{{time(data.generated_at)}}</view>
    <view v-if="!data" class="empty panel">{{loading?'正在读取管理数据…':'暂无统计数据，请刷新'}}</view>
    <template v-else>
      <SchedulerStatus v-if="data.runtime.execution_scope==='dual_node'||!data.runtime.clock_ok||!data.runtime.allow_live||data.runtime.last_error||data.runtime.scheduler_warning" :status="data.runtime"/><view class="runtime-bar"><text>● 调度 {{data.runtime.scheduler_running?'运行中':'未运行'}}</text><text>时钟 {{data.runtime.clock_ok?'正常':'待检查'}}</text><text>自动执行 {{data.runtime.allow_live?'开启':'关闭'}}</text></view>
      <view class="data-stamp">数据日期 {{data.date}} · {{kindNames[data.kind]}} · 更新 {{time(data.generated_at)}}<text v-if="Number.isInteger(data.runtime?.capacity)&&data.runtime.capacity>0"> · 提交并发上限 {{data.runtime.capacity}}</text></view>
      <view v-if="data.runtime.executor==='cpp'" class="executor-bar"><text>快速执行 · {{executorNames[data.runtime.executor_state]||'待检查'}}</text><button class="text-button" :disabled="executorBusy" @click="$emit('executor-control',data.runtime.executor_state==='ready'?'pause':data.runtime.executor_state==='paused'?'resume':'recover')">{{executorBusy?'处理中…':data.runtime.executor_state==='ready'?'暂停自动预约':data.runtime.executor_state==='paused'?'继续自动预约':'恢复连接'}}</button></view>
      <view class="metric-grid"><button v-for="item in counts" :key="item.key" class="metric" :class="{selected:selectedStatus===item.key}" @click="selectedStatus=item.key">
        <text>{{item.label}}</text><strong>{{item.value}}</strong><text class="muted small">{{item.hint}}</text>
      </button></view>
      <view class="summary-line">已结束成功率 <b>{{data.summary.success_rate===null?'—':data.summary.success_rate+'%'}}</b> · 成功 {{data.summary.success}} / 已结束 {{data.summary.closed}}</view>
      <section v-if="data.seat_conflicts" class="panel seat-conflicts" aria-label="内部撞座提醒">
        <view class="section-head"><h2>内部撞座提醒 · {{data.seat_conflicts.items.length}}{{data.seat_conflicts.has_more?'+':''}}</h2></view>
        <p class="hint">{{data.seat_conflicts.start_date}} 至 {{data.seat_conflicts.end_date}} · 已检查 {{data.seat_conflicts.checked_accounts}} 个计划</p>
        <p v-if="data.seat_conflicts.unchecked_accounts" class="hint attention" role="status">{{data.seat_conflicts.unchecked_accounts}} 个计划未参与检查，请核对配置或权限。</p>
        <p v-if="!data.seat_conflicts.items.length" class="empty compact">未发现已检查计划的座位重叠。</p>
        <view v-for="c in data.seat_conflicts.items.slice(0,showAllConflicts?50:3)" :key="c.target_date+'-'+c.seat.id+'-'+c.accounts.map(a=>a.account_id).join('-')" class="seat-conflict-row">
          <view class="seat-conflict-head"><strong>{{c.target_date}} · {{c.seat.name}} <span class="muted">{{c.seat.location}}</span></strong><text class="pill">{{c.kind==='preferred'?'首选重叠':'含备选，可能重叠'}}</text><text>重叠 {{c.begin}}–{{c.end}}</text></view>
          <view v-for="a in c.accounts" :key="a.account_id" class="seat-conflict-person"><view><b>{{a.name}}</b> <text class="muted">{{a.student_id}} · {{a.rank===1?'首选':'备选'+(a.rank-1)}} · {{a.begin}}–{{a.end}}</text></view><button class="text-button" @click="$emit('open-account',a.account_id)">查看计划</button></view>
        </view>
        <p v-if="data.seat_conflicts.has_more" class="hint">最多展示50条，仍有其他重叠。</p>
        <button v-if="data.seat_conflicts.items.length>3" class="text-button more-alerts" @click="showAllConflicts=!showAllConflicts">{{showAllConflicts?'收起提醒':'展开其余 '+(data.seat_conflicts.items.length-3)+' 条'}}</button>
        <details class="context-help"><summary>检查说明</summary><p>按已保存且尚未放号的计划检查。含备选表示可能竞争，预约结果以学校回执为准。这里只提示，不自动调整计划。</p></details>
      </section>
      <view class="next-attention"><view class="panel next-panel"><view class="section-head"><h2>下一轮计划</h2></view><view class="population"><view><b>{{data.population.enabled}}</b><text>定时开启</text></view><view><b>{{data.population.checked}}</b><text>本地检查通过</text></view><view :class="{attention:data.population.attention}"><b>{{data.population.attention}}</b><text>需处理</text></view></view><view v-if="nextRound" class="next-preview"><text>下次放号</text><strong>{{time(nextRound.release_at)}}</strong><span>使用 {{nextRound.target_date}} · {{nextRound.begin}}–{{nextRound.end}}</span></view><p v-else class="hint">未安排下一轮执行</p></view>
      <view class="panel"><view class="section-head"><h2>需要关注 · {{data.alert_total}}</h2><text v-if="data.alert_total>50" class="muted small">前50项</text></view>
        <view v-if="!data.alerts.length" class="empty compact">暂无待处理事项</view>
        <view v-for="(a,i) in data.alerts.slice(0,showAllAlerts?50:3)" :key="a.id||i" class="alert-row"><view><b>{{a.name||'本机后台'}} <text class="pill">{{statusNames[a.type]||'配置提醒'}}</text><text v-if="a.type==='dual_node'&&typeof a.active==='boolean'" class="pill">{{a.active?'需处理':'已恢复'}}</text></b><text class="row-sub">{{a.check==='ready'?'本地检查通过':a.message}}</text></view><button v-if="a.account_id" class="button ghost small-button" @click="$emit('open-account',a.account_id)">查看账号</button></view>
      <button v-if="data.alerts.length>3" class="text-button more-alerts" @click="showAllAlerts=!showAllAlerts">{{showAllAlerts?'收起关注事项':'查看其余 '+(data.alerts.length-3)+' 项'}}</button></view></view>
      <details class="stats-explanation"><summary>统计说明</summary><p>成功率按已结束任务计算，包含失败与跳过；待核对、未结束不计入。配置检查通过后，学校登录在放号前验证；预约成功以学校记录为准。</p></details><view class="chart-heading"><h2>统计图表</h2><text class="muted small">{{data.date}} · {{kindNames[data.kind]}}</text></view>
      <AdminCharts :date="data.date" :trend="data.trend" :delays="data.delays" :missing="data.delay_missing" :delay-count="data.delay_count"/>
      <view class="panel account-status-panel"><view class="section-head"><h2>账号与计划</h2><button class="button ghost small-button" @click="accountOnlyEnabled=!accountOnlyEnabled">{{accountOnlyEnabled?'显示全部账号':'仅看开启计划'}}</button></view><p class="hint">有效准入 {{data.population.authorized}} · 已保存账号 {{data.population.saved}}</p>
        <input class="input" v-model="accountSearch" placeholder="搜索姓名、学号或备注" aria-label="搜索账号状态"/>
        <view class="accounts-table"><view class="account-head"><text>账号</text><text>授权与登录</text><text>下一次放号 / 实际使用</text><text>座位与检查</text></view>
          <view v-if="!accounts.length" class="empty compact">暂无匹配账号</view>
          <view class="account-row" v-for="a in accounts" :key="a.student_id">
            <view><b>{{a.name}}</b><text class="row-sub">{{a.student_id}}</text><button v-if="a.account_id" class="link" @click="$emit('open-account',a.account_id)">查看计划与记录 →</button></view>
            <view><text class="pill" :class="a.authorized?'green':'red'">{{a.authorized?'已授权':'未授权'}}</text><text class="row-sub">{{a.saved?'账号已保存':'尚未登录填写'}}</text><text class="row-sub">网页登录 {{time(a.last_login)}}</text><text class="row-sub">学校验证 {{time(a.verified_at)}}</text></view>
            <view v-if="a.next_run"><b>{{time(a.next_run.release_at)}}</b><text class="row-sub">使用 {{a.next_run.target_date}} {{a.next_run.begin}}—{{a.next_run.end}}</text></view><view v-else class="muted">未安排下一轮</view>
            <view><text>{{a.next_run?.seats.map(s=>s.name).join(' → ')||'—'}}</text><text class="row-sub" :class="{attention:a.check==='attention'}">{{a.check==='ready'?'本地检查通过':a.message}}</text></view>
          </view>
        </view>
      </view>

      <view class="panel"><view class="section-head"><h2>执行明细 · {{data.date}}</h2><view class="actions"><button class="text-button" @click="$emit('open-history')">全部执行历史</button><button class="button ghost small-button" @click="selectedStatus='all'">全部状态</button></view></view>
        <view class="hint">{{selectedStatus==='all'?'全部状态':statusNames[selectedStatus]}} · 共{{data.runs_total}}条 · 最近200条</view>
        <view v-if="!runs.length" class="empty compact">当前筛选下无执行记录</view>
        <details v-for="r in runs" :key="r.id" class="run-detail"><summary><b>{{r.name}}</b><text class="pill">{{statusNames[r.status]||'未结束'}}</text><text>{{r.target_date}} · {{r.begin}}—{{r.end}}</text></summary>
          <p>{{r.message}}</p><view class="phase-grid"><view v-for="stage in ['prepare','warm','submit']" :key="stage"><b>{{phaseNames[stage]}}</b><text class="row-sub">总耗时 {{formatMs(r.phases?.[stage]?.elapsed_ms)}}</text><text class="row-sub">线程排队 {{formatMs(r.phases?.[stage]?.queue_ms)}}</text><text class="row-sub">账号锁等待 {{formatMs(r.phases?.[stage]?.account_lock_wait_ms)}}</text></view></view>
          <view v-for="a in r.attempts" :key="a.index" class="row-sub">尝试 {{a.index}} · {{a.outcome}} · 请求 {{formatMs(a.timings?.request_ms)}} {{a.check==='ready'?'本地检查通过':a.message}}</view>
          <button class="link" @click="$emit('open-account',r.account_id)">查看账号配置 →</button>
        </details>
      </view>
    </template>
  </view>
</template>
<script setup>
import {computed,ref} from 'vue';
import AdminCharts from '@/components/AdminCharts.vue';
import SchedulerStatus from '@/components/SchedulerStatus.vue';
import {formatMs} from '@/services/admin_dashboard.mjs';
const props=defineProps({role:String,data:Object,filters:{type:Object,default:()=>({})},loading:Boolean,error:String,stale:Boolean,executorBusy:Boolean});
defineEmits(['filters','refresh','open-account','open-history','executor-control']);
const executorNames={ready:'就绪',recovering:'恢复中',paused:'已暂停',blocked:'待处理'};
const selectedStatus=ref('all'),accountSearch=ref(''),accountOnlyEnabled=ref(false),showAllAlerts=ref(false),showAllConflicts=ref(false);
const nextRound=computed(()=>(props.data?.accounts||[]).map(a=>a.next_run).filter(Boolean).sort((a,b)=>a.due_at-b.due_at)[0]||null);
const kindNames={scheduled:'定时计划',manual:'当天手动',all:'全部任务'};
const statusNames={all:'全部',success:'已核实成功',failed:'失败',skipped:'已跳过',uncertain:'待核对',pending:'未结束',queued:'等待放号',preparing:'提前准备',submitting:'执行中',dual_node:'双机告警'};
const phaseNames={prepare:'提前登录',warm:'只读预热',submit:'提交执行'};
const time=value=>value?String(value).replace('T',' ').slice(0,19):'暂无记录';
const counts=computed(()=>[['all','已记录任务','total'],['success','已核实成功','success'],['failed','失败','failed'],['skipped','已跳过','skipped'],['uncertain','待核对','uncertain'],['pending','未结束','pending']].map(([key,label,field])=>({key,label,value:props.data?.summary[field]||0,hint:'点击筛选执行明细'})));
const accounts=computed(()=>(props.data?.accounts||[]).filter(a=>!accountOnlyEnabled.value||a.plan_enabled).filter(a=>(a.name+' '+a.student_id).toLowerCase().includes(accountSearch.value.trim().toLowerCase())));
const runs=computed(()=>(props.data?.runs||[]).filter(r=>selectedStatus.value==='all'||r.status===selectedStatus.value||selectedStatus.value==='pending'&&['queued','preparing','submitting'].includes(r.status)));
</script>
<style scoped>
.executor-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--muted)}.executor-bar .text-button{min-height:44px}
.seat-conflicts>.hint{margin-top:10px}.seat-conflict-row{border-top:1px solid var(--line);padding:16px 0;font-size:13px}.seat-conflict-row:first-of-type{margin-top:16px}.seat-conflict-head{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px;margin-bottom:8px}.seat-conflict-head strong{font-weight:600}.seat-conflict-head strong span{font-size:12px;font-weight:400}.seat-conflict-person{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0}.seat-conflict-person>view{overflow-wrap:anywhere;line-height:1.8}.seat-conflict-person .text-button{flex:none;padding:8px;font-size:12px}@media(max-width:599px){.seat-conflict-person{align-items:flex-start}.seat-conflict-person .muted{display:block}}
.stats-explanation{margin-top:-12px;font-size:12px;color:var(--muted)}.stats-explanation summary{cursor:pointer;display:flex;align-items:center;min-height:44px}.stats-explanation p{line-height:1.7;max-width:680px}.admin-dashboard{display:grid;gap:24px}.dashboard-head{display:flex;justify-content:space-between;gap:20px;align-items:center;flex-wrap:wrap}.dashboard-head p{margin-top:8px;font-size:12px}.dashboard-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.dashboard-filters .filter{background:#fff;min-height:46px;min-width:145px;font-size:14px}.runtime-bar{display:flex;gap:20px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);padding-bottom:22px}.runtime-bar text:first-child{color:var(--green)}.data-stamp{font-size:12px;color:var(--muted);line-height:1.7}.metric-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px}.metric{display:flex;flex-direction:column;align-items:flex-start;text-align:left;gap:16px;padding:18px;border:1px solid var(--line);border-radius:10px;background:white;min-height:110px}.metric>text:first-child{font-size:13px;color:var(--muted)}.metric strong{font-size:28px;font-weight:650;line-height:1.2}.metric>text:last-child{display:none}.metric.selected{border-color:#86aa96;background:#edf3ee}.summary-line{font-size:12px;color:var(--muted);line-height:1.9}.summary-line b{font-size:19px;color:var(--ink);margin:0 9px}.next-attention{display:grid;grid-template-columns:1fr 1.2fr;gap:22px;align-items:stretch}.population{display:flex;gap:32px;margin:30px 0 24px}.population>view{display:flex;flex-direction:column;gap:8px}.population b{font-size:27px;line-height:1.2}.population text{font-size:12px;color:var(--muted)}.next-preview{display:grid;grid-template-columns:85px 1fr;gap:10px 14px;padding-top:20px;border-top:1px solid var(--line);font-size:14px}.next-preview>text{font-size:12px;color:var(--muted)}.next-preview>span{grid-column:2;font-size:12px;color:var(--muted)}.attention{color:var(--amber)!important}.alert-row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:16px 0;border-bottom:1px solid var(--line);font-size:14px}.alert-row:last-child{border:0}.alert-row b{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.alert-row button{flex:none}.more-alerts{padding-left:0;font-size:13px}.chart-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.account-status-panel>.input{max-width:430px;margin:20px 0}.account-head,.account-row{display:grid;grid-template-columns:1fr 1.1fr 1.3fr 1.5fr;gap:20px;align-items:start;font-size:13px}.account-head{font-size:12px;color:var(--muted);padding:14px 0;border-bottom:1px solid var(--line)}.account-row{padding:22px 0;border-bottom:1px solid var(--line)}.account-row>view{min-width:0;overflow-wrap:anywhere}.account-row:last-child{border:0}.account-row .link{padding:6px 0;font-size:12px}.run-detail{border-top:1px solid var(--line);padding:18px 0}.run-detail>summary{display:flex;align-items:center;gap:14px;min-height:44px;flex-wrap:wrap;font-size:14px;cursor:pointer}.run-detail>summary>text:last-child{font-size:12px;color:var(--muted)}.run-detail>p{margin:18px 0;font-size:14px}.phase-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}.phase-grid>view{background:var(--wash);padding:14px;border-radius:8px;font-size:13px}@media(max-width:1000px){.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.next-attention{grid-template-columns:1fr 1fr}.account-head{display:none}.account-row{grid-template-columns:1fr 1fr}.next-preview{grid-template-columns:1fr}.next-preview>span{grid-column:1}}@media(max-width:599px){.admin-dashboard{gap:20px}.dashboard-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));width:100%;gap:8px}.dashboard-filters .filter{min-width:0;padding:10px 8px;font-size:16px;width:100%}.dashboard-filters>.dashboard-date{grid-column:1/-1}.dashboard-filters>.button{grid-column:1/-1}.metric-grid{gap:8px}.metric{min-height:94px;padding:14px;gap:12px}.metric>text:first-child{font-size:12px}.metric strong{font-size:25px}.next-attention{grid-template-columns:1fr}.runtime-bar{gap:10px;flex-wrap:wrap}.population{gap:28px;margin:24px 0}.next-preview{grid-template-columns:75px 1fr}.next-preview>span{grid-column:2}.account-row>view:nth-child(3),.account-row>view:nth-child(4){grid-column:1/-1}.phase-grid{grid-template-columns:1fr}.chart-heading>text{font-size:11px}}
.stats-explanation summary::after{content:"⌄";margin-left:auto;font-size:15px}.stats-explanation[open]>summary::after{transform:rotate(180deg)}
</style>
