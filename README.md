# Superuse

通过 SuperKVM 在 Windows 上查看并操作另一台机器，同时向 agent 提供 MCP。浏览器、托盘和多个 agent 共享一个设备后端；人工可显式接管，并在完成操作后主动交还。

Superuse 适配 CDC `1209:0001` 与视频 `345F:2131`（`SK USB3 Video`）这一组设备。被控端通过 HDMI/USB 连接，无需安装组件，不限定目标操作系统。

## 功能与边界

- 通知区域托盘、浏览器视频和键鼠操作、相对/绝对鼠标、组合键与全屏。
- MCP stdio 截图、键盘、ASCII 文本及有界鼠标动作。
- 单一 broker 持有设备，按会话/租约仲裁，人工接管撤销旧输入。
- 默认模拟模式；真实硬件必须显式启用且唯一匹配身份。

项目处于开发阶段。真实视频、相对移动、右键、键盘与文档保存流程已经实测；拖动/滚轮、长稳、干净环境分发及更多浏览器兼容仍需验证。详见[验证状态](docs/development/validation.md)。音频、剪贴板、文件传输、任意 Unicode、OTA 和远程网络服务不在当前范围。

## Portable 便携版

将 Windows x64 portable ZIP 整体解压，双击 `Superuse.exe`。EXE 同目录下存在 `data/` 就自动使用便携模式，配置、日志、人工保留状态与运行文件均放在其中；不依赖启动工作目录，也不回退到 AppData。

发布包自带空的 `data/`。首次启动生成 `data/config.toml`，默认模拟；停止应用后编辑配置以启用真实设备。正常退出后可移动整个目录；迁移后检查设备身份并更新 MCP EXE 路径。升级时保留自己的 `data/`。操作与构建见 [portable 说明](docs/portable.md)。

## 快速开始

Windows x64、Python 3.12、uv；在仓库根目录的 PowerShell 中运行：

```powershell
uv sync --locked --extra app --dev
uv run --locked superuse serve
```

程序默认进入托盘并使用模拟设备。右键图标选择“打开界面”，或另开终端运行 `uv run --locked superuse open`。

连接真实设备前，让其他应用断开串口/视频，复制并编辑本机配置：

```powershell
Copy-Item config.example.toml superuse.local.toml
uv run --locked superuse diagnose
uv run --locked superuse serve --real --config superuse.local.toml
```

只有唯一匹配设备才连接，不会改开其他串口或内置摄像头。真实配置、运行凭据、目标截图与日志不应提交。配置与操作步骤见[使用指南](docs/user-guide.md)。

鼠标默认相对移动：接管后点击画面捕获，Esc 释放捕获但保留控制权；切换模式也保留租约。**若绝对模式不能正常移动，请切换相对模式。** 窗口失焦/隐藏或断连会进入人工暂停，需重新接管或显式交还。

## MCP 接入

先启动 broker，再由 agent 宿主启动 stdio 桥：

```powershell
uv run --locked superuse mcp --client-label agent-a
```

打包版使用 `SuperuseMCP.exe`。参考 [MCP 配置示例](examples/mcp-client.example.json)替换安装路径，不需填写令牌。各桥共享一个 broker；工具不会自动申请控制权。典型顺序为截图 → `acquire_control` → 有界输入 → 新截图确认 → `release_control`。

相对位移为 HID 计数，绝对坐标须使用截图的 `basis_id`。`written` 仅表示报告已写出，不能代替目标 UI 确认。

## 开发和构建

```powershell
uv run --locked python scripts/check_design.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
node tests/web/browser-input.test.cjs
```

Node 仅用于开发检查。默认 pytest 排除硬件；运行集成测试前退出已有 broker，部分 Windows 用例会打开模拟页面。细节见[贡献指南](CONTRIBUTING.md)和[测试文档](docs/development/testing.md)。

```powershell
uv sync --locked --extra app --extra packaging --dev
uv run --locked --extra app --extra packaging pyinstaller packaging/superuse.spec --noconfirm
```

产物为 `dist/Superuse/`，含托盘 `Superuse.exe` 与 stdio `SuperuseMCP.exe`，运行时需要整个目录。[发布指南](docs/development/releasing.md)说明许可证、敏感信息检查和包验收。

构建带空 `data/`、依赖许可和 SHA-256 校验文件的 portable ZIP：

```powershell
uv run --locked --extra app --extra packaging python scripts/build_portable.py
```

## 文档与许可

从[文档导航](docs/README.md)查找使用、架构、接口与故障排查。自有代码采用 [MIT](LICENSE)；上游来源及独立许可见 [NOTICE](NOTICE.md) 与[上游复用说明](docs/third-party.md)。
