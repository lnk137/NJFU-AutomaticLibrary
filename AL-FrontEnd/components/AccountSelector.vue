<template>
  <view class="panel account-panel">
    <view class="section-head"><view><text class="eyebrow">ACCOUNTS</text><h2>{{canManage?'账号列表':'我的账号'}} <text class="muted small">{{accounts.length}}</text></h2></view><button v-if="canManage" class="button small-button" @click="$emit('select','')">＋ 新增</button></view>
    <view v-if="!accounts.length" class="empty compact">添加学号和学校登录密码，开始管理预约。</view>
    <button v-for="a in accounts" :key="a.id" class="account-item" :class="{selected:active===a.id}" @click="$emit('select',a.id)">
      <view class="avatar">{{(a.alias||a.student_id).slice(0,1)}}</view>
      <view class="account-copy"><text class="account-name">{{a.alias}}</text><text class="muted small">{{a.student_id}}</text></view>
      <text class="pill" :class="a.plan?.enabled?'green':''">{{a.plan?.enabled?'计划开启':'未开启'}}</text>
    </button>
    <view class="sidebar-note">{{canManage?'账号与计划分别保存。切换账号不会影响其他人的设置。':'你的设置与记录仅本人和管理员可见。'}}</view>
  </view>
</template>
<script setup>defineProps({accounts:Array,active:String,canManage:Boolean});defineEmits(['select']);</script>
