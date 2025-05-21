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
						<view class="time-picker-group">
							<view class="time-picker">
								<picker mode="multiSelector" 
									:value="[hourOptions.indexOf(getTimeComponents(time).hour), minuteOptions.indexOf(getTimeComponents(time).minute)]"
									:range="[hourOptions, minuteOptions]"
									@change="(e) => handleTimeChange(day, 'start', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
									<view class="picker-text">{{ getTimeComponents(time).hour }}:{{ getTimeComponents(time).minute }}</view>
								</picker>
							</view>
							<text class="time-separator">至</text>
							<view class="time-picker">
								<picker mode="multiSelector"
									:value="[hourOptions.indexOf(getTimeComponents(time.split('-')[1]).hour), minuteOptions.indexOf(getTimeComponents(time.split('-')[1]).minute)]"
									:range="[hourOptions, minuteOptions]"
									@change="(e) => handleTimeChange(day, 'end', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
									<view class="picker-text">{{ getTimeComponents(time.split('-')[1]).hour }}:{{ getTimeComponents(time.split('-')[1]).minute }}</view>
								</picker>
							</view>
						</view>
					</view>
				</view>

				<!-- 明天预约 -->
				<view v-if="form.mode === 'tomorrow'" class="form-item">
					<label>明天时间段：</label>
					<view class="time-picker-group">
						<view class="time-picker">
							<picker mode="multiSelector"
								:value="[hourOptions.indexOf(getTimeComponents(form.time.tomorrow).hour), minuteOptions.indexOf(getTimeComponents(form.time.tomorrow).minute)]"
								:range="[hourOptions, minuteOptions]"
								@change="(e) => handleTimeChange(null, 'start', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
								<view class="picker-text">{{ getTimeComponents(form.time.tomorrow).hour }}:{{ getTimeComponents(form.time.tomorrow).minute }}</view>
							</picker>
						</view>
						<text class="time-separator">至</text>
						<view class="time-picker">
							<picker mode="multiSelector"
								:value="[hourOptions.indexOf(getTimeComponents(form.time.tomorrow.split('-')[1]).hour), minuteOptions.indexOf(getTimeComponents(form.time.tomorrow.split('-')[1]).minute)]"
								:range="[hourOptions, minuteOptions]"
								@change="(e) => handleTimeChange(null, 'end', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
								<view class="picker-text">{{ getTimeComponents(form.time.tomorrow.split('-')[1]).hour }}:{{ getTimeComponents(form.time.tomorrow.split('-')[1]).minute }}</view>
							</picker>
						</view>
					</view>
				</view>

				<!-- 后天预约 -->
				<view v-if="form.mode === 'after_tomorrow'" class="form-item">
					<label>后天时间段：</label>
					<view class="time-picker-group">
						<view class="time-picker">
							<picker mode="multiSelector"
								:value="[hourOptions.indexOf(getTimeComponents(form.time.after_tomorrow).hour), minuteOptions.indexOf(getTimeComponents(form.time.after_tomorrow).minute)]"
								:range="[hourOptions, minuteOptions]"
								@change="(e) => handleTimeChange(null, 'start', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
								<view class="picker-text">{{ getTimeComponents(form.time.after_tomorrow).hour }}:{{ getTimeComponents(form.time.after_tomorrow).minute }}</view>
							</picker>
						</view>
						<text class="time-separator">至</text>
						<view class="time-picker">
							<picker mode="multiSelector"
								:value="[hourOptions.indexOf(getTimeComponents(form.time.after_tomorrow.split('-')[1]).hour), minuteOptions.indexOf(getTimeComponents(form.time.after_tomorrow.split('-')[1]).minute)]"
								:range="[hourOptions, minuteOptions]"
								@change="(e) => handleTimeChange(null, 'end', hourOptions[e.detail.value[0]], minuteOptions[e.detail.value[1]])">
								<view class="picker-text">{{ getTimeComponents(form.time.after_tomorrow.split('-')[1]).hour }}:{{ getTimeComponents(form.time.after_tomorrow.split('-')[1]).minute }}</view>
							</picker>
						</view>
					</view>
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

	// 时间选择相关
	const timeRange = ref(['07:30', '22:00']); // 修改时间范围
	const timeStep = 1; // 修改为1分钟间隔
	
	// 生成时间选项
	const generateTimeOptions = () => {
		const options = [];
		const [start, end] = timeRange.value;
		const [startHour, startMin] = start.split(':').map(Number);
		const [endHour, endMin] = end.split(':').map(Number);
		
		let currentTime = new Date();
		currentTime.setHours(startHour, startMin, 0);
		const endTime = new Date();
		endTime.setHours(endHour, endMin, 0);
		
		while (currentTime <= endTime) {
			const hours = currentTime.getHours().toString().padStart(2, '0');
			const minutes = currentTime.getMinutes().toString().padStart(2, '0');
			options.push(`${hours}:${minutes}`);
			currentTime.setMinutes(currentTime.getMinutes() + timeStep);
		}
		return options;
	};

	// 生成小时和分钟选项
	const generateHourOptions = () => {
		// 只生成7-22的小时选项
		return Array.from({length: 16}, (_, i) => (i + 7).toString().padStart(2, '0'));
	};

	const generateMinuteOptions = () => {
		return Array.from({length: 60}, (_, i) => i.toString().padStart(2, '0'));
	};

	const hourOptions = generateHourOptions();
	const minuteOptions = generateMinuteOptions();

	// 处理时间选择
	const handleTimeChange = (day, type, hourValue, minuteValue) => {
		const timeValue = `${hourValue}:${minuteValue}`;
		
		// 验证时间是否在允许范围内
		const [startHour, startMin] = timeRange.value[0].split(':').map(Number);
		const [endHour, endMin] = timeRange.value[1].split(':').map(Number);
		const [selectedHour, selectedMin] = timeValue.split(':').map(Number);
		
		// 特殊处理7:30的情况
		if (selectedHour === 7 && selectedMin < 30) {
			uni.showToast({
				title: '7点只能选择30分及以后',
				icon: 'none'
			});
			return;
		}
		
		const selectedTime = selectedHour * 60 + selectedMin;
		const startTime = startHour * 60 + startMin;
		const endTime = endHour * 60 + endMin;
		
		if (selectedTime < startTime || selectedTime > endTime) {
			uni.showToast({
				title: `时间必须在${timeRange.value[0]}-${timeRange.value[1]}之间`,
				icon: 'none'
			});
			return;
		}

		if (form.mode === 'week_time') {
			if (!form.time.week_time) form.time.week_time = {};
			if (!form.time.week_time[day]) form.time.week_time[day] = '00:00-00:00';
			
			const [start, end] = form.time.week_time[day].split('-');
			form.time.week_time[day] = type === 'start' ? 
				`${timeValue}-${end}` : 
				`${start}-${timeValue}`;
		} else if (form.mode === 'tomorrow') {
			if (!form.time.tomorrow) form.time.tomorrow = '00:00-00:00';
			const [start, end] = form.time.tomorrow.split('-');
			form.time.tomorrow = type === 'start' ? 
				`${timeValue}-${end}` : 
				`${start}-${timeValue}`;
		} else if (form.mode === 'after_tomorrow') {
			if (!form.time.after_tomorrow) form.time.after_tomorrow = '00:00-00:00';
			const [start, end] = form.time.after_tomorrow.split('-');
			form.time.after_tomorrow = type === 'start' ? 
				`${timeValue}-${end}` : 
				`${start}-${timeValue}`;
		}
	};

	// 获取当前时间的小时和分钟
	const getTimeComponents = (timeStr) => {
		if (!timeStr) return { hour: '00', minute: '00' };
		const [hour, minute] = timeStr.split('-')[0].split(':');
		return { hour, minute };
	};

	// 表单数据
	const form = reactive({
		pid: "",
		lib_password: "",
		vpn_password: "",
		mode: "", // 不设默认模式
		seat_list: [], // 不设默认座位列表
		time: {}, // 不设默认时间配置
		late_protection: "", // 不设默认迟到保护
		is_reserved: "" // 不设默认是否预约
	});

	onMounted(() => {
		// 从本地存储加载所有表单数据
		const savedUserInfo = uni.getStorageSync("userInfo");
		if (savedUserInfo) {
			const userInfo = JSON.parse(savedUserInfo);
			// 使用 Object.assign 确保加载的数据覆盖默认空值
			Object.assign(form, userInfo);

			// 确保 modeIndex 与加载的数据同步
			const loadedModeIndex = modeValues.indexOf(form.mode);
			if (loadedModeIndex !== -1) {
				modeIndex.value = loadedModeIndex;
			} else {
				// 如果加载的 mode 不在 modeValues 中，重置为默认第一个模式并更新 index
				form.mode = modeValues[0];
				modeIndex.value = 0;
			}
			// 确保 seat_list 至少有一个空字符串用于输入，如果加载的数据为空或不是数组
			if (!Array.isArray(form.seat_list) || form.seat_list.length === 0) {
				form.seat_list = [""];
			} else {
				// 如果 seat_list 存在且是数组，确保最后一个是空字符串以便添加新座位
				if (form.seat_list[form.seat_list.length - 1] !== "") {
					// form.seat_list.push(""); // 考虑是否需要在加载时自动添加一个空输入框
					// 或者在 addSeat 函数中处理当列表为空时的逻辑
				}
			}
		} else {
			// 如果本地存储没有数据，初始化 seat_list 为包含一个空字符串的数组
			form.seat_list = [""];
			// 初始化 mode 为第一个选项
			form.mode = modeValues[0];
			modeIndex.value = 0;
			// 其他字段保持 reactive 定义时的空值
		}
	});

	// 监听表单数据变化，自动保存到本地
	watch(
		() => form,
		(newForm) => {
			// 保存所有数据到 userInfo
			uni.setStorageSync("userInfo", JSON.stringify(newForm));
		},
		{ deep: true }
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

		// 在发送请求前，显式保存到本地存储
		uni.setStorageSync("userInfo", JSON.stringify(form));

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
		height: 100%;
		background-attachment: fixed;
		background-size: cover;
		padding: 15px;
		box-sizing: border-box;
		margin-top: 30px;
		position: relative;
		z-index: 1;
	}

	/* 添加一个伪元素来确保背景色完全覆盖 */
	.content::before {
		content: '';
		position: fixed;
		top: 0;
		left: 0;
		width: 100%;
		height: 100%;
		background-color: #E8F5E9;
		z-index: -1;
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
		gap: 10px;
	}

	.week-day-item text:first-child {
		width: 50px;
		font-size: 14px;
		flex-shrink: 0;
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
		display: flex;
		justify-content: center;
		align-items: center;
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
		display: flex;
		justify-content: center;
		align-items: center;
		padding: 0;
	}

	.time-picker-group {
		display: flex;
		align-items: center;
		gap: 10px;
		flex: 1;
	}

	.time-picker {
		flex: 1;
		background-color: #f9f9f9;
		border: 1px solid #dcdfe6;
		border-radius: 8px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 100px; /* 确保选择器有足够的宽度 */
	}

	.picker-text {
		font-size: 15px;
		color: #333;
		text-align: center;
		width: 100%;
	}

	.time-separator {
		color: #666;
		font-size: 14px;
	}
</style>