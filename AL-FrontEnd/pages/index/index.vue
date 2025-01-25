<template>
	<view class="content">
		<!-- 毛玻璃效果区域 -->
		<view class="result-container">
			<p>{{ resultTime }}</p>
			<p>{{ resultMessage }}</p>
		</view>

		<view class="button-list">
			<view class="button" @click="goToPage('/pages/account_info/account_info')">配置账号信息</view>
			<view class="button" @click="goToPage('/pages/announcement/announcement')">查看公告</view>
			<view class="switch-container">
				<view class="button" v-if="isReserved" @click="switchIsReserved">已开启预约状态</view>
				<view class="button" v-else @click="switchIsReserved" style="color: #c22626;">已关闭预约状态</view>
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
		const studentId = uni.getStorageSync("studentId");
		isStudentIdBound.value = !!studentId; // 如果学号存在，则为 true

		if (!isStudentIdBound.value) {
			uni.showToast({
				title: "请先绑定学号",
				icon: "none",
				duration: 2000,
			});
			return;
		}

		// 跳转到指定页面
		uni.navigateTo({
			url, // 跳转到指定页面的路径
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
	.content {
		background: #A2C8DA;
		/* 默认背景色，避免加载时空白 */
		background: url(@/static/b-i.png) no-repeat;
		background-size: cover;
		/* 图片覆盖填充 */
		background-position: center center;
		/* 居中对齐 */
		background-repeat: no-repeat;
		/* 防止图片重复 */
		background-attachment: fixed;
		/* 背景固定，滚动时不动 */
		height: 100vh;
		margin: 0;
		display: flex;

		align-items: center;
		flex-direction: column;
	}

	.result-container {
		margin-top: 150px;
		width: 72%;
		max-height: 300px;
		padding: 10px 20px;
		/* 增加水平内边距，让内容居中更好看 */
		text-align: center;
		background: rgba(255, 255, 255, 0.4);
		backdrop-filter: blur(10px);
		-webkit-backdrop-filter: blur(10px);
		color: #333;
		font-size: 16px;
		font-weight: bold;
		box-shadow: 0px 4px 10px rgba(0, 0, 0, 0.2);
		border-radius: 20px;
	}


	.button-list {
		display: flex;
		justify-content: center;
		align-items: center;
		flex-direction: column;
		height: 100%;
	}

	.button {
		background: rgba(255, 255, 255, 0.2);
		/* 半透明背景 */
		color: #265ac2;
		font-size: 16px;
		font-weight: bold;
		text-align: center;
		margin-top: 20px;
		padding: 12px 24px;
		border: 1px solid rgba(255, 255, 255, 0.3);
		/* 半透明边框 */
		border-radius: 30px;
		box-shadow: 0px 4px 6px rgba(0, 0, 0, 0.2);
		backdrop-filter: blur(10px);
		/* 毛玻璃模糊效果 */
		-webkit-backdrop-filter: blur(10px);
		/* Safari 兼容 */
		cursor: pointer;
		transition: all 0.3s ease;
	}
</style>