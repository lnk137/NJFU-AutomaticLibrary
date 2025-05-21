<template>
	<view class="content">
		<view class="header">预约记录</view>

		<!-- 预约列表 -->
		<view class="reservation-list">
			<view v-if="!reservations || reservations.length === 0" class="empty-tip">
				暂无预约记录
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
	import {
		ref
	} from 'vue';
	import {
		onShow
	} from '@dcloudio/uni-app';
	import {
		server_url
	} from '@/config/config.js';

	const reservations = ref([]);

	const userInfo = JSON.parse(uni.getStorageSync('userInfo') || '{}');

	// 格式化时间范围
	const formatTimeRange = (begin, end) => {
		if (!begin || !end) return '';
		const formatTime = (timeStr) => {
			const date = new Date(timeStr);
			return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
		};
		return `${formatTime(begin)} - ${formatTime(end)}`;
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
		5313: {
			text: '已结束',
			class: 'status-ended'
		},
	};

	const getStatusClass = (status) => statusMap[status]?.class || '';
	const getStatusText = (status) => statusMap[status]?.text || status;
	// canDelete 函数根据新的状态码逻辑调整
	const canDelete = (status) => status === 1027;

	// 获取预约记录
	const fetchReservations = async () => {
		try {
			// 从本地存储获取用户信息
			// 检查 userInfo 对象是否存在
			if (!userInfo || Object.keys(userInfo).length === 0) {
				console.error('本地存储中没有用户信息，无法发送请求。');
				uni.showToast({
					title: '请先在配置页面填写账号信息',
					icon: 'none',
					duration: 3000
				});
				return;
			}

			// 检查必要字段是否存在
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

			if (response.data && response.data.reservations) {
				reservations.value = response.data.reservations;
			}
		} catch (e) {
			uni.showToast({
				title: '获取预约记录失败',
				icon: 'none',
				duration: 2000
			});
		}
	};

	// 删除预约
	const deleteReservation = async (uuid) => {
		try {
			const res = await uni.showModal({
				title: '确认取消',
				content: '确定要取消这条预约记录吗？',
				confirmText: '确定',
				confirmColor: '#ff4d4f'
			});

			if (!res.confirm) return;

			// 检查 userInfo 对象是否存在
			if (!userInfo || Object.keys(userInfo).length === 0) {
				console.error('本地存储中没有用户信息，无法发送删除请求。');
				uni.showToast({
					title: '请先在配置页面填写账号信息',
					icon: 'none',
					duration: 3000
				});
				return;
			}

			// 检查必要字段是否存在
			if (!userInfo.pid || !userInfo.vpn_password || !userInfo.lib_password) {
				console.error('本地存储用户信息不完整，缺少必要字段。');
				uni.showToast({ // 可以合并或修改提示信息
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
				await fetchReservations();
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

	// 页面显示时自动获取预约记录
	onShow(() => {
		fetchReservations();
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

				&.status-reserve {
					color: #1890ff;
				}

				&.status-check-in {
					color: #52c41a;
				}

				&.status-check-out {
					color: #999;
				}

				&.status-cancel {
					color: #ff4d4f;
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
</style>