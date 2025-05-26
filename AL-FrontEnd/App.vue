<script>
	import {
		server_url
	} from '@/config/config.js';

	export default {
		onLaunch: function() {
			console.log('App Launch')
			// 检查更新
			this.checkUpdate();
		},
		onShow: function() {
			console.log('App Show')
		},
		onHide: function() {
			console.log('App Hide')
		},
		methods: {
			// 检查更新
			async checkUpdate() {
				// #ifdef APP-PLUS
				try {
					// 获取当前版本信息
					const currentVersion = plus.runtime.version;
					const currentVersionCode = plus.runtime.versionCode;
					
					console.log('当前版本信息：', {
						version: currentVersion,
						versionCode: currentVersionCode
					});

					// 从服务器获取最新版本信息
					const response = await uni.request({
						url: `${server_url}/app/check_update`,
						method: 'GET',
						data: {
							currentVersion,
							currentVersionCode
						}
					});
					
					console.log('服务器响应：', response);
					
					if (response.statusCode === 200 && response.data) {
						const {
							hasUpdate,
							latestVersion,
							latestVersionCode,
							downloadUrl,
							updateLog
						} = response.data;
						
						console.log('最新版本信息：', {
							hasUpdate,
							latestVersion,
							latestVersionCode,
							downloadUrl,
							updateLog
						});
						
						if (hasUpdate) {
							console.log('检测到新版本，当前版本：', currentVersion, '最新版本：', latestVersion);
							// 显示更新提示
							uni.showModal({
								title: '发现新版本',
								content: `最新版本：${latestVersion}\n更新内容：\n${updateLog}\n\n是否立即更新？`,
								confirmText: '立即更新',
								cancelText: '稍后再说',
								success: (res) => {
									if (res.confirm) {
										// 用户点击立即更新
										this.downloadAndInstall(downloadUrl);
									}
								}
							});
						}
					}
				} catch (error) {
					console.error('检查更新失败:', error);
				}
				// #endif
			},

			// 下载并安装更新
			downloadAndInstall(downloadUrl) {
				// #ifdef APP-PLUS
				uni.showLoading({
					title: '正在下载更新...'
				});

				const downloadTask = uni.downloadFile({
					url: downloadUrl,
					success: (res) => {
						if (res.statusCode === 200) {
							uni.hideLoading();
							// 安装应用
							plus.runtime.install(res.tempFilePath, {
								force: true
							}, () => {
								uni.showModal({
									title: '更新完成',
									content: '应用已更新完成，是否立即重启？',
									success: (res) => {
										if (res.confirm) {
											plus.runtime.restart();
										}
									}
								});
							}, (error) => {
								uni.showModal({
									title: '安装失败',
									content: '更新安装失败，请稍后重试',
									showCancel: false
								});
								console.error('安装更新失败:', error);
							});
						}
					},
					fail: (error) => {
						uni.hideLoading();
						uni.showModal({
							title: '下载失败',
							content: '更新下载失败，请检查网络后重试',
							showCancel: false
						});
						console.error('下载更新失败:', error);
					}
				});

				// 监听下载进度
				downloadTask.onProgressUpdate((res) => {
					if (res.progress === 100) {
						uni.hideLoading();
					}
				});
				// #endif
			}
		}
	}
</script>

<style>
	/*每个页面公共css */
</style>