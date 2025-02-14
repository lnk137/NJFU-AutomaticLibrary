# NJFU-AutomaticLibrary
基于 Flask 的自动化南京林业大学图书馆预约系统
## 项目介绍

    本项目使用Uniapp作为前端，Flask作为后端，实际运行时将Flask部署在云服务器或本地(不推荐)，通过js逆向出AES与RAS加密，使用webvpn进行预约，无需安装任何浏览器驱动，配置好后可以每天定时抢次日的图书馆座位
    
    能够保存用户账号信息和想要预约的时间和座位，每天固定时间自动预约次日座位（注意周五20:00闭馆）

## 环境依赖

    Python相关依赖已列在requirements.txt中
    
    需注意自行补充.env，config.js中内容
    .env结构请参考.env.example文件

## 项目结构

#### BackEnd

	├─blueprints          // Flask 蓝图，用于实现 API 功能
	├─db                  // 存放 SQLite 数据库文件
	├─log                 // 日志目录
	├─test                // 测试目录
	├─utils               // 工具函数
	├─座位信息              // 存储与座位相关的数据信息
	├─main.py             // 项目主入口，负责启动 Flask 应用和加载核心模块
	├─scheduled_task.py   // 定时任务脚本，用于处理预约
	├─.env.example   	  // 环境变量模版文件

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

## 界面展示

![页面_1](README.assets/%E9%A1%B5%E9%9D%A2_1.png)


## 运行方法

    1.终端输入git clone https://github.com/yourname/NJFU-AutomaticLibrary.git将项目克隆到本地
    
    2.在项目中输入命令python main.py启动本地服务器
    
    3.为scheduled_task.py配置定时启动任务
    
    4.(如果已经配置好前端)绑定学号后提交账号信息到数据库，之后保持本地服务器正常运行即可
      (如果不想配置前端)手动填写sqlite数据库的reservation_info表，之后保持本地服务器正常运行即可

## 联系方式                                               
	邮箱:lnk137@foxmail.com
## 许可证 
本项目根据 [GNU Affero 通用公共许可证 v3.0](https://www.gnu.org/licenses/agpl-3.0.html) 的条款进行许可

您可以自由使用、修改和分发本软件，但必须遵守 AGPL 的相关条款
