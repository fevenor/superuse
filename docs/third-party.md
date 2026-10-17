# 上游来源与复用

Superuse 按固定版本选择性适配上游代码，保留 MIT 许可和原作者署名。源码构建与运行不需要父目录调研文件、完整上游检出或厂商 WebApp。

| 来源 | 固定提交 | 用途 |
|---|---|---|
| [serial-hid-kvm](https://github.com/sunasaji/serial-hid-kvm) | `06379026341fbed19510831be051e0af360a752b` | 人工页面结构、键码、HID 协议和采集结构参考 |
| [mcp-serial-hid-kvm](https://github.com/sunasaji/mcp-serial-hid-kvm) | `de48e74cd417a64846ffe7623523cce117fb8f8e` | 工具清单、截图与客户端交互参考 |

`third_party/upstream-lock.json` 保存 commit、许可及选定源码 blob 的 Git SHA-1；它不是全仓安全审计结论。[移植记录](../third_party/CHANGES.md)说明落地路径与改动。完整上游快照不是运行依赖，不纳入本项目公开源码归档。

人工页面增加了会话、租约、basis、有序输入、观看/接管/暂停状态和最新帧分发。协议与键码接入唯一 SerialActor，不保留跨入口直接写串口的对象。设备发现改为 SuperKVM 身份匹配，采集按 Windows 后端独立映射；不保留最后一个浏览器离开就停止采集的行为。

上游 server 的裸 TCP 与直接输入路径不作为 Superuse 入口，MCP 按本仓库锁定的官方 SDK v2 实现。当前范围不引入音频、OCR、自动裁边和非必要布局。

厂商 demo 仅用作协议对照，厂商闭源网页包不属于可再分发资源。设备协议事实与本应用验收应分开：声明支持某命令不等于其在所有目标环境已验证。

原始 MIT 文本位于 [serial-hid-kvm 许可](../third_party/licenses/serial-hid-kvm-MIT.txt)和 [MCP 包装许可](../third_party/licenses/mcp-serial-hid-kvm-MIT.txt)。本项目 MIT 许可见 [LICENSE](../LICENSE)，二进制依赖的额外要求见[发布指南](development/releasing.md)。
