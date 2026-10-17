# Superuse 开发规则

## 开始前

1. 阅读 [架构](docs/development/architecture.md)、[控制状态机](docs/development/control-state-machine.md)、[接口契约](docs/development/api-contract.md)和相关任务。
2. 检查 Git 状态，保留其他开发者的改动。此目录是自包含仓库，父目录研究材料不是构建依赖。
3. schema、状态机和验收共同构成契约；更改时同步模型、示例、文档和有意义的测试。
4. 不因本文件自行启动或委派其他 agent。

## 设备与控制

- 只有 broker 的设备模块持有真实串口/采集句柄；MCP（stdio 与 HTTP）、浏览器（本机与局域网）、托盘走同一控制管理器，不创建第二份硬件句柄。
- 每个报告在唯一串口执行器中核对 session、lease、instance、generation 和设备状态；排队前检查不能替代执行时检查。
- 人工接管先取消旧动作、废止旧队列并完成释放屏障。旧 finally、失焦或断连清理不得释放新所有者的输入。
- 人工保留不会因 agent 下一次调用自动交还；通知不保证唤醒空闲模型。
- 不用控制端 SendInput 操作目标机，不默认打开任何摄像头/COM，不自动重放不确定的输入。
- SuperKVM 无逐包输入 ACK；串口写成功不能标为目标 UI 成功。ASCII 接口不混入 Unicode、剪贴板、文件传输或固件操作。
- 真实设备是默认模式：缺设备时程序常驻等待（托盘/页面/stdio 可用），接入后按稳定身份唯一匹配恢复。模拟只属于开发源码与测试 fixture（显式选择，frozen 发布构建拒绝并迁移旧配置）；缺设备、失权、停帧或配置错误明确失败，不回退为其他硬件/模拟成功。

## 网络访问（可信局域网）

- 远程浏览器（`lan_host:web_port`）与 Streamable HTTP MCP（`lan_host:mcp_port/mcp`）共用 `config.toml [network] key` 的单一密钥；明文 HTTP/WS，不做 HTTPS、证书、OAuth、账号或密钥强度规则。
- 密钥只作准入：远程浏览器经 key 登录换取独立 cookie 身份；每个 HTTP MCP 传输会话（`Mcp-Session-Id`）绑定独立 BrokerClient。不得以 TCP 连接、源 IP、label 或共享 key 充当逻辑身份。
- LAN 默认关闭，由托盘事务式开启（两个入口都绑定成功才算开启，失败回滚）；关闭先废止 network generation，再经控制管理器释放远程 owner，最后清理监听。旧 cookie/page token/租约不得复活。
- 本机专有入口（bootstrap、`/v1/ui/launch`、agent 会话签发）不暴露给远程；远程来源由服务端监听上下文决定，不信任客户端自报或 X-Forwarded-*。
- 日志、诊断、URL、runtime 公开字段与发布归档中不出现 key。

## 来源与隐私

上游固定在 `third_party/upstream-lock.json`；仅选择性适配代码，不直接启动上游裸 TCP/server 绕过仲裁。原 MIT 署名和许可必须保留，修改记入 `third_party/CHANGES.md`。本项目采用 MIT，不复制厂商闭源网页包。

公开内容不绑定特定目标操作系统。可说明绝对鼠标不兼容时切换相对模式；不加入私人目标信息、真实序列号、设备路径、令牌、截图或个人目录。

## 验证与交付

- 先运行 `uv run --locked python scripts/check_design.py` 和 Ruff；实现后运行与改动相称的检查。
- 默认测试与 CI 不打开真实设备。实机测试遵守 [测试规范](docs/development/testing.md)，已有明确授权时不重复征询。
- 禁止固件更新、配置复位、格式化、任意目标文件删除或与测试无关的操作。
- 日志不保存令牌、原始输入正文或默认截图。定向实机材料放本地忽略目录，公开报告只写脱敏结论。
- 交付说明区分模拟与实机、列出检查和剩余问题。未经实测不宣称通过。
- 发布前运行 [发布检查](docs/development/releasing.md)，以 Git 索引/源码归档为检查对象，不能只凭 .gitignore 推断无敏感信息。
