# 接口契约 v1

字段规范见 [Schema](../../schemas/contracts.schema.json)，正反例见 [examples/contracts](../../examples/contracts)。本文描述当前 v1 契约；实际兼容边界见[验证状态](validation.md)。

## 1. 公共规则

- 内部 API 前缀 `/v1`，仅绑定 `127.0.0.1` 的 OS 分配端口。同一进程通过 runtime 文件发现地址。
- 另有两个对外入口（默认关闭，见第 8 节）：局域网浏览器 `lan_host:web_port` 与 Streamable HTTP MCP `lan_host:mcp_port/mcp`，共用 `[network] key` 的单一密钥；密钥只作准入，不构成身份。
- 所有 JSON 为 UTF-8。未知字段拒绝；版本为整数 `1`。request body 上限 64KiB，文本另有更小限制。
- actor/role 来自认证会话，绝不从请求 body 采信。API 不接受客户端指定 session_id。
- 错误形式为 `{"error":{"code":"CONTROL_REVOKED","message":"Control changed","retryable":false},"request_id":"..."}`。不把 traceback 或秘密返回客户端。
- 请求尚无合法 request_id（如坏 JSON/认证前失败）时，错误中的 request_id 由服务端生成，仅用于追踪，不能拿它重放原输入。
- session/lease 失效属于正常业务错误；HTTP 认证失败 401，角色/CSRF/Origin 失败 403，schema 失败 400，状态冲突 409，过期 basis 410，限流 429，后端不可用 503。
- `request_id` 为客户端生成 UUID。对 control 写操作和输入作业，服务端按 `(session, request_id)` 保存 120 秒内最多 1024 项结果与 body hash。相同请求只返回同一结果；相同 ID 不同 body 返回 REQUEST_ID_CONFLICT。满容量时拒绝新写请求，不能提前逐出仍在去重窗口内的项。高频人工状态流另用连接内 sequence 去重，不占此作业缓存。
- 浏览器兼容局域网明文 HTTP：`crypto.randomUUID` 不可用时，通过 `crypto.getRandomValues` 生成 UUID v4；接管、交还、续租和快捷键统一使用该实现，不能以启用 HTTPS 作为使用前提。
- 幂等缓存中的旧成功响应不代表租约仍有效；每次实际输入继续校验。broker 重启后 instance 改变，旧写请求拒绝，不自动重发。

## 2. MCP 工具

使用官方 SDK（锁定版本）。stdio 桥：不同 agent 启动各自的 `superuse mcp --client-label ...` / `SuperuseMCP.exe`；每个桥连接同一 broker，创建不同 agent session，配置中无需 key。

Streamable HTTP（任务 2026-10-17）：`Superuse.exe` 在 `127.0.0.1:mcp_port`（配置 key 后）或 `lan_host:mcp_port`（LAN 开启后）提供 `/mcp`。每次 HTTP 请求校验 `Authorization: Bearer <key>`（key 的 UTF-8 字节原样作为 token；非 ASCII key 按此编码规则发送）。身份是 SDK 标准 transport session（`Mcp-Session-Id`，服务端生成）：每个传输会话懒创建一个独立 `BrokerClient`（独立 broker 会话、租约、续租/空闲策略、取消与作业归属），会话终止（DELETE/空闲超时/重绑）即关闭并释放。两个同名 label 的客户端仍是不同 session；同一 TCP 连接不合并 agent，不同连接也不代表不同 agent。无 Origin 的已认证 agent 请求允许；浏览器 Origin 按实际监听地址校验。工具 schema、截图返回、资源（`superuse://state`）与错误码和 stdio 共用。

工具参数均不接受客户端 session、任意路径、raw serial 或角色。桥保存 acquire 返回的 lease，后续输入工具自动附加它；工具本身不自动 acquire。桥断开、失权或 instance 改变时清空 lease。

| 工具 | 参数 | 输出/行为 |
|---|---|---|
| `get_status` | 无 | state、设备/视频健康、来源真实/模拟、能力；不修改租约 |
| `acquire_control` | `ttl_ms=15000`，5000–30000 | 申请该 agent 的租约；人工锁定/其他 agent 占用时失败 |
| `release_control` | 无 | 释放自己的租约并中和；无租约时幂等 no-op |
| `capture_screen` | `max_width=1280`，320–1920；`after_frame_id?`；`wait_ms=2000`，0–2000 | 返回 ImageContent + 同一图片的 frame 元数据，不申请/续租 |
| `press_key` | `code`，W3C physical code；`modifiers=[]`；`hold_ms=50`，10–1000 | 按下并抬起，有界完整动作 |
| `type_text` | `text`，1–512 字符；`char_interval_ms=15`，10–50 | 全量预校验后输入 US ASCII、LF、TAB；不解释花括号标签 |
| `mouse_move` | `basis_id,x,y` | 图片像素坐标移动，保留本 owner 的按钮状态 |
| `mouse_click` | `basis_id,x,y,button=left,count=1` | count 1 或 2；不隐式变更所有者 |
| `mouse_drag` | `basis_id,from_x,from_y,to_x,to_y,button=left,duration_ms=500` | 时长 50–5000；有界插值，每步可取消 |
| `mouse_scroll` | `basis_id,x,y,steps`，非零 -127–127 | 正值向上；在指定点滚动，鼠标按钮保持当前 owner 状态 |
| `mouse_move_relative` | `dx,dy`，整数 -4096–4096 | HID 相对计数移动；不接受 basis/绝对坐标，不隐式定位 |
| `mouse_click_relative` | `button=left,count=1`（1/2） | 在当前指针位置按下/释放，不隐式移动 |
| `mouse_drag_relative` | `dx,dy`，整数 -4096–4096；`button=left,duration_ms=500`（50–5000） | 当前位置按住、相对移动并释放，有界可取消 |
| `mouse_scroll_relative` | `steps`，非零 -127–127 | 在当前指针位置滚动，正值向上 |
| `release_inputs` | 无 | 仅中和自己的输入，保持租约；失权时不能影响新 owner |
| `cancel_action` | `job_id` | 取消自己的当前作业，尽力中和；不回滚已写入输入 |

首版不向 agent 暴露任意持久 key_down/mouse_down，以降低模型停顿留下按住状态的风险。人工前端用状态报告实现持续拖动和多键；agent 的拖动/快捷键工具在后端展开。需要更复杂组合时新增有界批处理设计，不能先加任意原始包出口。

relative 工具与既有绝对工具独立提供；绝对模式不兼容时可选择相对模式。dx/dy 是 HID 计数，**不是图像像素**；目标加速度、边界截断影响实际落点。agent 应小幅移动后重新截图，不能累加计数假定绝对位置，也不能把原 mouse_move(x,y) 静默转成相对位移。无设备 ACK，仍只返回 written。

可执行输入工具等待作业结束，最大 30 秒；过程中桥继续处理续租、状态和 MCP 取消。失败返回 CallToolResult 的 `isError=true` 与机器可读错误，正常业务失败不伪装成成功文本。成功使用 `completion="written"`、报告数、已完成步数和 job_id；没有视觉确认。

可使用 SDK 的结构化结果字段；为兼容客户端，同时提供简短 TextContent JSON。截图的 ImageContent 为 image/jpeg 的 base64 数据，而不是文件路径或 URL；紧邻 TextContent 返回 `basis_id`、图片尺寸、原生尺寸、frame_id、geometry_id、captured_at 和帧龄。图片、元数据必须对应同一帧/同一缩放结果。

建议暴露只读资源 `superuse://state`，客户端支持时发送资源变更通知。不使用任意自定义 JSON-RPC 通知要求所有客户端理解；通知不保证空闲模型立即被唤醒。每个输入调用的授权检查与明确失权错误是必要行为。

## 3. HTTP 路由

| 方法/路径 | 角色 | 请求/响应 |
|---|---|---|
| GET `/healthz` | 无 | 仅 alive、api_version；不含设备/用户/端口秘密 |
| POST `/v1/sessions/agent` | agent bootstrap bearer | SessionRequest → SessionGrant，label 仅用于显示 |
| POST `/v1/sessions/browser` | 一次性 bootstrap，严格 Origin | BrowserExchangeRequest → BrowserContext，并设置 HttpOnly cookie；不允许 role |
| GET `/v1/browser/context` | browser cookie，同源读取 | BrowserContext；不创建控制 session，无 cookie 要求从托盘重新打开 |
| POST `/v1/sessions/page` | browser cookie + CSRF | SessionRequest → PageGrant，每次页面加载创建独立 human actor，token 只存页面内存 |
| POST `/v1/ui/launch` | 独立 launcher bearer | 创建短期一次性打开 URL；供第二实例 `open` 使用，agent token 不允许调用 |
| POST `/v1/session/heartbeat` | 已认证 | 延长会话活性，不隐式续控制 lease |
| DELETE `/v1/session` | 已认证 | 清理自身会话并按 owner 状态释放/hold |
| GET `/v1/state` | 已认证 | 当前 State，不包含任何 lease/token；设备未就绪时附 `serial_detail`/`video_detail`（有界脱敏原因，可选字段） |
| GET `/v1/network` | 已认证 | 无密钥的网络状态：enabled、key_configured、generation、reason（disabled/key_missing/bind_error）、实际本机地址列表与端口；永不回显 key |
| GET `/v1/status` | 已认证 | StatusResponse，包含 State 与当前可用工具名称 capabilities |
| POST `/v1/control/acquire` | agent | `{request_id,ttl_ms}`，返回私有 Lease + State |
| POST `/v1/control/renew` | lease owner | `{request_id,lease,ttl_ms}`，返回新 expires_in_ms |
| POST `/v1/control/release` | agent owner | `{request_id,lease}`，fence 完成后返回 State |
| POST `/v1/control/takeover` | human | `{request_id}`；完成屏障后返回新 Lease + State |
| POST `/v1/control/handback` | 允许交还的 human | `{request_id}`；完成屏障后返回 State |
| POST `/v1/commands` | 有效 owner | CommandEnvelope，成功 202 返回 job_id；不代表已写入 |
| GET `/v1/jobs/{job_id}` | 本 session | Job；其他 session 查询返回 404，防止泄漏 |
| POST `/v1/jobs/{job_id}/cancel` | 本 session | `{request_id}`；返回当前 Job 状态 |
| GET `/v1/frames/latest` | 已认证 | frame 元数据 + base64 JPEG；应用 schema 中 Snapshot |
| GET `/v1/events` | 已认证 WS | StateEvent，全量状态，见下一节 |
| GET `/v1/video` | human WS | 二进制帧，见下一节 |
| GET `/v1/input` | human WS | HumanInputEnvelope，结果为 InputAck；不创建持久动作作业 |

`GET /v1/frames/latest` 查询字段与 MCP capture 对齐：max_width、after_frame_id、wait_ms。after_frame_id 仅在同一 instance 内有意义；桥携带当前 instance header，instance 不同返回 INSTANCE_CHANGED。首次截图可不指定 after_frame_id。wait=0 时无新鲜图立即失败；超过等待时间返回 FRAME_TIMEOUT，不回退为旧画面成功。

内部 HTTP snapshot base64 仅供本地桥使用以简化元数据绑定；视频 WS 使用二进制，不能把连续视频全改为 base64。若后续改为二进制图片 + 元数据接口，要一起更新契约和缓存绑定测试。

控制 route 共享幂等、身份和状态机；网页输入与 MCP 不能各自实现一份控制规则。状态变化/作业/视频路径不接受全局裸 `release_all`。

人工 HTTP 路由额外要求 `X-Superuse-Page` 随机 token，服务端据此确定 page actor；cookie 仅标识浏览器身份，不能令多个标签页共享 owner。context 为只读同源接口，响应不允许跨域读取。页面 reload 通过 cookie/context/page 重新建立 actor，不从 localStorage/sessionStorage 复活旧 lease。

CSRF header 名称固定 `X-CSRF-Token`。agent 的普通 API 使用 SessionGrant.session_token 作为 Bearer，bootstrap 仅用于创建会话。各类 secret 使用至少 32 字节随机值的 base64url 编码，不等同于公开 session_id。UI launch 使用 ControlRequest，返回 `{url,expires_in_ms:30000}`，URL 的 fragment 包含一次性 bootstrap。heartbeat 接受空对象并返回 `{expires_in_ms:15000}`；DELETE session 返回 `{closed:true}`。所有参数均应按路由白名单校验。

## 4. WebSocket 帧与背压

人工 WS 先验证 cookie/Origin，再在 3 秒内接收 `{"type":"authenticate","page_token":"...","csrf":"..."}`；认证前禁止发送图像或接收普通输入。agent 事件 WS 在握手时用 Authorization Bearer 认证，不使用人工 page token。

events 完成认证后立即发送全量 StateEvent。包含 `event_id`、`instance_id`、`state_revision`、`reason`、State。后续只发最新全量状态，允许合并中间版本。断线重连重新 GET state；generation 与 revision 用于发现失权，无需依赖收到每一条通知。每 session 最多 2 条事件订阅，队列上限 16。

video 每个 binary message：前 4 字节大端 uint32 表示 UTF-8 JSON header 长度，随后 header，再随后 JPEG bytes。header 上限 4096 字节，帧总上限 16MiB。header 为 FrameMetadata，含编码尺寸、原生尺寸、basis、frame ID、geometry、时间和 capture generation。接收端根据精确长度解析，不能靠 marker 猜测。

每 viewer 只保留最新待发送帧；慢客户端丢旧图，禁止累积历史。同一 frame/profile 共用编码结果。持续发送超时 2000ms 则关闭慢连接；关闭不影响其他 viewer 或采集。未持有控制的观看连接不能因 blur/close 释放别人输入。

input 只接受 JSON text HumanInputEnvelope：api_version、sequence、lease、action。action 仅 keyboard_state、mouse_state、mouse_scroll；其他离散工具栏动作走 HTTP CommandEnvelope。服务端绑定 WS session，逐次检查有效 lease，并直接进入同一 InputScheduler/SerialActor 的有界事件流；不能直接 serial.write。

v1 增量还允许人工专用 `mouse_relative_state`：`dx,dy` 各为 -127–127 整数，`buttons` 为完整位掩码 0–7，`wheel=0` 为 -127–127 整数。每条对应一个相对报告；不接受 basis/x/y，不创建持久作业。agent 无权调用该持久状态动作。服务端不静默丢弃已接收的相对报告；队列满时明确拒绝并将人工 owner fence 到 human_hold，避免静默丢弃抬起事件。浏览器可以在分配 sequence 前采样、合并和压缩纯移动。

人工页面默认 relative，可通过模式下拉框选择路径；仅将鼠标模式偏好保存在 localStorage。relative 下接管后点击画面请求 Pointer Lock，第一次捕获手势不传给目标。Esc/退出捕获、模式切换在同一有序 input 流发送键盘与鼠标零状态，保留租约；再次点击画面即可捕获。失焦、隐藏、输入断连仍撤销输入并保留人工 latch，页面在 human_hold 同时提供“接管”和“交还”。不写固件配置，不自动抢占 agent。

相对 mousemove 在页面按 16ms 采样，发送窗口最多保留 4 条等待终态回执的报告；只有 written/coalesced/rejected 回收窗口，accepted 不代表串口写完。窗口满时合并相同按钮状态、无滚轮的连续移动；待发的一段纯移动向量最多保留每轴 254 计数（按比例压缩，最多两条 HID 报告）。饱和后的反向输入优先表达新方向；最后更新超过 50ms 的未发送纯移动会过期。正常未过载位移保留计数，过载不承诺全部计数补发，也不因纯移动饱和而断开输入或撤销人工租约。

按钮变化独立于可过期移动保存，按钮/滚轮/键盘事件保持先后顺序且不随移动过期；本地状态事件队列仍有 128 项上限，真实异常溢出仍走受控暂停。绝对位置仅合并尚未发送的同 basis/按钮状态移动事件，服务端也不得替换正在写入的事件。失权/断连清空尚未发送的队列，旧输入不跨连接重放。依据、取舍和验证见[人工鼠标流控](input-flow-control.md)。

input 连接绑定其当前租约；更换租约需新连接，旧连接关闭清理只作用于绑定租约。页面按 state_revision 合并完整状态，并按 session/generation 判断所有权；旧连接回调不能清理新连接。租约按返回 TTL 的三分之一（最长 4 秒）续租。

sequence 在每条新输入连接上从 1 递增。严格接收下一序号，重复序号只返回最多保留 64 项中的旧 InputAck，不重新发包；过旧/缺号返回 SEQUENCE_OUT_OF_ORDER。已收到但业务拒绝的事件仍消耗其序号。新连接/失权后不得自动重放旧输入，先按状态机恢复人工控制。事件流仅保留 128 项有界待执行输入和 64 项回执，不把每个 mousemove 存成 120 秒 Job。

InputAck 带 sequence、status=accepted/written/coalesced/rejected；rejected 带 Error。accepted 只表示入队，written 表示主机完整写入，coalesced 表示无按钮移动被更新位置替代；均不代表目标 ACK。按下/抬起不可合并。非法二进制/超长消息关闭 input 连接；普通状态冲突不关闭观看视频。

心跳使用 WS ping/pong + session 活性。仅保持 video 连接不授予控制。

## 5. LAN 入口（可信局域网）

`config.toml [network]`：`key`（任意非空字符串，精确比较，不 trim/大小写/归一化；空 = 未配置，等价于关闭而非免认证）、`web_port=18765`、`mcp_port=18766`（两端口必须不同）、`lan_host="0.0.0.0"`（可填单一本机 IPv4）。配置修改重启生效；LAN 开关是托盘的进程内事务，不写配置。

LAN web 监听 `lan_host:web_port`，路由为内部 API 的控制子集：`POST /lan/login {key}` → 一次性设置 `superuse_lan` cookie（HttpOnly、SameSite=Strict；明文 HTTP 不设 Secure）并返回 CSRF；`GET /lan/context`、`POST /lan/logout`；随后 `/v1/sessions/page`（entrance=lan，绑定 network generation）→ `/v1/state|status`、takeover/handback/renew、commands/jobs、frames 与三条 WS。不暴露本机管理面（bootstrap 签发、`/v1/ui/launch`、agent 会话创建、runtime 读取）。LAN cookie 与本机 `superuse_browser` 是不同名字空间，互不解释；错误 key/未登录 401，限流 429，跨源 403。

托盘开关语义：开启 = 先绑 web、再把 HTTP MCP 重绑到 `lan_host`（本机会话有短暂重连窗口），两入口都成功才显示开启；任一失败回滚并恢复本机 MCP，`reason=bind_error` 带具体端口。关闭 = 先提升 network generation（旧 cookie/page token/MCP 会话立即失效且不可复活）→ 远程 owner 经控制管理器释放屏障清理（仅 LAN 入口的会话，本机 owner 不动）→ 拆监听 → 本机 MCP 回到 127.0.0.1。重复开关经互斥锁串行化。防火墙由用户按系统提示放行，程序不建永久规则。

## 6. 坐标和画面新鲜度

MVP 关闭自动裁边，显示完整采集画面。捕获实际 native_width/native_height；给 agent 的图片可以等比缩小，不放大。每种当前采集 generation、geometry 与编码尺寸对应一个服务端注册的 `basis_id`，同时绑定 instance。

客户端传的是**所看到图片的像素坐标**。服务端按 basis 中的 image_width/image_height 转成原图坐标，再转 HID 0–4095：`floor(image_x * 4096 / image_width)`，Y 同理；数值必须有限且 `0 <= x < image_width`。有效最右/下像素结果限制为 4095。越界、NaN、旧 basis 必须报错，不静默 clamp 用户传错的点。

浏览器依据实际画面显示区域减去留白/letterbox，再映射图片像素；点击画面之外的留白不产生输入。不要用 window.devicePixelRatio 重复缩放，也不要直接传 CSS 坐标。若未来启用裁边，必须在 basis 加上完整 crop/transform 并同步更新契约，不能单方面沿用旧公式。

basis 在所属实例、采集 generation、geometry、profile 仍有效时可复用；没有引用/使用超过 60 秒可回收，LRU 上限 64。页面视频持续使用的 basis 需要保活；回收旧 basis 后返回 STALE_BASIS，客户端重新截图。frame_id 为实例内严格递增整数，不能仅凭该数字作为坐标变换依据。

视频 >1000ms 未更新时，拒绝新的普通输入并返回 VIDEO_STALE；>2000ms 撤销当前 lease 进入 recovery。在这两个阈值之间维持状态但不能继续输入。恢复必须按状态机重新中和。时间差取 monotonic，不从 UTC 时钟相减。

## 7. Command 与 Job

CommandEnvelope 字段：api_version、request_id、lease、action。action.kind 对应 schema 中的枚举：press_key/type_text/mouse_move/mouse_click/mouse_drag/mouse_scroll/keyboard_state/mouse_state/release_inputs。

增量 action.kind：mouse_move_relative / mouse_click_relative / mouse_drag_relative / mouse_scroll_relative / mouse_relative_state。新增 schema 和示例与绝对契约并存。

keyboard_state 和 mouse_state 仅 human 角色可用。键盘状态是完整集合，最多 6 个普通键加修饰键；超过 6 键明确 INPUT_LIMIT，不任意截断。code 依据复用键码表逐项验证。鼠标按钮固定 left=1、right=2、middle=4；浏览器 DOM buttons 左=1、中=4、右=2，因此显式映射，不能把 event.button 当 bitmask。

Job 状态为 accepted/running/written/cancelled/failed。terminal=true 当且仅当后三种。取消/失败返回 error code、reports_written、steps_completed 和 partial=true/false，不代表目标 UI 被回滚。type_text 先检查完整字符串、参数及预计时长，再写第一字节；CRLF/CR 规范为 LF，拒绝其他控制字符和非 ASCII，不解析 `{ctrl+c}` 一类模板。组合键用 press_key。

作业总 deadline 默认 30000ms；超过返回 ACTION_TIMEOUT，取消剩余部分并中和有效 owner。已完成结果 TTL 120 秒，作业表最多 1024；满时拒绝新作业。不自动重试任何结果不确定的输入；桥仅能以同 request_id 查询旧结果。连接失效而无法查询时向 agent 返回 RESULT_UNKNOWN。

## 8. 错误代码

| code | 含义/客户端动作 |
|---|---|
| BROKER_UNAVAILABLE / AUTH_FAILED | 启动应用或修复会话；不要控制硬件 |
| CONTROL_REQUIRED | 先 acquire；不自动申请 |
| CONTROL_BUSY | 其他 agent 持有；退让，避免忙循环 |
| HUMAN_CONTROL | 人工锁定；等待明确交还 |
| CONTROL_REVOKED / LEASE_EXPIRED | 清除本地 lease，停止旧任务，重新观察 |
| INSTANCE_CHANGED | 清除 lease、basis、job cache；新会话重新连接 |
| DEVICE_UNAVAILABLE / DEVICE_AMBIGUOUS | 重连选定设备或人工选择，禁止回退其他设备 |
| VIDEO_STALE / VIDEO_UNAVAILABLE / FRAME_TIMEOUT | 不使用旧截图假装成功 |
| STALE_BASIS / COORDINATE_OUT_OF_BOUNDS | 获取新截图并重新定位 |
| INPUT_LIMIT / UNSUPPORTED_KEY / UNSUPPORTED_TEXT | 修正输入，保证在首包前失败 |
| INPUT_BACKPRESSURE / REQUEST_ID_CONFLICT | 修复节流/请求；不得盲目重发输入 |
| SEQUENCE_OUT_OF_ORDER | 人工输入连接失序，停止输入并重新观察/恢复，不重放旧事件 |
| ACTION_CANCELLED / ACTION_TIMEOUT | 检查 partial 与已写计数，重新观察 |
| SERIAL_WRITE_FAILED / RECOVERY_REQUIRED / RESULT_UNKNOWN | 结果可能部分发生；恢复并看图确认 |

schema 只能校验结构。角色、所有权、动态坐标边界、输入键表、时间、幂等和设备健康必须额外做语义校验，并有相应测试。
