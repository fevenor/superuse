# 上游复用记录

来源与固定提交见 `upstream-lock.json`，原作者 Suna.S 的 MIT 文本原样保留在 `licenses/`。完整上游快照仅用于本地审查，不是运行依赖，也不纳入公开分发。锁文件的 `runtime_code_imported=false` 表示未把上游包作为运行时导入；以下选择性适配仍须保留来源。

| 上游路径（serial-hid-kvm @0637902） | 项目路径 | 修改 |
|---|---|---|
| `_web_viewer.py` 的 `_VIEWER_HTML` / `_ICON_SVG` | `web/vendor/` | 提取的原始参考资源，仅加来源注释 |
| `_web_viewer.py` 的人工界面 | `web/index.html`、`web/app.js`、`web/app.css` | 拆分本地资源；增加 bootstrap/page 身份、lease/basis、有序输入、观看/接管/交还、状态事件及二进制视频帧 |
| `_JS_CODE_TO_HID` / `_JS_MOD_BITS`、`hid_keycodes.py` 的 US104 表 | `src/superuse/device/keymap.py` | 保留键码；改为纯映射，不引入全局布局修改 |
| `hid_protocol.py` | `src/superuse/device/protocol.py` | 保留格式与校验；删除可跨入口直接写串口的对象，经唯一执行器发送 |
| `capture.py` | `src/superuse/device/capture.py` | 结构参考；改稳定身份、后端独立映射、生命周期和共享帧缓存；不保留自动裁边/最后浏览器退出停采集 |
| `serial_detect.py` | 不移植 | CH340 识别不适用当前设备，改为明确 SuperKVM USB 身份 |
| `server.py` | 不移植 | 不暴露裸 TCP 或绕过仲裁的直接输入 |
| 布局、音频、OCR 等 | 不移植 | 当前范围仅 US ASCII，不引入其他运行组件 |

相对输入增量使用 0x05 有界动作与人工状态流；页面默认相对移动，首次捕获手势不转发，退出捕获/切换模式在有序流中中和而保留租约。位移按短窗口累加，状态按 revision/连接身份合并；预览采用最新帧解码和独立 1920 宽/quality 90 profile。这些是 Superuse 适配层的修改。

mcp-serial-hid-kvm @de48e74 仅作工具/图片返回参考，未直接导入旧 SDK 包装。`src/superuse/mcp/` 按本项目官方 SDK v2 契约实现鉴权、续租、取消与失权。

厂商 demo 只作协议对照；闭源 WebApp 未复制。原 HID 字节格式不变，全局 release_all 语义改为 owner-scoped 释放与执行器 fence。
