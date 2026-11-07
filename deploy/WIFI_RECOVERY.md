# @f-Young 恢复任务

用户级 LaunchAgent 每天本机时间 06:55 启动，程序仅在北京时间 06:55–06:59 检查。仅当现有电源保护启用、未暂停，且预约后台的运行和时钟证据新鲜有效时工作。06:56:30 以前可尝试开启 Wi-Fi 或加入 `@f-Young`，最多三次；06:57 预登录及 07:00 放号期间不切换网络。SSID 因系统权限无法确认且接口仍有地址时，不强制重连。

校园网认证页 `http://10.11.2.3/` 仅作只读检查。程序不读取钥匙串、不提交凭据、不注销会话；认证失效时仍需人工登录。学校网站使用系统默认路由探测，VPN 可影响这一结果。关机、未登录 macOS、无法唤醒、权限或校园网认证失效都可能使预约失败。

查看当前检查结果：

```sh
/Users/a0000/项目/图书馆自动预约/.venv/bin/python -B /Users/a0000/项目/图书馆自动预约/deploy/wifi_recovery.py inspect
```

查看最近一次定时检查（含 `checked_at`，不存在时为 `not_checked`）：

```sh
/Users/a0000/项目/图书馆自动预约/.venv/bin/python -B /Users/a0000/项目/图书馆自动预约/deploy/wifi_recovery.py status
```

`portal` 为 `authenticated` 代表认证页返回明确的已登录标记；`login_required` 代表检测到认证表单；`unknown`、`unavailable` 均不能证明已登录。`association` 为 `unknown` 时不代表断网。

暂停此独立任务：

```sh
launchctl bootout gui/$(id -u)/org.njfu.autolib.wifi-recovery
```

重新加载：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.njfu.autolib.wifi-recovery.plist
```

这些命令不改变原预约计划、已有唤醒组件或预约记录；再次登录时系统仍会加载保留的 LaunchAgent plist。若要永久停用，须另外处理该 plist。
