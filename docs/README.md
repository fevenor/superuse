# 文档导航

Superuse 是 Windows 上的 SuperKVM 控制软件。被控端通过 HDMI/USB 连接，无需安装配套 agent；是否能正确接收某类键鼠输入，仍取决于目标系统和设备兼容性。

## 使用

| 文档 | 解决的问题 |
|---|---|
| [使用指南](user-guide.md) | 安装依赖、模拟运行、连接真实设备、浏览器操作和 MCP 接入 |
| [故障排查](troubleshooting.md) | 串口占用、画面失联、绝对模式不兼容、人工暂停与失权 |
| [验证状态](development/validation.md) | 哪些已经测试，哪些仍待验证 |

## 开发与维护

| 文档 | 内容 |
|---|---|
| [架构](development/architecture.md) | 模块、线程、设备所有权与技术选择 |
| [控制状态机](development/control-state-machine.md) | 租约、接管、释放屏障、取消与恢复 |
| [接口契约](development/api-contract.md) | MCP、HTTP、WebSocket、坐标和错误 |
| [设备与视频](development/device-video.md) | 支持的 USB 身份、协议、采集与帧分发 |
| [Windows 运行机制](development/windows-operations.md) | 托盘、配置、单实例、鉴权和文件位置 |
| [测试与验收](development/testing.md) | 自动检查、硬件测试门槛和验收方法 |
| [贡献指南](development/contributing.md) | 开发流程、修改边界与提交要求 |
| [发布指南](development/releasing.md) | 敏感信息检查、源码打包和 Windows 包验证 |
| [上游来源](third-party.md) | 固定版本、复用范围与许可 |

字段以 [JSON Schema](../schemas/contracts.schema.json) 为准，时序以控制状态机为准。历史调研、逐轮审查、设备截图和原始日志不作为公开文档的一部分。
