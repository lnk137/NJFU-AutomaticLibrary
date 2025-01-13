<template>
	<view class="content" :style="{ backgroundImage: backgroundLoaded ? `url(${backgroundImage})` : 'none' }">
		<view class="glass">
			<input v-model="studentId" class="input" type="number" placeholder="请输入学号（10位数字）" maxlength="10" />
			<view class="button-container">
				<button class="button" @click="bindStudentId">绑定学号</button>
				<button class="button" @click="unbindStudentId">解除绑定</button>
			</view>
		</view>
	</view>
</template>

<script setup>
	import {
		ref,
		onMounted
	} from 'vue';

	const studentId = ref(''); // 存储输入的学号
	// 背景图片加载状态
	const backgroundLoaded = ref(false);
	const backgroundImage = "/static/b-i.png"; // 背景图片路径
	// 在组件挂载后获取本地存储的学号
	onMounted(() => {
		const storedStudentId = uni.getStorageSync('studentId'); // 获取本地存储的学号
		if (storedStudentId) {
			studentId.value = storedStudentId; // 填充到输入框中
		}
		console.log('storedStudentId:', storedStudentId);
		uni.getImageInfo({
			src: backgroundImage,
			success: () => {
				backgroundLoaded.value = true; // 图片加载完成后设置为已加载
			},
			fail: (err) => {
				console.error("背景图片加载失败", err);
			},
		});
	});

	// 绑定学号并保存到本地存储
	const bindStudentId = () => {
		if (studentId.value.trim()) {
			uni.setStorageSync('studentId', studentId.value);
			uni.showToast({
				title: '学号绑定成功',
				icon: 'success',
				duration: 2000,
			});
		} else {
			uni.showToast({
				title: '请输入有效的学号',
				icon: 'none',
				duration: 2000,
			});
		}
	};

	// 解除绑定，删除本地存储中的学号
	const unbindStudentId = () => {
		uni.removeStorageSync('studentId');
		studentId.value = ''; // 清空输入框中的学号
		uni.showToast({
			title: '解除绑定成功',
			icon: 'success',
			duration: 2000,
		});
	};
</script>

<style scoped>
	.content {
		display: flex;
		justify-content: center;
		align-items: center;
		height: 100vh;
		background: #A2C8DA;

		background-size: cover;
		background-attachment: fixed;
	}

	.glass {
		background: rgba(255, 255, 255, 0.5);
		/* 半透明背景 */
		border-radius: 10px;
		padding: 20px;
		width: 80%;
		max-width: 400px;
		backdrop-filter: blur(10px);
		/* 毛玻璃效果 */
		display: flex;
		flex-direction: column;
		align-items: center;
	}

	.input {
		width: 100%;
		padding: 10px;
		margin-bottom: 20px;
		border-radius: 5px;
		border: 1px solid #bcbcbc;
		font-size: 16px;
	}

	.button-container {
		display: flex;
		align-items: center
	}

	/* 修改后的按钮样式 */
	.button {
		background: rgba(255, 255, 255, 0.2);
		/* 半透明背景 */
		color: #265ac2;
		font-size: 16px;
		font-weight: bold;
		text-align: center;
		margin: 15px;

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

	/* Hover 样式 */
	.button:hover {
		transform: scale(1.1);
		background: rgba(255, 255, 255, 0.3);
		/* 增加背景透明度 */
		box-shadow: 0px 6px 12px rgba(0, 0, 0, 0.3);
	}



	/* 解除绑定按钮的特殊样式 */
	.button:last-child {
		background-color: rgba(255, 255, 255, 0.3);
		color: #ff3b30;
		border-color: rgba(255, 255, 255, 0.5);
	}
</style>