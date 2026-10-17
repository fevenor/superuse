# 测试目录

- `unit/`：控制、调度、坐标、协议、身份与可注入故障；不打开硬件。
- `integration/`：模拟 broker、HTTP/WS、官方 MCP SDK、Windows 单实例/ACL 与包冒烟。
- `web/`：Node 执行实际页面处理器的回归；不要求浏览器或硬件。
- `hardware/`：明确启用后才连接唯一匹配的 SuperKVM，默认 pytest/CI 排除。

检查命令与 C/D/V/I/H/W 行为用例见[测试与验收](../docs/development/testing.md)。运行服务集成测试前退出已有 broker，以免单实例锁冲突；部分 Windows 用例会打开模拟页面。

`unit/test_portable.py` 覆盖 EXE/data 发现、cwd/资源目录隔离、不可写不回退、配置原子生成与保留、启动失败清理和发布目录防污染。`integration/test_portable_package.py` 在构建真实 ZIP 后验证解压、MCP 自动发现、迁移与凭据轮换，以及无界面启动失败正确退出；使用模拟设备，未构建 ZIP 时跳过。

前端检查：`node --check web/app.js`、`node tests/web/browser-input.test.cjs`。Node 仅用于开发，覆盖位移累加/按钮顺序、捕获与模式切换、失焦释放、暂停交还、旧事件/连接、续租和最新帧解码。

硬件测试必须显式设置 `SUPERUSE_ALLOW_HARDWARE_TESTS=1`，使用 `-o addopts= -m hardware` 选择。先运行预览并检查目标；会操作桌面的用例还需 `SUPERUSE_HARDWARE_DESKTOP_CONFIRMED=1`。测试不终止厂商程序，开始前让其他应用断开串口/视频。

`test_real_relative_pointer_probe` 与 `test_real_relative_mcp_and_human_input` 只发送小幅无按钮相对移动和释放，不尝试解锁。其他用例可能点击、按键、重连句柄，运行前阅读对应函数。证据写入忽略的 `artifacts/hardware/`，可能包含目标内容和设备身份，不能公开上传。

自动断言、串口写出与目标视觉确认分别报告；用例通过不代表所有按钮、拖动/滚轮或长期稳定性已通过。
