export function schedulerNotice(status={}) {
  if(status.executor_state==='blocked') return {blocked:true,title:'自动预约待处理',detail:'请联系管理员检查后台。'};
  if(status.executor_state==='recovering') return {blocked:true,title:'后台正在恢复',detail:'已保存计划保留，恢复后自动继续。'};
  if(status.executor_state==='paused'&&status.allow_live===false) return {blocked:true,title:'管理员已暂停',detail:'已保存计划和已有预约保留。'};
  if(status.clock_ok===false||status.last_error) return {
    blocked:true,title:'自动预约已暂停',
    detail:status.last_error||'时钟校验未通过，请联系管理员处理。',
    hint:'暂停期间不会提交预约。已保存计划和已有预约保留。'
  };
  if(status.allow_live===false) return {blocked:false,title:'仅登录与查询',detail:'真实预约未开启。'};
  if(status.scheduler_running===false) return {blocked:true,title:'自动预约未运行',detail:'请联系管理员检查后台。'};
  if(status.execution_scope==='dual_node') {
    const state=status.dual_node_status?.state;
    const pending={waiting:'等待双机探测窗口',probing:'双机正在探测',eligible:'探测通过，等待选机',preparing:'双机正在准备',selected:'双机正在准备',authorized:'双机已授权，等待执行结果',idle:'暂无双机待执行任务'};
    if(pending[state]&&!status.scheduler_warning)return {blocked:false,title:pending[state],detail:status.execution_message||'计划已同步，执行资格将在放号前核验。'};
    if(state!=='ready'||status.ready_to_submit===false)return {blocked:true,title:'双机预约需检查',detail:status.scheduler_warning||status.execution_message||'双机执行资格尚未确认，请联系管理员检查。'};
    const detail=status.execution_message||'已取得当前双机执行资格，预约结果以学校回执为准。';
    return {blocked:false,title:'双机执行已就绪',detail:status.dual_node_status?.degraded?'当前仅一台执行节点可用。'+detail:detail};
  }
  if(status.scheduler_warning) return {
    blocked:false,title:'后台异常，正在自动恢复',detail:status.scheduler_warning,
    hint:'已保存计划保留；恢复后自动继续。部分账号异常不会暂停其他账号。'
  };
  if(status.allow_live===true&&status.scheduler_running===true&&status.clock_ok===true) return {
    blocked:false,title:'定时预约已开启',detail:'调度器运行中 · 时钟校验正常'
  };
  return {blocked:false,title:'正在连接本机后台…',detail:'正在核对预约执行状态。'};
}
