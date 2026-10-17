# Superuse portable

解压整个 Superuse 目录，保留两个 EXE、_internal 和空的 data 目录。
双击 Superuse.exe 启动托盘，从托盘打开浏览器界面。

程序以 EXE 所在目录为基础：存在 data/ 就自动使用 portable 模式。
首次启动在 data/config.toml 写入模拟配置，默认不连接真实硬件。
配置字段见同目录 config.example.toml；停止程序后编辑 data/config.toml。
要使用真实设备，将 device.mode 改为 real，并确认只选择预期的 SuperKVM。

data/ 内保存 config.toml、logs/、state/ 和 runtime/。
所在目录必须可写；写入失败会报错，不会改为使用 AppData。
从任意工作目录启动两个 EXE，它们仍共享此处的 runtime。

迁移前从托盘正常退出，然后移动整个 Superuse 目录。
运行凭据每次服务启动重新生成，旧会话、租约与动作不会恢复；人工保留状态会保留。
换电脑后检查设备身份配置，并更新 agent 宿主中的 SuperuseMCP.exe 路径。
不要在程序运行中移动目录；同一登录会话仅运行一个 broker。

升级时正常退出，替换程序文件并保留自己的 data/。
发布 ZIP 的 data/ 是空目录，不会包含或覆盖用户配置。
不要把使用过的目录直接压缩公开；data/ 可能有设备标识、目标内容和运行凭据。

MCP 宿主启动同目录 SuperuseMCP.exe，参数为 --client-label agent-a。
首次启动仍为观看/模拟模式；真实输入及人工接管遵循应用的租约规则。

没有 data/ 时使用常规模式：运行数据保存到当前用户的 AppData。
--config 与 --runtime-dir 显式参数可覆盖各自路径，普通便携使用无需设置。
移除 data/ 不是交还人工控制的方法；应在应用界面主动交还。

LICENSE、NOTICE.md、THIRD-PARTY-NOTICES.md 和 licenses/ 保留项目与依赖许可。
