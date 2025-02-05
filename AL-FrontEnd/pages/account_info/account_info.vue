<template>
	<view class="content">
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
		<image src="/static/index/wave.png" class="wave" />
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


	// 表单数据
	const form = reactive({
		logonName: "",
		password: "",
		timeSlot: "",
		seatNumbers: ["", "", ""],
	});

	onMounted(() => {

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
			const storedReserved = uni.getStorageSync("isReserved"); // 从本地存储读取
			// 整合提交数据
			const requestData = {
				pid: form.logonName,
				logonName: form.logonName,
				password: form.password,
				timeSlot: form.timeSlot,
				seat_list: form.seatNumbers.filter((seat) => seat.trim() !== ""), // 过滤掉空的座位号
				is_reserved: storedReserved,
			};

			console.log("提交的数据", requestData); // 提交到后端
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
			console.error("提交失败:", error);
			uni.showToast({
				title: '提交失败',
				icon: 'none',
			});
		}

	};
</script>

<style scoped>
	.content {
		background-color: #E8F5E9;
		height: 100vh;
		margin: 0;
		display: flex;
		justify-content: center;
		align-items: center;
	}

	.form-container {
		margin-bottom: 60px;
		width: 70%;
		max-width: 400px;
		padding: 20px;
		background-color: #daebdb;
		border-radius: 15px;
		box-shadow: 2px 2px 2px 1px rgb(0 0 0 / 10%);
		/* border: 2px solid #8b9b88; */
	}

	.form-item {
		margin-bottom: 15px;
		display: flex;
		flex-direction: column;
	}

	label {
		font-size: 16px;
		margin-bottom: 5px;
		color: #535d52;
	}

	input {
		padding: 10px;
		/* border: 1px solid #8b9b88; */
		box-shadow: 2px 2px 2px 1px rgb(0 0 0 / 10%);
		border-radius: 5px;
		font-size: 14px;
		background-color: #C8E6C9;
		color: #535d52;
	}

	button {
		background-color: #aacead;
		box-shadow: 2px 2px 2px 1px rgb(0 0 0 / 10%);
		color: #535d52;
		border-radius: 5px;
		font-size: 16px;
	}

	.wave {
		width: 100%;
		height: 120px;
		position: fixed;
		bottom: 0;
		left: 0;
	}
</style>