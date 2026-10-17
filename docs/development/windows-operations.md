# Windows 运行、鉴权与交付

## 1. CLI 与托盘生命周期

| 命令 | 行为 |
|---|---|
| `superuse serve [--config PATH] [--real] [--headless] [--runtime-dir DIR]` | 启动单一 broker；默认模拟，普通模式进入托盘 |
| `superuse open [--runtime-dir DIR]` | 为已有服务创建一次性打开链接，调起默认浏览器 |
| `superuse status [--runtime-dir DIR]` | 输出脱敏的服务发现信息，不包含 runtime 凭据 |
| `superuse diagnose [--config PATH] [--output DIR]` | 只读枚举与配置摘要；不打开硬件或发送输入 |
| `superuse mcp [--client-label LABEL] [--runtime-dir DIR]` | stdio MCP 桥；不自行启动 broker |

`Superuse.exe` 为 windowed 托盘入口，`SuperuseMCP.exe` 保留 stdio。两者共享 onedir 依赖。普通启动不自动打开网页或发送业务键鼠动作；真实连接会先中和输入，再进入 idle 或持久化 human_hold。

托盘菜单提供打开界面、状态、接管并打开、交还、复制脱敏诊断与退出。事件提交到 broker 循环，不直接操作设备。托盘接管产生独立 human session；页面经正常 takeover 接手，租约不放入 URL。

正常退出先拒绝新输入、撤销旧租约并走释放屏障，再停采集、关闭 HTTP/WS/串口、移除本 instance 的 runtime 文件、释放互斥量。清理失败必须保留不确定性提示，不能宣称目标物理状态已确认。

## 2. 单实例与文件位置

命名互斥量 `Local\Superuse.<当前用户 SID>.broker` 在同一交互登录会话防止重复 broker。它不是跨用户授权；其他登录会话争用同一设备仍可能失败。

重复启动向健康主实例的 launcher 请求打开页面后退出。服务发现不只凭 PID，而检查 runtime 身份与 health；不得杀同名进程或清理其他实例文件。

| 默认位置 | 内容 |
|---|---|
| `%LOCALAPPDATA%\Superuse\runtime\broker.json` | 端口、instance、PID、agent bootstrap 和 launcher 凭据 |
| `%LOCALAPPDATA%\Superuse\state\control.json` | 人工保留标志，不保存 lease |
| `%LOCALAPPDATA%\Superuse\logs\` | 轮转日志 |
| `--config` 指定文件 | 本机配置；不自动从任意目录加载 |
| `diagnose --output` 指定目录 | 用户显式导出的诊断 |

自定义 runtime 时，state/logs 使用该目录的父目录。文件原子写入，Windows ACL 限制当前用户与必要系统管理员访问；移除 runtime 前核对 instance，避免删除新服务文件。

## 3. 本地鉴权

服务只监听 `127.0.0.1`，校验 Host/Origin，不开放任意 CORS。回环监听本身不能替代鉴权；同一用户下可以读取凭据或注入进程的恶意软件不在令牌防护范围内。

- agent bootstrap 与 launcher 凭据独立，随机值至少 256bit。agent 凭据不能创建 human 或调用人工 takeover。
- 打开界面的 bootstrap 通过 URL fragment 传递，30 秒有效且一次使用。兑换后页面移除 fragment，浏览器身份使用 HttpOnly/SameSite cookie 与 CSRF。
- 每个标签页创建独立 page session，page token 只保留内存。HTTP 带 page token/CSRF，人工 WS 在 3 秒内用首条 authenticate 绑定身份；认证前不发图或接受业务输入。
- 刷新不会继承旧 lease。旧连接清理只作用于绑定租约，不影响同页新租约或其他页 owner。
- MCP 从当前用户 runtime 读取 bootstrap，配置示例不嵌入令牌；stdout 只传 MCP。
- 静态资源全部本地加载，使用 CSP；队列、请求与连接数有上限，接管使用独立优先控制通道。

## 4. 配置

优先级为内置默认 < `--config` 文件 < 显式 CLI 覆盖（如 `--real`）。字段见 [Schema](../../schemas/contracts.schema.json) 的 Config 和[配置示例](../../config.example.toml)。文件可只覆盖部分字段，按表合并并严格校验未知字段、类型和范围。

`renew_interval_ms` 不超过配置 TTL 的一半，`freshness_ms` 小于 `stall_timeout_ms`；实际续租还按当前 TTL/3 限制。真实模式按 VID/PID 与可选稳定身份筛选，多匹配时必须进一步指定身份。COM/视频索引只用于本次映射，不作为稳定配置。

不支持热更新；更换设备或采集参数需重启并中和，不能在正在运行的动作中静默替换。

## 5. 日志与诊断

日志使用 UTC，记录状态原因、generation、动作类别和结果，不保存令牌、完整输入正文或原始设备日志载荷。默认不截图。诊断导出虽然缩短设备标识，仍需人工检查再公开。

设备枚举、原始诊断、真实配置、运行凭据、截图和录屏不纳入公开仓库。实机证据只用于本地定向验证，公开文档保留脱敏摘要。

## 6. 打包与发布

构建方式见[发布指南](releasing.md)。项目采用 MIT，上游独立许可保留；二进制依赖许可和干净环境测试应按实际包补齐。

当前不注册 Windows Service，不默认开机启动或自动修改防火墙、驱动、浏览器策略及 agent 配置。控制端目标为 Windows x64，未测试的系统版本和浏览器不能直接列为已兼容。
