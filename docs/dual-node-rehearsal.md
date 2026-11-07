# Dual-node selection and single-owner rehearsal

独立实验工具位于 `experimental/dual_node_rehearsal/`，用于校验 Mac/Ubuntu 只读探测报告、选择模拟执行主机，并验证同一个本地 SQLite 账本只产生一个 owner 和一次消费。它使用 `fixture-*` 合成预约意图，不连接现有预约后台。

所有结果都固定包含：

```json
{
  "mode": "dual_node_rehearsal_v1",
  "rehearsal_only": true,
  "live_execution_authorized": false
}
```

## Run the offline demo

从项目根目录执行，`--directory` 必须是尚不存在的绝对路径：

```bash
.venv/bin/python -B -m experimental.dual_node_rehearsal demo \
  --directory /absolute/new/rehearsal-evidence
```

程序写入合成报告、选机结果、分配及消费竞争记录、终态和验收摘要。每轮使用 4 个真实子进程、两批各 4 个任务，经 barrier 同步后竞争同一账本；每行记录 PID。8 次分配只应创建 1 条决定，8 次消费只应成功 1 次。失败或超时会终止并等待本轮子进程，已生成的诊断证据保留。

演练故意向同一账本注入冲突的候选资格，因此 `policy_selected_node` 和 `race_owner` 可以不同；`conflict_injected=true` 会明确标记这一点。虚拟时间用于覆盖放座与过期边界，演练不等待真实放座时间，也不发送网络或预约请求。

## Import real read-only reports

`evaluate` 只评估报告；`freeze` 才写模拟账本。输入路径必须是绝对路径；拒绝符号链接、硬链接、重复 JSON 字段和非有限数字。输出只创建新文件，不覆盖已有文件。

```bash
.venv/bin/python -B -m experimental.dual_node_rehearsal evaluate \
  --intent /absolute/fixture-intent.json \
  --mac-report /absolute/mac-samples.json \
  --ubuntu-report /absolute/ubuntu-samples.json \
  --eligible mac ubuntu --incumbent mac \
  --output /absolute/new/evaluation.json
```

`fixture-intent.json` 的字段示例可从 demo 生成的 `intent.json` 获取。真实报告由 [只读探测工具](dual-node-readonly-probe.md) 提供；报告的形状和哈希不会证明主机身份或学校账号具备预约资格。`--eligible` 是操作者显式提供的模拟资格。

选机要求：

- 两份原始报告的版本、固定端点、参数、采样窗口及 CAPTCHA 状态一致；重新计算统计，不采用保存的摘要。
- 每台至少 3 个成功样本，成功率至少 75%。每个参与统计的成功观测以及最后一次观测都须在 180 秒内，允许最多 5 秒主机时间差。
- 报告结束时间最多比最后一次实际采样结束晚 5 秒。修改报告头不能刷新旧样本。
- 只在声明放座时间前 305–900 秒冻结，放座钟点须出现在两份报告的保护时刻表中；重新执行安静窗口检查。
- 默认 6 次、间隔 10 秒的报告符合采样跨度要求。原探测器仍可输出更长报告，但包含超过新鲜度限制的成功观测时，选择器会拒绝整份报告。
- 无明显网络偏好时保留指定 incumbent；单一显式合格节点优先于网络排序。

## Ledger behavior

```bash
.venv/bin/python -B -m experimental.dual_node_rehearsal init \
  --state /absolute/new/private-ledger
```

账本目录必须新建且私有；打开已有账本不会重新初始化缺失或损坏状态。完整意图决定绑定哈希，任务 ID 则不包含座位、版本或主机。因此同一任务换座位或版本会发生绑定冲突，不能借此创建第二个 owner。

`freeze` 接受与 `evaluate` 相同输入，并增加 `--state`。`consume` 使用保存的 assignment、`--state` 和 `--node`；`finish` 再提供 `--status revoked|uncertain|completed`；`inspect` 使用 `--task-id`。各子命令的 `--help` 提供完整参数。参数错误只返回固定错误码，不回显传入值。

有效账本和当前时间通过检查后，先在同一把文件锁内持久化观察到的最大墙钟时间，再进行独立业务事务。业务拒绝或回滚会保留时间记录，但不改变任务；之后的时钟回退会被拒绝。无效时间、身份检查失败、锁竞争或时间提交失败不会进入业务事务。

消费仅允许 owner 使用匹配 ticket，在放座时间至其后 30 秒内成功一次。过期、撤销、完成或结果不确定后均保留原 owner，不提供转移、重置或删除 API。结果不确定时不会再授权另一台主机。

## Validation scope

同一个冻结候选已分别在 Mac 与 Ubuntu 上执行相关测试和实际子进程演练。Ubuntu 测试使用 SSH 下发的小型临时源码目录，复用现有 Python；不会安装服务或改变自动预约。

两台机器上的演练各自只使用一个本地账本。它们不是共享账本或跨主机互斥验收，不能据此声称分布式 exactly-once、远程 fencing、数据库复制/NFS 安全、可信时钟、学校账号准备就绪或真实预约成功。当前 CLI 不向生产 native executor 发出授权。

最终候选哈希、两端测试、PID 竞争记录、源码和服务保护检查保存在 `development/work/dual-node-rehearsal-20261105-zw4hhwc3/`；以 `acceptance-final-20261106.json` 为本轮汇总，旧的中断与失败记录保留作历史证据。

本地回归命令：

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_dual_node_rehearsal_*.py' -v
.venv/bin/python -B -m unittest discover -s tests -p 'test_dual_node_probe.py' -v
```
