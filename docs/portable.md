# Portable 模式

## 目录约定

以启动 EXE 所在目录为基础目录；其中存在 `data/` 就启用 portable 模式。两个 EXE 使用同一规则，路径不依赖终端或 agent 的当前工作目录。源码运行时以仓库根目录为基础目录。

```text
Superuse/
  Superuse.exe
  SuperuseMCP.exe
  _internal/
  data/                   # 发布时为空；存在即启用 portable
    config.toml           # 首次启动生成，默认 real
    logs/
    state/control.json    # 人工保留状态
    runtime/broker.json   # 本次服务凭据，退出时清理
  config.example.toml
  PORTABLE.md
  MCP.md                  # agent 可直接读取的配置、工具与故障处理说明
  mcp-client.example.json
  mcp-client-http.example.json
  LICENSE
  NOTICE.md
  THIRD-PARTY-NOTICES.md
  license-inventory.json
  licenses/
```

没有 `data/` 时继续采用常规模式，Windows 运行数据使用 `%LOCALAPPDATA%\Superuse`。存在同名普通文件或失效的目录链接会明确报错。portable 目录不可写时直接失败，不会改用个人目录。

## 使用、升级和迁移

首次启动默认真实模式：程序常驻等待 SuperKVM 设备接入，不打开其他硬件。初始配置完整写入后才发布为 `data/config.toml`，不会覆盖已有配置。正常退出，按同目录 `config.example.toml` 编辑该文件；多组设备时在 `[device]` 中填入选定串口/视频稳定身份。从旧版本升级时，已有的 `device.mode="simulated"` 配置会在启动时自动迁移为 `real`（其余设置与人工保留状态原样保留），不存在任何让发布版运行模拟设备的入口。

MCP 配置指向同目录 `SuperuseMCP.exe`，通常只需 `--client-label`。serve、open、status、diagnose 和 MCP 共享目录发现规则。显式 `--config` 优先于 portable 配置，`--runtime-dir` 覆盖运行目录；自定义 runtime 时日志/状态仍沿用原来的 runtime 父目录规则。

ZIP 根目录附 [MCP 接入说明源文件](../packaging/MCP.md) 和 stdio/HTTP 两份配置示例，解压后 agent 直接读取 `MCP.md` 即可选择接入方式并按工具流程操作，无需源码仓库或 Python 环境。HTTP MCP 本机默认地址为 `http://127.0.0.1:18766/mcp`，远程使用需配置非空 `[network] key`，并由托盘开启局域网访问。

迁移前从托盘正常退出，再移动整个目录。配置、日志和人工保留状态随目录移动；运行凭据与 instance 每次启动重新生成，session、lease、job 不恢复，也不重放动作。人工保留状态不会因移动而自动交还，仍需在界面主动交还。

换电脑后设备路径和访问权限可能变化，应重新核对设备选择；MCP 宿主的 EXE 路径也需要更新。当前单实例约束仍然有效，不能同时运行多个副本来控制同一设备。

升级时替换程序文件，保留用户的 `data/`。官方构建流程只在 ZIP 中写入空 `data/`，不会携带默认配置去覆盖既有配置。不要通过删除 `data/` 绕过人工保留状态。

## 构建和验证

```powershell
uv sync --locked --extra app --extra packaging --dev
uv run --locked --extra app --extra packaging python scripts/build_portable.py
uv run --locked --extra app --extra packaging pytest tests/integration/test_portable_package.py -q
```

产物为 `dist/Superuse-<版本>-windows-x64-portable.zip` 及 `.zip.sha256`。脚本在新的构建临时目录中运行 PyInstaller，只接收两个 EXE 和 `_internal`，不重新压缩用户安装目录。发布时会包含项目、直接复用上游、Python 和构建环境依赖的原始许可与清单。

ZIP 迁移测试以真实模式等待设备运行，验证空 data 目录、异工作目录启动、MCP 自动发现、中文/空格路径迁移、旧 simulated 配置迁移、配置保留、旧 runtime 替换及人工保留状态；还验证无界面模式遇到配置错误或无法创建 runtime 时返回非零退出码。`--headless` 失败不会弹出阻塞自动化的错误对话框。它不代替干净 Windows 或跨机器的真实设备兼容测试。

`data/` 可能包含私人设备标识和运行凭据。发布新版始终运行构建脚本，不将使用过的目录直接压缩上传。
