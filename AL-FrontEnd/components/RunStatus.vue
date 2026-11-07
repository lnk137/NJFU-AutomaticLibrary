<template><text class="pill" :class="status==='success'?'green':status==='failed'?'red':''">{{labels[status]||status||'尚未执行'}}</text></template>
<script setup>
defineProps({status:String});
const labels={queued:'待执行',preparing:'准备中',submitting:'提交中',uncertain:'待确认',success:'已核实成功',failed:'失败',skipped:'已跳过'};
</script>
