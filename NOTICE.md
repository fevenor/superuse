# 来源与许可

Superuse 自有代码采用 MIT，见 [LICENSE](LICENSE)。部分协议、键码和人工页面适配自 Suna.S 的 MIT 项目：

- [serial-hid-kvm](https://github.com/sunasaji/serial-hid-kvm)
- [mcp-serial-hid-kvm](https://github.com/sunasaji/mcp-serial-hid-kvm)

固定提交与源码 blob 摘要保存在 `third_party/upstream-lock.json`，原始许可保存在 `third_party/licenses/`。逐项移植范围与修改见 [third_party/CHANGES.md](third_party/CHANGES.md)。这些独立许可和署名不被本项目主许可替代。

厂商公开 demo 仅用于协议对照，闭源 WebApp 未纳入资源。本项目不要求或再分发厂商服务。

Windows 二进制包还包含 Python 运行时和第三方依赖。公开分发二进制前，应按锁定的实际依赖补齐许可文本、notices 与分发检查；本仓库的两份上游 MIT 清单不是完整的二进制依赖许可清单。见[发布指南](docs/development/releasing.md)。

portable 构建会汇总 Python 和已安装构建环境 distribution 的原始许可。pySerial 3.5 的 BSD-3-Clause 原文来自[对应版本的上游许可](https://raw.githubusercontent.com/pyserial/pyserial/v3.5/LICENSE.txt)，保存在 third_party/licenses 并以 third_party/runtime-licenses.json 校验摘要。
