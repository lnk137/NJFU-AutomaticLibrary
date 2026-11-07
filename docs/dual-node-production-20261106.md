# 双机生产部署与维护交接

最新独立验收：2026-11-07 10:09（Asia/Shanghai），17/17 PASS。双机探测、账号隔离、跨段冷却及监控告警修复已上线，见[发布报告](../development/outputs/booking-reliability-released-20261107-33j7lgpv.md)。本文件记录当前正式部署。

## 生效范围

- 13 个原有普通定时账号已迁移到双机选机。1 个启用的分段账号仍走原路径，另 4 个关闭的计划保持关闭；18 个账号的公开管理字段与切换前一致。
- 原计划由 Mac 发布到服务器唯一协调账本。服务器当时有 78 项未来任务，任务 ID、计划绑定哈希与当前计划逐项一致；它们是未来任务，并非 78 次成功预约。
- Mac、Ubuntu 均连接校园网。Ubuntu 通过原有反向 SSH 隧道维护；两机通过独立、受限的 SSH 身份访问服务器协调入口。学校凭据仅保存在两端各自加密的存储中，服务器不持有学校密码。
- 放号前两机探测网络和执行资格，协调器选定唯一主机。授权前的准备失败允许有界候选回退；签发授权后不改派，回执不确定时保留状态且禁止重放。工程验证不等于学校侧严格 exactly-once 保证。
- 已安排下一次 Mac 提前唤醒：2026-11-08 06:52；原 06:55 唤醒事件保留。下一轮原计划放号时间为 2026-11-08 07:00，预约使用日仍由原计划的 `day_offset` 决定。

## 版本与长期运行位置

当前双机节点及服务器源码为 94 文件，聚合 SHA256：

`681475d080e7d18f494263314a401f3fd664435dddded412afb577806656a4c0`

Mac 主服务的 `native-selection.json` 另外固定 4 个入口文件，共 98 文件；其 release SHA256 为（与上述聚合的范围和序列化定义不同）：

`2d02cc6188e257a4a8b5b4a7510360d5fe86315fc3439154b44e48a5cf4299d4`

| 组件 | 正在使用的位置或服务 |
| --- | --- |
| 主后台与计划发布器 | `/Users/a0000/项目/图书馆自动预约`；`org.njfu.autolib.power` |
| 发布器配置 | 主项目 `.local-data/dual-node-publisher.json` |
| Mac 节点 | `/Users/a0000/.local/share/njfu-dual-node/`；`org.njfu.autolib.dual-node` |
| Ubuntu 节点 | `/home/wls/.local/share/njfu-dual-node/`；用户服务 `njfu-dual-node.service` |
| Ubuntu 节点配置 | `/home/wls/.config/njfu-dual-node/node.json` |
| 原反向控制隧道 | Ubuntu 用户服务 `njfu-control-tunnel.service`，继续保留 |
| 服务器不可变源码 | `/opt/njfu-dual-node/releases/20261107-reliability-33j7lgpv` |
| 唯一协调账本 | `/var/lib/njfu-dual-node/state/authority`，外置 high-water anchor 位于其父目录 |
| Mac 提前唤醒 | `org.njfu.autolib.dual-probe.501`；`/Library/Application Support/NJFU-AutoLib-Dual-Probe/501` |

服务器 authority ID 为 `0bf1bb31872546bd87aa078ab832a571`。Mac/Ubuntu 实际 native 二进制分别固定为 `fbc9e73a9f9764d9f89a57a146223db35d147c50507a6ae4a5e96a31f1ae51cf`、`f3f76f608a0f3d6cd8dc08eb4c119369ddb9ac13f82d4fcd54cbfb153026009a`。

## 后续修改必须保留的行为

1. **以当前已集成后端作为修改基线。** 该版本同时保留邮件取消、iPhone/Safari 导航修复和分段功能。旧的 70 文件后端、旧工作区或早期候选覆盖部署会丢失双机提交隔离。源码、native 和选择清单的更新需作为同一候选验证，不能只更新哈希跳过验证。
2. **13 个账号的旧执行路径已持久隔离。** 原 native 的对应账号执行开关关闭；Python、手动、表单导入等旧提交入口也受保护。关闭发布器、停止节点或重启主后台都不能解除这一隔离。受管账号暂不支持切换为分段计划或借旧入口手动预约。
3. **保留所有执行历史与防重放状态。** 不删除或重新初始化服务器账本、签名密钥、high-water anchor、两端 runtime/grants/BurnJournal、主后台 native 账本及 managed registry。不能更换账号引用、恢复旧数据库或清理授权记录来重试任务。签发授权后结果不确定，应核对学校回执后人工处理。
4. 管理后台的暂停/恢复需取得服务器持久确认。`dual_control_unconfirmed` 代表尚未确认；`dual_pause_pending` 代表仍需等待执行主机确认。停止本机进程或 SSH 断开不能替代全局暂停，暂停也不撤销学校已接受的预约。
5. 普通计划和日期例外由发布器持续同步；已签发授权的任务不能替换为另一份计划。同密码重新登录保留 credential revision；**实际学校密码变更需要单独维护**：两端凭据、发布器注册与服务器登记的 credential revision 必须一致。当前没有自动轮换流程；版本不符会阻断该账号，不会回退到旧提交器。不要重新运行本次一次性凭据部署脚本。
6. 一次性切换与初始化脚本已经执行，不能作为日常启动脚本重复运行。冷备是诊断及受控恢复证据，不能直接恢复到当前授权账本之上。先核实已签发授权、节点本地消费和学校回执，再制定恢复方案。

## 验收证据与边界

最新发布证据位于 `development/work/booking-reliability-release-20261107-33j7lgpv/`，入口为 `final-verification.json`、`evidence/independent-acceptance.md`、`protection-checks.json`。旧发布证据保留作历史。

本轮隔离候选通过 402 个独立后端用例（435 次执行）、293 项前端回归及 H5 build。上线核对了实际新进程、98/94 文件与入口、计划、历史及防重账本。当前两节点在线、并发20、状态waiting（等待探测窗口），不能将同步/在线称为提交就绪。Mac首启预检失败后由30秒机制自动恢复，当前稳定，原诊断保留。节点日志位于 runtime/diagnostics/，本地告警位于 .local-data/dual-monitor-alerts.json，无新增邮件通道。

临时准入读取错误会阻断本轮同步并保留已发布任务，明确停用仍撤销。不可用或未排空的节点执行器会按健康状态退出，由 Ubuntu 的 on-failure/10秒或 Mac 的30秒调度恢复；保留原授权消费账本，不能以重启为由重放旧授权。正式环境未人为注入故障验证。

未来预约成功以学校回执为准。物理合盖/睡眠后的唤醒、校园网恢复及实际故障切换仍待真实场景观察。请保持两机接电、校园网可用，Mac 先开盖放置；当前系统唤醒事件不构成合盖可靠性的证明。
