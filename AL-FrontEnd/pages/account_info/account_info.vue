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
					<label for="vpn_password">网上办事大厅密码(用于连接VPN)：</label>
					<input id="vpn_password" type="password" placeholder="请输入网上办事大厅密码" v-model="form.vpn_password" />
				</view>
			</view>

			<!-- 座位信息 -->
			<view class="form-section">
				<view class="section-title">座位信息</view>
				<view class="form-item">
					<label>预约座位列表(填写完整座位号)：</label>
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
						<text class="week-day-label">周{{ day === '7' ? '日' : ['一', '二', '三', '四', '五', '六'][parseInt(day)-1] }}</text>
						<view class="time-picker-group">
							<picker mode="time" :value="getStartTime(time)" @change="(e) => handleTimeChange(day, 'start', e.detail.value)" 
								:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
								<view class="picker-text">{{ getStartTime(time) || '开始时间' }}</view>
							</picker>
							<text class="time-separator">至</text>
							<picker mode="time" :value="getEndTime(time)" @change="(e) => handleTimeChange(day, 'end', e.detail.value)"
								:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
								<view class="picker-text">{{ getEndTime(time) || '结束时间' }}</view>
							</picker>
						</view>
					</view>
				</view>

				<!-- 明天预约 -->
				<view v-if="form.mode === 'tomorrow'" class="form-item">
					<label>明天时间段：</label>
					<view class="time-picker-group">
						<picker mode="time" :value="getStartTime(form.time.tomorrow)" @change="(e) => handleTomorrowTimeChange('start', e.detail.value)"
							:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
							<view class="picker-text">{{ getStartTime(form.time.tomorrow) || '开始时间' }}</view>
						</picker>
						<text class="time-separator">至</text>
						<picker mode="time" :value="getEndTime(form.time.tomorrow)" @change="(e) => handleTomorrowTimeChange('end', e.detail.value)"
							:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
							<view class="picker-text">{{ getEndTime(form.time.tomorrow) || '结束时间' }}</view>
						</picker>
					</view>
				</view>

				<!-- 后天预约 -->
				<view v-if="form.mode === 'after_tomorrow'" class="form-item">
					<label>后天时间段：</label>
					<view class="time-picker-group">
						<picker mode="time" :value="getStartTime(form.time.after_tomorrow)" @change="(e) => handleAfterTomorrowTimeChange('start', e.detail.value)"
							:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
							<view class="picker-text">{{ getStartTime(form.time.after_tomorrow) || '开始时间' }}</view>
						</picker>
						<text class="time-separator">至</text>
						<picker mode="time" :value="getEndTime(form.time.after_tomorrow)" @change="(e) => handleAfterTomorrowTimeChange('end', e.detail.value)"
							:start="TIME_RANGE.start" :end="TIME_RANGE.end" class="time-picker">
							<view class="picker-text">{{ getEndTime(form.time.after_tomorrow) || '结束时间' }}</view>
						</picker>
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
	const modeIndex = ref(1);

	// 添加时间范围常量
	const TIME_RANGE = {
		start: '07:30',
		end: '22:00'
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
				// 如果加载的 mode 不在 modeValues 中，重置为默认"明天预约"模式
				form.mode = modeValues[1];  // 使用 index 1 对应"明天预约"
				modeIndex.value = 1;
			}
			// 确保 seat_list 至少有一个空字符串用于输入，如果加载的数据为空或不是数组
			if (!Array.isArray(form.seat_list) || form.seat_list.length === 0) {
				form.seat_list = [""];
			}
		} else {
			// 如果本地存储没有数据，初始化所有默认值
			form.seat_list = [""];
			form.mode = modeValues[1];  // 默认使用"明天预约"
			modeIndex.value = 1;
			form.late_protection = "False";
			form.is_reserved = "False";
		}
		
		// 无论是否加载本地数据，都确保 time 对象及其属性被初始化
		if (!form.time) form.time = {};
		if (!form.time.tomorrow) form.time.tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`; // 使用默认时间范围初始化
		if (!form.time.after_tomorrow) form.time.after_tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`; // 使用默认时间范围初始化

		// 确保 week_time 在 time 对象中存在并初始化所有周的默认时间
		if (!form.time.week_time) {
			form.time.week_time = {};
		}
		// 遍历所有周，如果时间不存在或格式不正确，则初始化为默认时间
		for (let i = 1; i <= 7; i++) {
			const dayStr = i.toString();
			if (!form.time.week_time[dayStr] || !validateTimeFormat(form.time.week_time[dayStr])) {
				 form.time.week_time[dayStr] = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
			}
		}
	});

	// 监听表单数据变化，自动保存到本地
	watch(
		() => form,
		(newForm) => {
			// 保存所有数据到 userInfo
			uni.setStorageSync("userInfo", JSON.stringify(newForm));
		}, {
			deep: true
		}
	);

	const modeChange = (e) => {
		const index = e.detail.value;
		form.mode = modeValues[index];
		modeIndex.value = index;

		// 切换模式时，如果对应的时间段没有值，初始化为默认时间范围
		if (form.mode === 'tomorrow' && (!form.time.tomorrow || !validateTimeFormat(form.time.tomorrow))) {
			form.time.tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
		} else if (form.mode === 'after_tomorrow' && (!form.time.after_tomorrow || !validateTimeFormat(form.time.after_tomorrow))) {
			form.time.after_tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
		} else if (form.mode === 'week_time') {
			// 在 onMounted 中已经确保了 week_time 的初始化
			// 遍历所有周，如果时间不存在或格式不正确，则初始化为默认时间
			for (let i = 1; i <= 7; i++) {
				const dayStr = i.toString();
				if (!form.time.week_time[dayStr] || !validateTimeFormat(form.time.week_time[dayStr])) {
					 form.time.week_time[dayStr] = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				}
			}
		}
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
		// 这个函数现在主要用于最终提交前的兜底验证
		const timeRegex = /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/;
		if (!timeRegex.test(time)) return false;

		// 添加时间范围的验证
		const [start, end] = time.split('-');
		if (!validateTimeRange(start) || !validateTimeRange(end)) return false;

		// 添加开始时间早于结束时间的验证
		const startTime = new Date(`2000/01/01 ${start}`);
		const endTime = new Date(`2000/01/01 ${end}`);
		if (startTime >= endTime) return false;

		return true;
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
			// 遍历所有周，进行最终验证
			for (let i = 1; i <= 7; i++) {
				const dayStr = i.toString();
				// 如果当前模式是 week_time，才进行验证
				if (form.mode === 'week_time' && (!form.time.week_time[dayStr] || !validateTimeFormat(form.time.week_time[dayStr]))) {
					timeValid = false;
					break; // 发现一个无效时间就停止检查
				}
			}
		} else if (form.mode === "tomorrow") {
			// 如果当前模式是 tomorrow，才进行验证
			if (form.mode === 'tomorrow' && (!form.time.tomorrow || !validateTimeFormat(form.time.tomorrow))) {
				timeValid = false;
			}
		} else if (form.mode === "after_tomorrow") {
			// 如果当前模式是 after_tomorrow，才进行验证
			if (form.mode === 'after_tomorrow' && (!form.time.after_tomorrow || !validateTimeFormat(form.time.after_tomorrow))) {
				timeValid = false;
			}
		}

		if (!timeValid) {
			uni.showToast({
				title: "时间设置无效，请检查格式和范围 (HH:MM-HH:MM, 07:00-22:00)，并确保结束时间晚于开始时间",
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

	const getStartTime = (timeStr) => {
		if (!timeStr || !timeStr.includes('-')) return '';
		return timeStr.split('-')[0];
	};

	const getEndTime = (timeStr) => {
		if (!timeStr || !timeStr.includes('-')) return '';
		return timeStr.split('-')[1];
	};

	// 验证时间是否在允许范围内
	const validateTimeRange = (time) => {
		if (!time) return false;
		
		const [hours, minutes] = time.split(':').map(Number);
		const timeInMinutes = hours * 60 + minutes;
		
		const [startHours, startMinutes] = TIME_RANGE.start.split(':').map(Number);
		const [endHours, endMinutes] = TIME_RANGE.end.split(':').map(Number);
		
		const startTimeInMinutes = startHours * 60 + startMinutes;
		const endTimeInMinutes = endHours * 60 + endMinutes;
		
		return timeInMinutes >= startTimeInMinutes && timeInMinutes <= endTimeInMinutes;
	};

	// 修改时间选择处理函数
	const handleTimeChange = (day, type, timeValue) => {
		// 验证时间是否在允许范围内
		if (!validateTimeRange(timeValue)) {
			uni.showToast({
				title: `时间必须在${TIME_RANGE.start}-${TIME_RANGE.end}之间`,
				icon: 'none'
			});
			return;
		}

		if (!form.time.week_time[day]) {
			form.time.week_time[day] = '00:00-00:00';
		}
		
		const [start, end] = form.time.week_time[day].split('-');
		form.time.week_time[day] = type === 'start' ? 
			`${timeValue}-${end}` : 
			`${start}-${timeValue}`;
		
		// 验证时间
		validateAndUpdateTime(day);
	};

	// 修改时间验证函数
	const validateAndUpdateTime = (day) => {
		const time = form.time.week_time[day];
		// 仅当当前模式是 week_time 时才进行此验证，并只重置当前天的值
		if (form.mode === 'week_time' && time && time.includes('-')) {
			const [start, end] = time.split('-');
			
			// 验证开始时间
			if (!validateTimeRange(start)) {
				uni.showToast({
					title: `周${day === '7' ? '日' : ['一', '二', '三', '四', '五', '六'][parseInt(day)-1]}开始时间必须在${TIME_RANGE.start}-${TIME_RANGE.end}之间`,
					icon: 'none'
				});
				// 只重置当前天的开始时间
				 form.time.week_time[day] = `${TIME_RANGE.start}-${getEndTime(time)}`;
				 return; // 验证失败，不再进行后续检查
			}
			
			// 验证结束时间
			if (!validateTimeRange(end)) {
				uni.showToast({
					title: `周${day === '7' ? '日' : ['一', '二', '三', '四', '五', '六'][parseInt(day)-1]}结束时间必须在${TIME_RANGE.start}-${TIME_RANGE.end}之间`,
					icon: 'none'
				});
				// 只重置当前天的结束时间
				form.time.week_time[day] = `${getStartTime(time)}-${TIME_RANGE.end}`;
				return; // 验证失败，不再进行后续检查
			}
			
			const startTime = new Date(`2000/01/01 ${start}`);
			const endTime = new Date(`2000/01/01 ${end}`);
			
			if (startTime >= endTime) {
				uni.showToast({
					title: `周${day === '7' ? '日' : ['一', '二', '三', '四', '五', '六'][parseInt(day)-1]}结束时间必须晚于开始时间`,
					icon: 'none'
				});
				// 重置为默认值，不再改变模式
				form.time.week_time[day] = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				// 不再执行模式切换
				// form.mode = modeValues[1];  // 重置为"明天预约"模式
				// modeIndex.value = 1;
			}
		}
	};

	// 修改处理明天预约时间选择的函数
	const handleTomorrowTimeChange = (type, timeValue) => {
		 // 仅当当前模式是 tomorrow 时才进行此验证
		if (form.mode === 'tomorrow') {
			// 验证时间是否在允许范围内
			if (!validateTimeRange(timeValue)) {
				uni.showToast({
					title: `明天预约时间必须在${TIME_RANGE.start}-${TIME_RANGE.end}之间`,
					icon: 'none'
				});
				// 可以选择不更新或重置，这里选择不更新
				// form.time.tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				return;
			}
			
			if (!form.time.tomorrow) {
				form.time.tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
			}
			
			const [start, end] = form.time.tomorrow.split('-');
			let newTime;
			if (type === 'start') {
				 // 确保更新后的结束时间也在范围内
				const newEnd = validateTimeRange(end) ? end : TIME_RANGE.end;
				newTime = `${timeValue}-${newEnd}`;
			} else {
				// 确保更新后的开始时间也在范围内
				 const newStart = validateTimeRange(start) ? start : TIME_RANGE.start;
				 newTime = `${newStart}-${timeValue}`;
			}
				
			// 验证开始时间是否早于结束时间
			const [newStart, newEnd] = newTime.split('-');
			const newStartTime = new Date(`2000/01/01 ${newStart}`);
			const newEndTime = new Date(`2000/01/01 ${newEnd}`);
			
			if (newStartTime >= newEndTime) {
				uni.showToast({
					title: '明天预约结束时间必须晚于开始时间',
					icon: 'none'
				});
				// 不更新时间，保持原状或重置为默认范围
				form.time.tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				return;
			}
			
			form.time.tomorrow = newTime;
		}
	};

	// 修改处理后天预约时间选择的函数
	const handleAfterTomorrowTimeChange = (type, timeValue) => {
		 // 仅当当前模式是 after_tomorrow 时才进行此验证
		if (form.mode === 'after_tomorrow') {
			// 验证时间是否在允许范围内
			if (!validateTimeRange(timeValue)) {
				uni.showToast({
					title: `后天预约时间必须在${TIME_RANGE.start}-${TIME_RANGE.end}之间`,
					icon: 'none'
				});
				// 可以选择不更新或重置，这里选择不更新
				// form.time.after_tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				return;
			}
			
			if (!form.time.after_tomorrow) {
				form.time.after_tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
			}
			
			const [start, end] = form.time.after_tomorrow.split('-');
			 let newTime;
			if (type === 'start') {
				 // 确保更新后的结束时间也在范围内
				 const newEnd = validateTimeRange(end) ? end : TIME_RANGE.end;
				newTime = `${timeValue}-${newEnd}`;
			} else {
				// 确保更新后的开始时间也在范围内
				 const newStart = validateTimeRange(start) ? start : TIME_RANGE.start;
				 newTime = `${newStart}-${timeValue}`;
			}
			
			// 验证开始时间是否早于结束时间
			const [newStart, newEnd] = newTime.split('-');
			const newStartTime = new Date(`2000/01/01 ${newStart}`);
			const newEndTime = new Date(`2000/01/01 ${newEnd}`);
			
			if (newStartTime >= newEndTime) {
				uni.showToast({
					title: '后天预约结束时间必须晚于开始时间',
					icon: 'none'
				});
				 // 不更新时间，保持原状或重置为默认范围
				form.time.after_tomorrow = `${TIME_RANGE.start}-${TIME_RANGE.end}`;
				return;
			}
			
			form.time.after_tomorrow = newTime;
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
		gap: 8px;
		padding: 4px 0;
	}

	.week-day-label {
		width: 45px;
		font-size: 14px;
		flex-shrink: 0;
		color: #333;
	}

	.time-picker-group {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		min-width: 0; /* 防止溢出 */
	}

	.time-picker {
		flex: 1;
		min-width: 0;
		background-color: #f9f9f9;
		border: 1px solid #dcdfe6;
		border-radius: 8px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		position: relative;
	}

	.time-picker::after {
		content: '';
		position: absolute;
		right: 8px;
		top: 50%;
		transform: translateY(-50%);
		width: 0;
		height: 0;
		border-left: 4px solid transparent;
		border-right: 4px solid transparent;
		border-top: 4px solid #666;
	}

	.picker-text {
		font-size: 14px;
		color: #333;
		text-align: center;
		width: 100%;
		padding: 0 24px 0 8px; /* 为下拉箭头留出空间 */
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.time-separator {
		color: #666;
		font-size: 14px;
		padding: 0 4px;
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

	.time-input {
		flex: 1;
		padding: 10px 12px;
		border: 1px solid #dcdfe6;
		border-radius: 8px;
		font-size: 15px;
		height: 40px;
		background-color: #f9f9f9;
		box-sizing: border-box;
	}
</style>