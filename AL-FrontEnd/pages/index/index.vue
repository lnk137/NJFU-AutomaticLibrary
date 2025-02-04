<template>
	<view class="content">
		<!-- 毛玻璃效果区域 -->
		<view class="header">NJFU AutoLib</view>
		<view class="result-container">
			<p>{{ resultTime }}</p>
			<p>{{ resultMessage }}</p>
		</view>

		<view class="button-list">
			<!-- 			<view class="button" @click="goToPage('/pages/account_info/account_info')">配置账号信息</view>
			<view class="button" @click="goToPage('/pages/announcement/announcement')">查看公告</view>
			<view class="switch-container">
				<view class="button" v-if="isReserved" @click="switchIsReserved">已开启预约状态</view>
				<view class="button" v-else @click="switchIsReserved" style="color: #c22626;">已关闭预约状态</view>
			</view> -->
			<view class="button" @click="goToPage('/pages/account_info/account_info')">
				<image src="/static/index/set.png" class="icon" />
				<view>配置</view>
			</view>
			<view class="button" @click="goToPage('/pages/announcement/announcement')">
				<image src="/static/index/ac.png" class="icon" style="scale: 1.1;" />
				<view>公告</view>
			</view>
			<view v-if="isReserved" @click="switchIsReserved" class="button">
				<image src="/static/index/yes.png" class="icon" />
				<view>切换</view>
			</view>
			<view v-else @click="switchIsReserved" class="button">
				<image src="/static/index/err.png" class="icon" />
				<view>切换</view>
			</view>
		</view>
	</view>
</template>

<script setup>
	import {
		ref,
		onMounted,
	} from "vue";
	import {
		onShow
	} from "@dcloudio/uni-app"; // 导入 onShow
	// config.js
	import {
		server_url
	} from "@/config/config.js"; // 根据实际路径调整

	// 判断学号是否绑定
	const isStudentIdBound = ref(false);

	const goToPage = (url) => {
		uni.navigateTo({
			url,
		});
	};

	// 预约状态
	const isReserved = ref(true);

	// 切换预约状态并提交到后端
	const switchIsReserved = async () => {
		try {
			// 获取本地存储的 studentId
			const storedStudentId = uni.getStorageSync("studentId");
			if (!storedStudentId) {
				throw new Error("请先绑定学号");
			}

			// 准备切换的预约状态
			const newReservedState = !isReserved.value;

			// 整合提交数据
			const requestData = {
				pid: storedStudentId, // 从本地存储获取的学号
				is_reserved: newReservedState,
			};

			// 提交到后端
			const response = await uni.request({
				url: `${server_url}/db/update_reservation_status`, // 使用反引号拼接
				method: "POST",
				data: requestData, // 提交的 JSON 数据
				header: {
					"Content-Type": "application/json", // 设置请求头
				},
			});

			isReserved.value = newReservedState;
			uni.setStorageSync("isReserved", isReserved.value);

			// 显示提交成功提示
			uni.showToast({
				title: response.data.message,
				icon: "success",
				duration: 2000,
			});
		} catch (e) {
			// 错误处理
			uni.showToast({
				title: "提交失败，请检查网络",
				icon: "none",
				duration: 2000,
			});
		}
	};

	// 页面加载时读取预约状态
	onMounted(() => {
		const storedReserved = uni.getStorageSync("isReserved"); // 从本地存储读取
		if (storedReserved !== null && storedReserved !== undefined) {
			isReserved.value = storedReserved; // 更新状态
		}

		// 从后端获取结果字符串
		fetchResultMessage();
	});
	// 在页面显示时调用获取结果的函数
	onShow(() => {
		fetchResultMessage();
	});
	// 数据库返回的结果字符串
	const resultMessage = ref('');
	const resultTime = ref("");
	// 从后端获取结果字符串
	const fetchResultMessage = async () => {
		try {
			// 获取本地存储的 studentId
			const storedStudentId = uni.getStorageSync("studentId");
			if (!storedStudentId) {
				throw new Error("请先绑定学号");
			}

			const response = await uni.request({
				url: `${server_url}/db/get_reservations_by_pid`, // 替换为后端接口地址
				method: "POST",
				data: {
					'pid': storedStudentId
				}, // 提交的 JSON 数据
				header: {
					"Content-Type": "application/json", // 设置请求头
				},
			});
			console.log(response.data);
			const result = response.data.message;
			resultMessage.value = result.result_info;
			resultTime.value = result.created_at;

		} catch (e) {
			resultMessage.value = "尚未进行过预约";
		}
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
</style>