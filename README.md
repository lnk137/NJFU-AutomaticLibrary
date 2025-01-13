# 项目介绍

    本项目使用Uniapp作为前端，Flask作为后端，实际运行时将Flask部署在云服务器或本地(不推荐)
    
    能够保存用户账号信息和想要预约的时间和座位，每天固定时间自动预约次日座位（注意周五20:00闭馆）

# 环境依赖

    Python相关依赖已列在requirements.txt中
    
    需注意自行补充config包中内容，config.py中包括各种路径变量，config.js中包括url变量

# 项目结构

#### BackEnd

	├─blueprints          // Flask 蓝图，用于实现 API 功能
	├─config              // 配置文件，包含各种路径变量
	├─db                  // 存放 SQLite 数据库文件
	├─log                 // 日志目录
	├─test                // 测试目录
	├─utils               // 工具函数
	├─座位信息              // 存储与座位相关的数据信息
	├─main.py             // 项目主入口，负责启动 Flask 应用和加载核心模块
	├─scheduled_task.py   // 定时任务脚本，用于处理预约

#### FrontEnd

    ├─components          // 存放自定义组件
    ├─config              // 包含服务器ip
    ├─pages               // 页面文件目录，每个子目录对应一个页面
    │  ├─account_info     // 用户账号信息页面
    │  ├─announcement     // 公告页面
    │  ├─index            // 首页
    │  ├─my               // 个人中心，用于学号绑定
    │  └─test             // 测试页面
    ├─static              // 静态资源目录，存放图片、图标等静态文件
    │  └─tabbar           // TabBar 图标文件，存放底部导航栏图标
    ├─stores              // 状态管理目录，存放 Pinia 状态管理文件
    ├─utils               // 工具函数目录
