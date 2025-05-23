<template>
	<view class="content">
		<view class="header">预约记录</view>

		<!-- 预约结果信息 -->
		<view v-if="result" class="result-info">
			<text class="result-text">{{result}}</text>
		</view>

		<!-- 查询按钮 -->
		<button class="query-btn" @click="queryInfo" :disabled="loading">{{ loading ? '查询中...' : '查询记录' }}</button>

		<!-- 错误信息 (针对预约记录列表查询的错误) -->
		<view v-if="errorMessage" class="error-message">
			<text>{{ errorMessage }}</text>
		</view>

		<!-- 预约列表 -->
		<view class="reservation-list">
			<view v-if="!loading && !errorMessage && (!reservations || reservations.length === 0)" class="empty-tip">
				
			</view>
			<view v-else v-for="(reservation, index) in reservations" :key="index" class="reservation-item">
				<view class="reservation-info">
					<view class="info-row">
						<text class="label">座位：</text>
						<text class="value">{{reservation.devInfo.devName}}</text>
					</view>
					<view class="info-row">
						<text class="label">房间：</text>
						<text class="value">{{reservation.devInfo.roomName}}</text>
					</view>
					<view class="info-row">
						<text class="label">时间：</text>
						<text
							class="value">{{formatTimeRange(reservation.resvBeginTime, reservation.resvEndTime)}}</text>
					</view>
					<view class="info-row">
						<text class="label">状态：</text>
						<text class="value"
							:class="getStatusClass(reservation.resvStatus)">{{getStatusText(reservation.resvStatus)}}</text>
					</view>
				</view>
				<button v-if="canDelete(reservation.resvStatus)" class="delete-btn"
					@click="deleteReservation(reservation.uuid)">取消预约</button>
			</view>
		</view>
	</view>
</template>

<script setup>
	import { ref } from 'vue';
	import { onShow } from '@dcloudio/uni-app';
	import { server_url } from '@/config/config.js';

	const reservations = ref([]);
	const result = ref('');
	const errorMessage = ref(null); // 用于存储预约记录列表查询的错误信息
	const loading = ref(false); // 用于表示预约记录列表的加载状态

	// 从本地存储获取用户信息，这里只需要获取一次
	const userInfo = JSON.parse(uni.getStorageSync('userInfo') || '{}');

	// 格式化时间范围
	const formatTimeRange = (begin, end) => {
		if (!begin || !end) return '';
		const beginDate = new Date(begin);
		const endDate = new Date(end);
		
		const formatDate = (date) => {
			const month = String(date.getMonth() + 1).padStart(2, '0');
			const day = String(date.getDate()).padStart(2, '0');
			return `${month}-${day}`;
		};
		
		const formatTime = (date) => {
			const hours = String(date.getHours()).padStart(2, '0');
			const minutes = String(date.getMinutes()).padStart(2, '0');
			return `${hours}:${minutes}`;
		};
		
		// 检查是否同一天
		const isSameDay = beginDate.getDate() === endDate.getDate() &&
			beginDate.getMonth() === endDate.getMonth() &&
			beginDate.getFullYear() === endDate.getFullYear();
		
		if (isSameDay) {
			return `${formatDate(beginDate)} ${formatTime(beginDate)} - ${formatTime(endDate)}`;
		} else {
			return `${formatDate(beginDate)} ${formatTime(beginDate)} - ${formatDate(endDate)} ${formatTime(endDate)}`;
		}
	};

	// 获取状态文本和样式
	const statusMap = {
		1027: {
			text: '未开始',
			class: 'status-pending'
		},
		1029: {
			text: '待签到',
			class: 'status-reserve'
		},
		1093: {
			text: '已签到',
			class: 'status-check-in'
		},
		5313: {
			text: '已结束',
			class: 'status-ended'
		},
	};

	const getStatusClass = (status) => statusMap[status]?.class || '';
	const getStatusText = (status) => statusMap[status]?.text || status;
	// canDelete 函数根据新的状态码逻辑调整
	const canDelete = (status) => status === 1027;

	// 查询预约记录列表的方法 (由按钮点击触发)
	const queryInfo = async () => {
		// 重置列表状态和错误信息
		reservations.value = [];
		errorMessage.value = null; // 清空列表查询错误信息
		loading.value = true; // 设置加载状态

		try {
			// 检查用户信息，这是两个查询共享的逻辑
			if (!userInfo || Object.keys(userInfo).length === 0) {
				console.error('本地存储中没有用户信息，无法发送请求。');
				errorMessage.value = '请先在配置页面填写账号信息';
				return;
			}

			// 检查必要字段，这是两个查询共享的逻辑
			if (!userInfo.pid || !userInfo.vpn_password || !userInfo.lib_password) {
				console.error('本地存储用户信息不完整，缺少必要字段。');
				errorMessage.value = '请先在配置页面填写完整的账号信息';
				return;
			}

			const requestData = {
				'pid': userInfo.pid,
				'vpn_password': userInfo.vpn_password,
				'lib_password': userInfo.lib_password
			};
			console.log('发送预约记录查询请求的数据:', requestData);

			const response = await uni.request({
				url: `${server_url}/db/reservation/query_info`,
				method: 'POST',
				data: requestData,
				header: {
					'Content-Type': 'application/json',
				},
			});

			console.log('获取预约记录响应:', response);

			// 检查响应状态码
			if (response.statusCode !== 200) {
				const errorDetail = response.data?.error || `状态码: ${response.statusCode}`;
				throw new Error(`获取预约记录失败: ${errorDetail}`);
			}

			// 检查业务逻辑上的错误（如果后端在body里返回错误信息）
			if (response.data && response.data.error) {
                throw new Error(`获取预约记录失败: ${response.data.error}`);
            }

			// 成功获取预约记录
			if (response.data && response.data.reservations) {
				reservations.value = response.data.reservations;
			} else {
                // 没有预约记录，这不是错误，只是列表为空
                reservations.value = []; // 确保清空旧数据
            }

		} catch (e) {
			console.error('查询预约记录列表失败:', e);
			errorMessage.value = e.message || '获取预约记录失败，请稍后再试';
			reservations.value = []; // 确保错误时清空列表
		} finally {
			loading.value = false; // 结束加载状态
		}
	};

	// 获取预约结果信息 (页面显示时自动触发)
	const fetchResult = async () => {
		// fetchResult 仅更新 result.value，不处理错误，错误由 queryInfo 统一处理
		// 移除 queryInfo 内部的调用，改由 onShow 触发
		try {
			// 检查 pid 是否存在，这是获取预约结果的最低要求
			if (!userInfo || !userInfo.pid) {
				result.value = '请先在配置页面填写学号以获取预约结果'; // 提示用户填写学号
				return;
			}

			const response = await uni.request({
				url: `${server_url}/db/reservation/result`,
				method: 'POST',
				data: {
					'pid': userInfo.pid
				},
				header: {
					'Content-Type': 'application/json',
				},
			});

			console.log('获取预约结果响应:', response);

			// 成功获取结果或没有结果，都更新 result.value
			if (response.data && response.data.result) {
				result.value = response.data.result;
			} else {
                 result.value = '暂无预约结果'; // 如果没有结果，显示默认提示
            }
		} catch (e) {
			console.error('获取预约结果失败:', e);
			// 获取结果失败时，显示错误信息或者默认提示
			result.value = '获取预约结果失败，请稍后再试';
		}
	};

	// 删除预约 (保持原样，删除成功后调用 queryInfo 刷新列表)
	const deleteReservation = async (uuid) => {
		try {
			const res = await uni.showModal({
				title: '确认取消',
				content: '确定要取消这条预约记录吗？',
				confirmText: '确定',
				confirmColor: '#ff4d4f'
			});

			if (!res.confirm) return;

			if (!userInfo || Object.keys(userInfo).length === 0) {
				console.error('本地存储中没有用户信息，无法发送删除请求。');
				uni.showToast({
					title: '请先在配置页面填写账号信息',
					icon: 'none',
					duration: 3000
				});
				return;
			}

			if (!userInfo.pid || !userInfo.vpn_password || !userInfo.lib_password) {
				console.error('本地存储用户信息不完整，缺少必要字段。');
				uni.showToast({
					title: '请先在配置页面填写完整的账号信息',
					icon: 'none',
					duration: 3000
				});
				return;
			}

			const requestData = {
				'pid': userInfo.pid,
				'vpn_password': userInfo.vpn_password,
				'lib_password': userInfo.lib_password,
				'uuid': uuid
			};
			console.log('发送删除预约请求的数据:', requestData);

			const response = await uni.request({
				url: `${server_url}/db/reservation/delete`,
				method: 'POST',
				data: requestData,
				header: {
					'Content-Type': 'application/json',
				},
			});

			console.log('删除预约响应:', response);

			if (response.data.success) {
				uni.showToast({
					title: '取消成功',
					icon: 'success',
					duration: 2000
				});
				await queryInfo();
			} else {
				throw new Error(response.data.message || '取消失败');
			}
		} catch (e) {
			uni.showToast({
				title: '取消失败，请重试',
				icon: 'none',
				duration: 2000
			});
			console.error('取消预约失败:', e);
		}
	};

	// 页面显示时自动获取预约结果
	onShow(() => {
		fetchResult();
	});
</script>

<style scoped lang="less">
	.content {
		background-color: #E8F5E9;
		min-height: 100vh;
		padding: 20px;
	}

	.header {
		font-size: 32px;
		font-weight: bold;
		color: #535d52;
		margin: 20px 0;
		text-align: center;
	}

	.reservation-list {
		width: 100%;
		margin-top: 20px; /* 在按钮和结果/错误信息下方留白 */
	}

	.empty-tip {
		text-align: center;
		color: #666;
		font-size: 16px;
		margin-top: 40px;
	}

	.reservation-item {
		background: #fff;
		border-radius: 12px;
		padding: 15px;
		margin-bottom: 15px;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
	}

	.reservation-info {
		.info-row {
			display: flex;
			margin-bottom: 8px;

			.label {
				color: #666;
				width: 60px;
			}

			.value {
				color: #333;
				flex: 1;

				&.status-pending {
					color: #8c8c8c;  // 未开始 - 浅灰色
				}

				&.status-reserve {
					color: #1890ff;  // 待签到 - 蓝色
				}

				&.status-check-in {
					color: #52c41a;  // 已签到 - 绿色
				}

				&.status-ended {
					color: #595959;  // 已结束 - 深灰色
				}
			}
		}
	}

	.delete-btn {
		background-color: #ff4d4f;
		color: white;
		border: none;
		padding: 6px 12px;
		border-radius: 6px;
		font-size: 14px;
		margin-top: 10px;
		width: 100%;
	}

	.result-info {
		background: #fff;
		border-radius: 12px;
		padding: 15px;
		margin-bottom: 20px;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);

		.result-text {
			color: #535d52;
			font-size: 16px;
			line-height: 1.5;
			word-break: break-all;
		}
	}

	.query-btn {
		margin: 20px auto;
		width: 80%;
		background-color: #6b9cce;
		color: white;
		border-radius: 8px;
		height: 40px;
		line-height: 40px;
		text-align: center;
		font-size: 16px;
	}

	.error-message {
		text-align: center;
		color: #ff4d4f; /* 红色字体表示错误 */
		font-size: 14px;
		margin-top: 20px;
		padding: 10px;
		background-color: #fef0f0; /* 浅红色背景 */
		border: 1px solid #f56c6c; /* 红色边框 */
		border-radius: 8px;
	}
</style>