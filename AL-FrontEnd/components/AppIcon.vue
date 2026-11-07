<template><image class="app-icon" :src="source" mode="aspectFit" aria-hidden="true"/></template>
<script setup>
import {computed} from 'vue';
const props=defineProps({name:{type:String,default:'help-circle'}});
const safeName=computed(()=>/^[a-z-]+$/.test(props.name)?props.name:'help-circle');
const icons=import.meta.glob('../static/icons/*.svg',{eager:true,query:'?url',import:'default'});
const source=computed(()=>icons['../static/icons/'+safeName.value+'.svg']||icons['../static/icons/help-circle.svg']);
</script>
<style scoped>.app-icon{display:inline-block;width:21px;height:21px;flex:none;vertical-align:middle;opacity:.85;pointer-events:none}</style>
