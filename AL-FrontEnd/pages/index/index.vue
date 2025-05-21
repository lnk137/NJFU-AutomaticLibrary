<template>
	<view class="content">
		<!-- 毛玻璃效果区域 -->
		<view class="header">NJFU AutoLib</view>
		<!-- <view class="result-container">
			<p>{{ resultTime }}</p>
			<p>{{ resultMessage }}</p>
		</view> -->

		<view class="button-list">
			<view class="button" @click="goToPage('/pages/account_info/account_info')">
				<image src="/static/index/set.png" class="icon" />
				<view>配置</view>
			</view>
			<view class="button" @click="goToPage('/pages/announcement/announcement')">
				<image src="/static/index/ac.png" class="icon" style="scale: 1.1;" />
				<view>公告</view>
			</view>
			<view class="button" @click="goToPage('/pages/reservation_info/reservation_info')">
				<image src="/static/index/search.png" class="icon" />
				<view>查询</view>
			</view>
			<!-- 			<view v-if="isReserved" @click="switchIsReserved" class="button">
				<image src="/static/index/yes.png" class="icon" />
				<view>切换</view>
			</view>
			<view v-else @click="switchIsReserved" class="button">
				<image src="/static/index/err.png" class="icon" />
				<view>切换</view>
			</view> -->
		</view>
		<image src="/static/index/wave.png" class="wave" />
	</view>
</template>

<script setup>
	import {
		onMounted,
	} from "vue";
	import {
		onShow
	} from "@dcloudio/uni-app"; // 导入 onShow
	// config.js
	import {
		server_url
	} from "@/config/config.js"; // 根据实际路径调整


	const form = uni.getStorageSync("form");

	const goToPage = (url) => {
		uni.navigateTo({
			url,
		});
	};
</script>

<style scoped lang="less">
	.header {
		margin-top: 100px;
		font-size: 32px;
		font-weight: bold;
		color: #535d52
	}

	.content {
		/* 默认背景色，避免加载时空白 */
		background-color: #E8F5E9;
		/* 背景固定，滚动时不动 */
		height: 100vh;
		margin: 0;
		display: flex;

		align-items: center;
		flex-direction: column;
	}

	.result-container {
		margin-top: 60px;
		width: 72%;
		max-height: 300px;
		padding: 10px 20px;
		text-align: center;
		background: #C8E6C9;
		color: #535d52;
		font-size: 16px;
		font-weight: bold;
		border-radius: 20px;
		border: 2px solid #8b9b88;
	}


	.button-list {
		margin-top: 50px;
		display: flex;
		justify-content: center;
		align-items: center;
		flex-wrap: wrap;
	}

	.button {
		margin: 20px;
		background-color: #C8E6C9;
		color: #535d52;
		font-size: 16px;
		font-weight: bold;
		text-align: center;
		padding: 10px;
		border: 2px solid #707d6e;
		border-radius: 30px;
		display: flex;
		flex-direction: column;
		justify-content: center;
		align-items: center;
		border-radius: 50%;
		width: 60px;
		/* 设置宽度 */
		height: 60px;
		/* 设置高度 */
	}

	.icon {
		width: 35px;
		height: 35px;
	}

	.wave {
		width: 100%;
		height: 150px;
		position: fixed;
		bottom: 0;
		left: 0;
	}
</style>