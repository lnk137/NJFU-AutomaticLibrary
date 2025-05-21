<template>
	<view class="content">
		<view class="form-container">
			<!-- 基本信息 -->
			<view class="form-section">
				<view class="section-title">基本信息</view>
				<view class="form-item">
					<label for="pid">学号：</label>
					<input id="pid" type="text" placeholder="请输入学号" v-model="form.pid" />
				</view>
				<view class="form-item">
					<label for="lib_password">图书馆密码：</label>
					<input id="lib_password" type="password" placeholder="请输入图书馆密码" v-model="form.lib_password" />
				</view>
				<view class="form-item">
					<label for="vpn_password">VPN密码：</label>
					<input id="vpn_password" type="password" placeholder="请输入VPN密码" v-model="form.vpn_password" />
				</view>
			</view>

			<!-- 座位信息 -->
			<view class="form-section">
				<view class="section-title">座位信息</view>
				<view class="form-item">
					<label>预约座位列表：</label>
					<view class="seat-list">
						<view v-for="(seat, index) in form.seat_list" :key="index" class="seat-item">
							<input type="text" :placeholder="'座位号 ' + (index + 1)" v-model="form.seat_list[index]" />
							<text class="remove-seat" @click="removeSeat(index)"
								v-if="form.seat_list.length > 1">×</text>
						</view>
						<button class="add-seat" @click="addSeat" v-if="form.seat_list.length < 5">添加座位</button>
					</view>
				</view>
			</view>

			<!-- 时间配置 -->
			<view class="form-section">
				<view class="section-title">时间配置</view>
				<view class="form-item">
					<label>预约模式：</label>
					<picker @change="modeChange" :value="modeIndex" :range="modeOptions">
						<view class="picker">{{ modeOptions[modeIndex] }}</view>
					</picker>
				</view>

				<!-- 每周固定时间 -->
				<view v-if="form.mode === 'week_time'" class="week-time">
					<view v-for="(time, day) in form.time.week_time" :key="day" class="week-day-item">
						<text>周{{ day === '7' ? '日' : ['一', '二', '三', '四', '五', '六'][parseInt(day)-1] }}：</text>
						<input type="text" v-model="form.time.week_time[day]" placeholder="如：10:00-12:00" />
					</view>
				</view>

				<!-- 明天预约 -->
				<view v-if="form.mode === 'tomorrow'" class="form-item">
					<label>明天时间段：</label>
					<input type="text" v-model="form.time.tomorrow" placeholder="如：14:48-18:00" />
				</view>

				<!-- 后天预约 -->
				<view v-if="form.mode === 'after_tomorrow'" class="form-item">
					<label>后天时间段：</label>
					<input type="text" v-model="form.time.after_tomorrow" placeholder="如：10:00-11:00" />
				</view>
			</view>

			<!-- 开关设置 -->
			<view class="form-section">
				<view class="section-title">功能开关</view>
				<view class="switch-item">
					<label>迟到保护：</label>
					<switch :checked="form.late_protection === 'True'"
						@change="(e) => switchChange(e, 'late_protection')" />
				</view>
				<view class="switch-item">
					<label>是否预约：</label>
					<switch :checked="form.is_reserved === 'True'" @change="(e) => switchChange(e, 'is_reserved')" />
				</view>
			</view>

			<view class="form-item">
				<button @click="submitForm" class="submit-btn">保存配置</button>
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

	const modeOptions = ['每周固定', '明天预约', '后天预约'];
	const modeValues = ['week_time', 'tomorrow', 'after_tomorrow'];
	const modeIndex = ref(0);

	// 表单数据
	const form = reactive({
		pid: "",
		lib_password: "",
		vpn_password: "",
		mode: "week_time",
		seat_list: [""],
		time: {
			week_time: {
				'1': '10:00-12:00',
				'2': '10:00-12:00',
				'3': '10:00-12:00',
				'4': '10:00-12:00',
				'5': '10:00-12:00',
				'6': '10:00-12:00',
				'7': '08:00-12:00'
			},
			tomorrow: "14:48-18:00",
			after_tomorrow: "10:00-11:00"
		},
		late_protection: "True",
		is_reserved: "True"
	});

	onMounted(() => {
		// 从本地存储加载表单数据
		const savedForm = uni.getStorageSync("libraryConfig");
		if (savedForm) {
			Object.assign(form, JSON.parse(savedForm));
			modeIndex.value = modeValues.indexOf(form.mode);
		}
	});

	// 监听表单数据变化，自动保存到本地
	watch(
		() => form,
		(newForm) => {
			uni.setStorageSync("libraryConfig", JSON.stringify(newForm)); // 保存到本地存储
		}, {
			deep: true
		}
	);

	const modeChange = (e) => {
		const index = e.detail.value;
		form.mode = modeValues[index];
		modeIndex.value = index;
	};

	const switchChange = (e, field) => {
		form[field] = e.detail.value ? "True" : "False";
	};

	const addSeat = () => {
		if (form.seat_list.length < 5) {
			form.seat_list.push("");
		}
	};

	const removeSeat = (index) => {
		form.seat_list.splice(index, 1);
	};

	const validateTimeFormat = (time) => {
		const timeRegex = /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/;
		return timeRegex.test(time);
	};

	const submitForm = async () => {
		console.log("form", form)
		// 基本验证
		if (!form.pid || !form.lib_password || !form.vpn_password) {
			uni.showToast({
				title: "请填写完整的基本信息",
				icon: "none"
			});
			return;
		}

		// 座位验证
		if (!form.seat_list.some(seat => seat.trim())) {
			uni.showToast({
				title: "请至少填写一个座位号",
				icon: "none"
			});
			return;
		}

		// 时间格式验证
		let timeValid = true;
		if (form.mode === "week_time") {
			Object.values(form.time.week_time).forEach(time => {
				if (!validateTimeFormat(time)) timeValid = false;
			});
		} else if (form.mode === "tomorrow") {
			timeValid = validateTimeFormat(form.time.tomorrow);
		} else {
			timeValid = validateTimeFormat(form.time.after_tomorrow);
		}

		if (!timeValid) {
			uni.showToast({
				title: "时间格式错误，请使用HH:MM-HH:MM格式",
				icon: "none"
			});
			return;
		}

		try {
			// 过滤掉空的座位号
			const cleanSeatList = form.seat_list.filter(seat => seat.trim() !== "");
			
			// 构建提交数据，确保字段名称与后端API一致
			const submitData = {
				pid: form.pid,
				lib_password: form.lib_password,
				vpn_password: form.vpn_password,
				seat_list: cleanSeatList,
				mode: form.mode,
				time: form.time,
				priority: 0, // 使用固定值，不再从表单获取
				is_reserved: form.is_reserved,
				late_protection: form.late_protection
			};

			// 提交到服务器
			const response = await uni.request({
				url: `${server_url}/db/reservation/all`,
				method: "POST",
				data: submitData,
				header: {
					"Content-Type": "application/json"
				}
			});

			if (response.data && response.statusCode === 200) {
				uni.showToast({
					title: "配置保存成功",
					icon: "success"
				});
			} else {
				throw new Error(response.data?.error || "保存失败");
			}
		} catch (error) {
			console.error("保存失败:", error);
			uni.showToast({
				title: error.message || "保存失败，请重试",
				icon: "none"
			});
		}
	};
</script>

<style scoped>
	.content {
		background-color: #E8F5E9;
		min-height: 100vh;
		padding: 15px;
		box-sizing: border-box;
	}

	.form-container {
		max-width: 100%;
		margin: 0 auto;
		padding: 10px;
	}

	.form-section {
		background-color: #ffffff;
		border-radius: 12px;
		padding: 15px;
		margin-bottom: 15px;
		box-shadow: 0 2px 6px rgba(0, 0, 0, 0.1);
	}

	.section-title {
		font-size: 16px;
		font-weight: bold;
		color: #2c3e50;
		margin-bottom: 15px;
		padding-bottom: 8px;
		border-bottom: 1px solid #eee;
	}

	.form-item {
		margin-bottom: 15px;
	}

	.form-item label {
		display: block;
		margin-bottom: 8px;
		color: #34495e;
		font-size: 14px;
	}

	input {
		width: 100%;
		padding: 10px 12px;
		border: 1px solid #dcdfe6;
		border-radius: 8px;
		font-size: 15px;
		height: 40px;
		background-color: #f9f9f9;
		box-sizing: border-box;
	}

	.picker {
		padding: 10px 12px;
		border: 1px solid #dcdfe6;
		border-radius: 8px;
		background-color: #f9f9f9;
		height: 40px;
		line-height: 20px;
		display: flex;
		align-items: center;
		position: relative;
	}

	.picker::after {
		content: '';
		width: 0;
		height: 0;
		border-left: 5px solid transparent;
		border-right: 5px solid transparent;
		border-top: 5px solid #666;
		position: absolute;
		right: 12px;
		top: 50%;
		transform: translateY(-50%);
	}

	.week-time {
		margin-top: 10px;
	}

	.week-day-item {
		display: flex;
		align-items: center;
		margin-bottom: 12px;
	}

	.week-day-item text {
		width: 50px;
		font-size: 14px;
	}

	.switch-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 15px;
		height: 44px;
		padding: 0 5px;
	}

	.seat-list {
		margin-top: 10px;
	}

	.seat-item {
		display: flex;
		align-items: center;
		margin-bottom: 12px;
	}

	.remove-seat {
		margin-left: 10px;
		color: #ff4949;
		font-size: 24px;
		cursor: pointer;
		width: 28px;
		height: 28px;
		line-height: 28px;
		text-align: center;
	}

	.add-seat {
		margin-top: 10px;
		background-color: #67c23a;
		color: white;
		border: none;
		padding: 8px 15px;
		border-radius: 8px;
		font-size: 14px;
		height: 38px;
		box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
	}

	.submit-btn {
		width: 100%;
		background-color: #409eff;
		color: white;
		border: none;
		padding: 12px;
		border-radius: 8px;
		font-size: 16px;
		margin-top: 20px;
		height: 48px;
		box-shadow: 0 2px 6px rgba(64, 158, 255, 0.3);
	}
</style>