# 开发设计入口

完整设计集中在 [docs/development/architecture.md](docs/development/architecture.md)，避免与使用文档或旧任务计划重复。

- [控制状态机](docs/development/control-state-machine.md)：所有权、租约、释放屏障与恢复。
- [接口契约](docs/development/api-contract.md)：MCP、HTTP、WebSocket 和截图坐标。
- [设备与视频](docs/development/device-video.md)：USB 身份、协议、采集与分发。
- [Windows 运行机制](docs/development/windows-operations.md)：托盘、鉴权、配置和生命周期。
- [测试与验收](docs/development/testing.md)：自动回归与实机验证方法。

公开文档只描述当前设计和脱敏的验证边界；内部开发过程、私人设备信息和原始实机材料不纳入仓库。
