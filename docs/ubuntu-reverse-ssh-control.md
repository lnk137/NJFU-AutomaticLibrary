# Ubuntu reverse SSH control

Mac 可以通过现有雨云服务器登录校园网 Ubuntu，执行命令及传输文件。图形桌面继续使用向日葵。

## Connect

在本机终端执行：

```bash
ssh -F /Users/a0000/.ssh/njfu-ubuntu-control/config njfu-ubuntu
```

执行单条命令或使用 SFTP：

```bash
ssh -F /Users/a0000/.ssh/njfu-ubuntu-control/config njfu-ubuntu 'uname -s'
sftp -F /Users/a0000/.ssh/njfu-ubuntu-control/config njfu-ubuntu
```

连接配置使用独立文件，没有修改全局 SSH 配置。无需保持本次管理员维护连接。

## Connection and lifecycle

Ubuntu 主动连接服务器 `183.66.27.20:23486`，将服务器 `127.0.0.1:15222` 转到 Ubuntu `127.0.0.1:22`。Mac 使用专用跳转账号进入这个回环端口，再独立验证 Ubuntu 主机身份和 `wls` 登录公钥。

- `njfu-ubuntu-tunnel`：仅允许建立指定反向监听。
- `njfu-ubuntu-jump`：仅允许转到指定控制端口。
- 两个服务器账号均禁止 shell、SFTP、PTY、agent 和 X11 转发。
- 专用私钥留在生成它的电脑；服务器只保存相应公钥。
- 原有预约和收件端口 `15003–15006` 保持原用途。

Ubuntu 用户服务 `njfu-control-tunnel.service` 已启用，连接退出后约 10 秒重试。机器原有 `Linger=yes` 保持不变，用户服务可以在无桌面登录时运行。实际重启、断电恢复和睡眠唤醒未验收；关机或断网期间无法连接。

查看状态：

```bash
ssh -F /Users/a0000/.ssh/njfu-ubuntu-control/config njfu-ubuntu \
  'systemctl --user status njfu-control-tunnel.service --no-pager'
```

## Files

| Host | Path | Purpose |
| --- | --- | --- |
| Mac | `/Users/a0000/.ssh/njfu-ubuntu-control/` | 私有连接配置、两把专用私钥、公钥和主机信任记录 |
| Ubuntu | `/home/wls/.ssh/njfu-control/` | 隧道私钥、配置和服务器主机信任记录 |
| Ubuntu | `/home/wls/.ssh/authorized_keys` | 追加本次 Mac 登录公钥，保留原内容 |
| Ubuntu | `/home/wls/.config/systemd/user/njfu-control-tunnel.service` | 自动重连服务 |
| Server | `/etc/ssh/sshd_config.d/80-njfu-ubuntu-control.conf` | 两个新账号的独立权限限制 |
| Server | `/var/lib/njfu-ubuntu-tunnel/.ssh/authorized_keys` | 隧道账号公钥 |
| Server | `/var/lib/njfu-ubuntu-jump/.ssh/authorized_keys` | 跳转账号公钥 |
| Server | `/var/lib/njfu-control-setup-20261105/` | 部署前后记录与恢复证据 |

本次源文件、模板和验收记录保存在项目 `development/work/ubuntu-reverse-ssh-20261105-6woo3k3k/`。其中早期 `validation.json` 是部署前记录，实际结果见 `acceptance.json`。

## Stop and recover

通过 Ubuntu 本地终端或向日葵停用服务：

```bash
systemctl --user disable --now njfu-control-tunnel.service
```

恢复：

```bash
systemctl --user enable --now njfu-control-tunnel.service
```

停用会切断这条控制通道；保留向日葵作为恢复入口。完整撤销应按部署记录核对两个新账号、配置及本次追加的公钥行，再逐项撤销。不要删除原有 SSH 文件或停止其他 SSH 连接。

## Acceptance

2026-11-05 已实际验证：严格主机身份校验下的 SSH 登录、32 KiB SFTP 往返及 SHA-256 一致、两个中转账号的 8 项越权连接拒绝，以及只断开本次隧道后的自动重连。一次测试在 13.861 秒后恢复，不能据此保证所有网络故障的恢复时间。

控制通道不授予真实预约操作权限；双机选机和单机执行演练仍是独立候选工作。
