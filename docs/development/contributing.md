# 贡献指南

## 开发环境

在 Windows x64、Python 3.12 环境中，从仓库根目录执行：

```powershell
uv sync --locked --extra app --dev
uv run --locked python scripts/check_design.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
node --check web/app.js
node tests/web/browser-input.test.cjs
```

Node 仅用于前端开发检查，不是运行或打包依赖。测试服务需要独占应用的单实例锁，运行集成测试前正常退出本机已有 broker。默认测试不打开硬件，但部分 Windows 测试会打开模拟页面；仅跑不打开页面的回归可按[测试文档](testing.md)选择。

## 修改要求

开始前阅读 [AGENTS.md](../../AGENTS.md)、[架构](architecture.md)及相关契约，检查工作区状态。保留其他人的未提交改动。

- 改接口时同步 schema、模型、示例、文档与正反例；改时序时同步状态机和竞态测试。
- 每次写入在唯一 SerialActor 校验授权。不能让 Web、MCP 或托盘直接持有真实设备句柄。
- 绝不自动重放结果不确定的输入。`written` 不代表目标 UI 已确认。
- 真实设备测试需要明确操作范围和测试开关；CI 只使用模拟设备。
- 保留上游署名和 MIT 许可，新增移植修改记录到 `third_party/CHANGES.md`。
- 不提交真实配置、令牌、序列号、设备路径、原始诊断、截图和私人目录。

## 提交说明

说明具体行为变化、原因、测试结果和剩余边界。模拟结果与实机结果分开；硬件包、图像证据和性能数据不能凭旧报告继承。影响公开分发时，再按[发布指南](releasing.md)检查 Git 索引和构建产物。
