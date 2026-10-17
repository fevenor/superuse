# 使用指南

## 环境与设备

控制端使用 Windows x64，源码运行需要 Python 3.12 和 uv。目标设备需提供 SuperKVM CDC `1209:0001` 与 `SK USB3 Video` `345F:2131`。软件按这些身份选择设备，不将其作为任意 USB KVM 的通用驱动。

被控端通过 HDMI 输出视频，并通过 USB 接收键鼠；无需安装 Superuse。目标输入布局、输入法、固件和绝对鼠标兼容性会影响行为。ASCII 文本输入应使用 US 键盘布局及英文输入状态。

## 先以模拟模式运行

在仓库根目录的 PowerShell 中运行：

```powershell
uv sync --locked --extra app --dev
uv run --locked superuse serve
```

程序进入通知区域，右键图标选择“打开界面”。也可另开终端执行 `uv run --locked superuse open`。模拟模式不会打开真实串口或视频设备，页面会标明模拟来源。

`--headless` 适合调试和测试，不显示托盘；普通使用保持托盘入口即可。`status` 输出脱敏的服务发现信息，`diagnose` 只读枚举设备，不打开摄像头。

## 连接真实设备

先让其他占用设备的应用断开串口/视频连接，再创建本机配置：

```powershell
Copy-Item config.example.toml superuse.local.toml
uv run --locked superuse diagnose
```

示例配置默认模拟，真实模式需显式开启。若只有一组匹配设备，空的 `serial_number`、`device_path` 可用于唯一匹配；存在多组设备时应填入本机稳定身份。完整视频路径可通过 `superuse.device.discovery.list_video_devices()` 只读枚举，不能将诊断中的名称顺序当作采集索引。设备标识只保存在本机配置中。

```powershell
uv run --locked superuse serve --real --config superuse.local.toml
```

配置文件需要通过 `--config` 显式指定；源码入口不会自动读取任意当前目录配置。启动时会中和输入，随后开放观看和控制。找不到设备、多设备匹配或设备被占用会报错，不会改开其他串口/内置摄像头或回退模拟模式。

## 浏览器操作

页面打开后先进入观看模式。点击“接管”，等待释放屏障完成后才能发送键鼠输入。人工接管会撤销 agent 的旧租约；agent 后续输入得到明确失权结果。

鼠标默认相对移动，模式偏好会保存在浏览器本地：

1. 接管后点击画面捕获鼠标；这一次点击不会发往目标。
2. 正常移动、点击或使用滚轮。
3. 按 Esc 释放捕获；当前控制权保留，再次点击画面可继续。

可切换绝对坐标模式。**若绝对模式不能正常移动，请切换相对模式**；无需改变设备固件设置。模式切换会释放已按下的键鼠，保留当前租约。

窗口真正失焦、隐藏或输入连接中断时，会释放输入并进入“人工暂停”。此时仍阻止 agent，需要人工重新“接管”或点击“交还”。只有显式交还才解除人工保留，不会因 agent 下一次请求而自动交还。

工具栏可发送 Ctrl+Alt+Del、Alt+Tab，切换显示比例与全屏。浏览器可能截获本机系统快捷键，使用工具栏发送这些组合键更直接。

## MCP 接入

先启动一个 broker，再让各 agent 宿主分别启动一个 stdio 桥：

```powershell
uv run --locked superuse mcp --client-label agent-a
```

打包版使用 `SuperuseMCP.exe`。可复制 [MCP 配置示例](../examples/mcp-client.example.json)，替换为实际安装路径；配置内不填写令牌。使用自定义 runtime 目录时，serve 与 MCP 的 `--runtime-dir` 必须一致。

agent 的典型调用顺序：`get_status` → `capture_screen` → `acquire_control` → 有界输入 → 新截图确认 → `release_control`。输入工具不会自动申请控制权。

绝对坐标工具使用截图返回的 `basis_id` 和该截图的像素坐标。相对工具使用 HID 计数，受到目标加速度和边界影响；应小幅移动后重新观察，不能将计数直接当作像素。接口返回 `written` 只表示报告已写出，目标是否完成操作应通过新截图确认。

## 文件与退出

默认运行文件位于 `%LOCALAPPDATA%\Superuse`，包括 runtime 凭据、人工保留状态和轮转日志。不要上传整个 runtime 文件、真实配置或目标截图。配置变更需要重启服务；右键托盘选择“退出”可正常释放设备。

更多问题见[故障排查](troubleshooting.md)，高级运行机制见 [Windows 文档](development/windows-operations.md)。
