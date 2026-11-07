<script>
import {useWorkspace} from '@/stores/workspace.js';
import {installButtonAccessibility} from '@/services/button_accessibility.mjs';
export default {onLaunch(){if(typeof document!=='undefined')installButtonAccessibility(document);try{if(typeof document!=='undefined')document.documentElement.dataset.motion=localStorage.getItem('njfu-reduced-motion')==='1'?'reduce':'';}catch{}uni.$on('njfu-auth-required',()=>{useWorkspace().clearSession();const pages=typeof getCurrentPages==='function'?getCurrentPages():[];if(pages[pages.length-1]?.route!=='pages/login/login')uni.reLaunch({url:'/pages/login/login'});});},onShow(){},onHide(){}};
</script>
<style>
@import "./styles/theme.css";
</style>
