<template>
	<view class="content">
		<view style="margin-bottom:60px;" />
		<view class="item" v-for="acc in announcement" :key="acc.title">
			<!-- 标题 -->
			<view style="font-weight: bold; font-size: 20px; margin-bottom: 10px; color: #3c5873; user-select: text;">
				{{ acc.title }}
			</view>

			<!-- 内容 -->
			<view
				style="font-size: 15px; margin-bottom: 10px; color: #405f7c; user-select: text;white-space: pre-line;">
				{{ acc.content }}
			</view>

			<!-- 时间 -->
			<view style="font-weight: bold; font-size: 12px; color: #557fa4; user-select: text;">
				{{ acc.publish_time }}
			</view>
		</view>
	</view>
</template>

<script setup>
	import {
		ref
	} from "vue";
	import {
		onShow
	} from "@dcloudio/uni-app";
	import {
		server_url
	} from "@/config/config.js"; // 根据实际路径调整
	// 公告列表
	const announcement = ref([]);

	// 从本地存储加载公告数据
	const loadLocalAnnouncements = () => {
		try {
			const storedAnnouncements = uni.getStorageSync("announcements");
			if (storedAnnouncements) {
				announcement.value = JSON.parse(storedAnnouncements); // 加载本地数据
			}
		} catch (error) {
			console.error("加载本地公告失败:", error);
		}
	};

	// 从后端拉取公告数据
	const fetchAnnouncement = async () => {
		try {
			const response = await uni.request({
				url: `${server_url}/db/get_announcements`, // 替换为后端接口地址
				method: "GET",
			});

			if (response.statusCode === 200 && response.data.announcements) {
				// 替换换行符并更新公告列表
				const result = response.data.announcements.map(({
					content,
					...rest
				}) => ({
					...rest,
					content: content.replace(/\\n/g, "\n"), // 替换 \\n 为换行符
				}));

				announcement.value = result; // 更新公告数据
				uni.setStorageSync("announcements", JSON.stringify(result)); // 保存到本地存储

				console.log("公告数据已更新:", result);
			} else {
				console.error("公告请求失败:", response);
			}
		} catch (error) {
			console.error("公告获取失败:", error);
		}
	};


	// 页面显示时加载本地数据并更新数据
	onShow(() => {
		loadLocalAnnouncements(); // 首先加载本地数据
		fetchAnnouncement(); // 更新数据
	});
</script>


<style scoped>
	.content {
		background: #569EDE;
		margin: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		height: 100vh;
	}

	.item {
		width: 70%;
		height: auto;
		margin: 20px;
		padding: 20px;
		background: rgba(255, 255, 255, 0.2);
		backdrop-filter: blur(10px);
		-webkit-backdrop-filter: blur(10px);
		border-radius: 15px;
		box-shadow: 0 8px 20px rgba(0, 0, 0, 0.2);
		display: flex;
		flex-direction: column;
		justify-content: center;
		align-items: center;
	}
</style>