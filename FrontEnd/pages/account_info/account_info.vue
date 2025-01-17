<template>
	<view class="content" :style="{ backgroundImage: backgroundLoaded ? `url(${backgroundImage})` : 'none' }">
		<view class="form-container">
			<view class="form-item">
				<label for="logonName">图书馆账号：</label>
				<input id="logonName" type="text" placeholder="图书馆账号" v-model="form.logonName" />
			</view>
			<view class="form-item">
				<label for="password">图书馆密码：</label>
				<input id="password" type="text" placeholder="请输入图书馆密码" v-model="form.password" />
			</view>
			<view class="form-item">
				<label for="timeSlot">预约时间段：</label>
				<input id="timeSlot" type="text" placeholder="如：08:00-10:00" v-model="form.timeSlot" />
			</view>
			<view class="form-item">
				<label>预约座位号(如:2F-B067,4F-A398)：</label>
				<view>
					<input type="text" placeholder="座位号1" v-model="form.seatNumbers[0]" style="margin-bottom: 10px;" />
					<input type="text" placeholder="座位号2" v-model="form.seatNumbers[1]" style="margin-bottom: 10px;" />
					<input type="text" placeholder="座位号3" v-model="form.seatNumbers[2]" />
				</view>
			</view>
			<view class="form-item">
				<button @click="submitForm">提交信息</button>
			</view>
		</view>
	</view>
</template>

<script setup>
	import {
		reactive,
		onMounted,
		ref,
		watch
	} from "vue";
	import {
		server_url
	} from "@/config/config.js";

	const backgroundLoaded = ref(false);
	const backgroundImage = "/static/b-i.png"; // 背景图片路径

	// 表单数据
	const form = reactive({
		logonName: "",
		password: "",
		timeSlot: "",
		seatNumbers: ["", "", ""],
	});

	// 在组件挂载后加载背景图片和表单信息
	onMounted(() => {
		// 加载背景图片
		uni.getImageInfo({
			src: backgroundImage,
			success: () => {
				backgroundLoaded.value = true; // 图片加载完成后设置为已加载
			},
			fail: (err) => {
				console.error("背景图片加载失败", err);
			},
		});

		// 从本地存储加载表单数据
		const savedForm = uni.getStorageSync("form");
		if (savedForm) {
			Object.assign(form, savedForm); // 将本地存储的数据填充到表单中
		}
	});

	// 监听表单数据变化，自动保存到本地
	watch(
		() => form,
		(newForm) => {
			uni.setStorageSync("form", newForm); // 保存到本地存储
		}, {
			deep: true
		}
	);

	const submitForm = () => {
		// 验证表单数据
		if (!form.logonName || !form.password || !form.timeSlot) {
			uni.showToast({
				title: "请填写完整信息",
				icon: "none",
			});
			return;
		}

		// 验证预约时间段格式
		const timeSlotRegex = /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/;
		if (!timeSlotRegex.test(form.timeSlot)) {
			uni.showToast({
				title: "时间段格式错误，格式如 08:00-10:00，使用英文符号",
				icon: "none",
			});
			return;
		}

		// 验证座位号格式
		const seatNumberRegex = /^[1-9][0-9]*F-[A-Z]\d{3}$/; // 示例正则：匹配类似 "2F-A012"
		const invalidSeats = form.seatNumbers.filter(
			(seat) => seat.trim() !== "" && !seatNumberRegex.test(seat.trim())
		);

		if (invalidSeats.length > 0) {
			uni.showToast({
				title: `无效座位号: ${invalidSeats.join(", ")}`,
				icon: "none",
			});
			return;
		}

		// 确保至少填写一个座位号
		if (!form.seatNumbers.some((seat) => seat.trim() !== "")) {
			uni.showToast({
				title: "请至少填写一个座位号",
				icon: "none",
			});
			return;
		}

		// 表单数据提交逻辑
		uni.showModal({
			title: "确认预约",
			content: `账号：${form.logonName}\n预约时间段：${form.timeSlot}\n座位号：${form.seatNumbers
	            .filter((seat) => seat.trim() !== "")
	            .join(", ")}`,
			success: (res) => {
				if (res.confirm) {
					// 如果用户点击确认，则调用发送数据的函数
					sendReservationData();
				}
			},
		});
	};


	const sendReservationData = async () => {
		try {
			// 获取本地存储的 studentId
			const storedStudentId = uni.getStorageSync("studentId");
			if (!storedStudentId) {
				throw new Error("请先绑定学号");
			}
			// 判断预约状态
			const storedReserved = uni.getStorageSync("isReserved"); // 从本地存储读取

			// 整合提交数据
			const requestData = {
				pid: storedStudentId, // 从本地存储获取的学号
				logonName: form.logonName,
				password: form.password,
				timeSlot: form.timeSlot,
				seat_list: form.seatNumbers.filter((seat) => seat.trim() !== ""), // 过滤掉空的座位号
				is_reserved: storedReserved,
			};

			// 提交到后端
			const response = await uni.request({
				url: `${server_url}/db/insert_reservation`, // 后端接口地址
				method: "POST",
				data: requestData, // 提交的 JSON 数据
				header: {
					"Content-Type": "application/json", // 设置请求头
				},
			});

			// 提示用户提交结果
			uni.showToast({
				title: response.data.message,
				icon: "none",
				duration: 2000,
			});

		} catch (error) {
			uni.showToast({
				title: '提交失败',
				icon: 'none',
			});
		}

	};
</script>

<style scoped>
	.content {
		background: #A2C8DA;
		background-size: cover;
		background-position: center center;
		height: 100vh;
		margin: 0;
		display: flex;
		justify-content: center;
		align-items: center;
	}

	.form-container {
		width: 70%;
		max-width: 400px;
		padding: 20px;
		background: rgba(255, 255, 255, 0.2);
		backdrop-filter: blur(10px);
		-webkit-backdrop-filter: blur(10px);
		border-radius: 15px;
		box-shadow: 0 8px 20px rgba(0, 0, 0, 0.2);
	}

	.form-item {
		margin-bottom: 15px;
		display: flex;
		flex-direction: column;
	}

	label {
		font-size: 16px;
		margin-bottom: 5px;
		color: #265ac2;
	}

	input {
		padding: 10px;
		border: 1px solid rgba(255, 255, 255, 0.3);
		border-radius: 5px;
		font-size: 14px;
		background: rgba(255, 255, 255, 0.2);
		color: #0e2249;
		backdrop-filter: blur(5px);
		-webkit-backdrop-filter: blur(5px);
	}

	input::placeholder {
		color: rgba(255, 255, 255, 0.6);
	}

	button {
		background: rgba(255, 255, 255, 0.2);
		color: #fff;
		border: 1px solid rgba(255, 255, 255, 0.3);
		border-radius: 5px;
		font-size: 16px;
		cursor: pointer;
		backdrop-filter: blur(5px);
		-webkit-backdrop-filter: blur(5px);
		transition: all 0.3s ease;
	}

	button:hover {
		background: rgba(255, 255, 255, 0.4);
	}

	button:active {
		background: rgba(255, 255, 255, 0.3);
	}
</style>