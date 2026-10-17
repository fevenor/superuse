# 控制权、取消与恢复

本文为时序规范。`schemas/contracts.schema.json` 为字段规范。所有时长使用单调时钟；UTC 时间只用于日志与显示。模式名称在 wire 上使用下表小写值。

## 1. 状态与不变量

| mode | owner | human_latched | 普通输入 |
|---|---|---|---|
| `idle` | null | false | 无；agent 可申请，人工可接管 |
| `agent` | agent session | false | 仅有效租约所属 agent |
| `human` | human session | true | 仅当前人工租约 |
| `human_hold` | null | true | 无；等待人工重连/接管或明确交还 |
| `transition` | null | 保留/设置 | 无；串口释放屏障进行中 |
| `recovery` | null | 保留 | 无；设备/视频/释放不确定 |
| `stopping` | null | 保留 | 无；应用退出中 |

设备串口、采集分别有 `disconnected/starting/ready/fault` 状态；它们不是所有权模式。健康条件为串口 ready 且有新鲜有效视频，初始化默认处于 recovery，完成中和与首帧后，按人工保留状态进入 idle 或 human_hold。

不变量：一份串口与采集句柄；一个 serial writer；一个有效输入 owner；所有报告逐次经过 generation 检查；队列、作业与输入状态均有界。正常输入、自动清理和浏览器失焦清理都服从同一规则，只有执行器自身的 fence 可以跨所有者发送释放报告。

`instance_id` 每次 broker 启动生成随机 UUID。`generation` 每次撤销控制或进入屏障单调增加，不能重用。`lease_id` 为随机 UUID，绑定一个 session 和 generation，仅在发给拥有者的响应中出现，不在广播状态或日志中出现。租约不是仅靠 generation 猜测的授权。

## 2. 转换表

| 当前状态 | 事件 | 行为与最终状态 |
|---|---|---|
| idle | agent acquire | 检查健康，fence，中和成功后授予 agent |
| agent | 同 session acquire | 返回现有租约，不新建并行控制 |
| agent | 另一 agent acquire | CONTROL_BUSY；不排队自动强抢 |
| agent/idle/human/human_hold | human takeover | 设置 latch，撤销旧授权，fence，授予发起人工 session |
| human | 同 session 重复 takeover | 有效时返回既有租约；不重复释放其按键 |
| human/human_hold | agent acquire | HUMAN_CONTROL，不自动交还 |
| agent | release/过期/session 丢失 | 撤销、fence；成功到 idle |
| human | 人工失焦/隐藏 | 取消该会话动作并 fence；成功到 human_hold，保留 latch |
| human | 人工 WS/会话失效 | 撤销、fence，到 human_hold，保留 latch |
| human | 人工 lease 到期 | 撤销、fence，到 human_hold，保留 latch |
| human/human_hold | explicit handback | 清除 latch，fence，成功到 idle；不自动授予旧 agent |
| 任意可运行状态 | 串口故障/视频停滞/几何改变 | 撤销并进入 recovery；尽力中和，不授予新 owner |
| recovery | 同一选定设备恢复 + 新鲜帧 + 中和成功 | latch 为 true 则 human_hold，否则 idle |
| 任意 | stop | 撤销、停止接收、尽力 fence、释放资源，到 stopping 并退出 |

human_hold 中任一已认证人工会话可接管或交还。human 中只有当前人工 owner 可交还；另一人工会话必须先显式接管。托盘的本地可信操作可强制接管/交还，得到一个 tray 人工 session；打开页面后仍需明确转交给该页面，通过正常 takeover 再走屏障。

## 3. Agent 租约与会话

- 默认 TTL 15000ms，允许 5000–30000ms；MCP 桥持有有效租约时每 5000ms 尝试续租。
- 续租仅延长同一有效租约，不改变 generation；已失效、过期或被接管的租约不能复活。
- 桥仅在有运行中输入作业，或最近一次接受输入距今不超过 60000ms 时续租。读取截图/状态不续租；超过窗口主动释放。这样 agent 思考或闲置不会永久占用，长作业依然可续租。
- 每个 MCP stdio 进程一个 agent session；同一客户端的 label 可以重复，不能作为身份。会话心跳由桥维持，15 秒失联过期；HTTP TCP 临时重连不会凭空创建新的 owner。
- acquire 必须由 MCP `acquire_control` 显式触发；普通输入工具不会隐式申请或抢占。工具描述告知 agent 先申请，得到失权结果后重新观察，不能无限循环申请。
- 页面心跳 5000ms，15 秒会话失活进入 human_hold；明确 WS close 可立即处理，无需等待 TTL。
- 人工 owner 是每个标签页独立的 page session，不能直接用浏览器共享 cookie 身份。仅持有当前 lease 的 input 连接断开会清理该 owner；观看 video/events 连接断开不触发全局释放。新的 input 连接不能沿用旧连接的按键状态。
- 人工页面在持有控制且可交互时也需定时 renew；session heartbeat 不等于续控制租约。失焦/隐藏走 human_hold。桥/页面实际 renew 间隔取配置值与当前 TTL/3 的较小者，不能对 5 秒 TTL 仍固定等 5 秒才续。

recovery 中的人工接管仍先设置并持久化 latch，阻止恢复后 agent 抢入；设备未健康时返回 RECOVERY_REQUIRED，保持 recovery，不能授予可用人工 lease。stopping 中拒绝新的接管。

lease 状态广播只包含 owner 描述、generation、mode、revision；会话身份与控制令牌不等于可自行指定的 request 字段。

## 4. 接管的线性化与释放屏障

不能只给 serial.write 加锁。输入执行器必须同时处理授权和发送顺序：

1. 控制管理器收到接管事件后立即标记 transition、设置人工 latch、增加 generation，停止接受旧代次动作并通知观察者“接管中”。这一步是旧授权的撤销点。
2. 优先控制通道向 SerialActor 提交 fence。它先让已开始的单次写入完成或超时，删除旧代次排队报告，取消对应长作业。
3. actor 在唯一写线程发送全零键盘报告和零按钮/零位移鼠标报告，清空设备侧的期望输入状态、调度器 held state 和原 owner 的键鼠集合。
4. 两个中和报告均完整写入后，actor 回复 fence 完成；管理器才创建新 lease、发布 human/agent，返回控制成功。
5. 超时、短写或中和失败进入 recovery。可以显示“人工已阻止 agent，设备恢复待确认”，不能显示已取得可用控制。

已被 Windows 驱动接受的字节无法撤回。屏障保证发送顺序及后续旧输入拒绝，不保证接管请求瞬间能阻止目标已经收到的最后一个按键。没有硬件 ACK，因此屏障成功表示主机端中和报告写入成功，不是目标物理状态的独立确认。

串口写超时初始设 250ms，屏障总 deadline 2000ms。不得在 fence 路径调用可能无限等待的 flush。只有一条有界进行中的写入，不预先向 OS 堆积大量输入。接管控制通道不能被普通输入队列填满。

管理器不得跨串口 I/O 持有全局锁。用转换 ticket（generation + transition ID）接收完成事件；过期 fence 完成回调不能覆盖较新的接管或 stopping 状态。串口开始写入的授权判定与 generation 更新必须通过同一 actor 顺序或短原子临界区协调，不能采用“检查后释放锁、稍后无条件写”的 TOCTOU 实现。

## 5. 长动作、取消与旧清理

动作必须拆为报告级步骤，等待使用可取消定时器。不能在控制器锁内 sleep，或把整个 type_text 作为不可中断函数放入线程池。

- 每个执行步骤检查 session、instance、lease、generation、deadline、设备健康和坐标 basis。
- 下一个报告进入 actor 时再检查一次；不能提前为长文字批量写完所有串口包。
- 新接管优先于排队输入；已完成部分保留计数，后续报告拒绝，结果标 cancelled/revoked。
- `finally` 只请求“如果我仍是同一所有者则释放”。失权时不得再调用全局 release_all；fence 已承担跨所有者清理。
- HTTP/MCP 取消尽力中止尚未执行部分，不承诺回滚已写入输入；取消后的中和同样遵循有效 owner 或 fence。
- 相对动作以有界 HID 计数分包，每包继续校验 owner/generation 和视频；点击/拖动只释放本动作按下的按钮。相对 owner 的取消/释放不使用历史绝对坐标。服务端已接收的人工相对报告不静默丢弃，溢出走 owner fence；浏览器在分配 sequence 前可压缩、淘汰过载的纯移动，按钮/键盘/滚轮状态变化必须保留。
- MCP 提交尚未返回 job_id 时取消，桥按本次提交捕获的原 lease 发起有界释放；已接受动作被撤销，尚未接受的迟到提交因旧 lease 被拒绝。此路径清空桥中的原 lease，需要重新观察与申请；迟到清理不能释放较新的租约或人工 owner，不重放输入以获取 job_id。
- 一个 owner 最多一个有状态动作作业。低频快捷键/点击保持顺序；浏览器无按钮 mousemove 可合并最新值。按下、抬起、滚轮、控制事件不能随意丢弃。
- 普通输入队列默认 128 项，溢出返回 INPUT_BACKPRESSURE。若按键释放无法入队，进入受控的 owner fence，不允许静默丢 keyup 留下粘键。

## 6. 故障与恢复

| 事件 | 必须处理 |
|---|---|
| 串口短写/超时/拔出 | 结果不确定，撤销 owner，不重发原动作；recovery |
| 视频帧龄 > 2000ms | 撤销输入，保留人工 latch；截图明确报错 |
| 分辨率/来源/采集 generation 改变 | 旧 basis 和租约失效；重新中和与观察后再申请 |
| broker 正常退出 | 停接受、撤销、fence、停采集、关连接、删属于本 instance 的 runtime 文件 |
| broker 被强杀 | 无法保证发送释放；下次连接先中和，不重放旧作业 |
| 串口无法恢复但已写下按键 | 明确提示状态未知，可能需用户重新连接目标 USB；不能假装释放成功 |

人工 latch 持久化为不含秘密的安全状态标志。先原子写入 latch 再确认人工接管；交还先写为 false 再确认。崩溃重启读取 true 时进入 human_hold；缺失状态文件的首次启动按 false 处理，损坏的已有文件按 true 保守恢复并提示。lease/session/job 不跨进程恢复。

portable 模式的 latch 位于应用目录 data/state/control.json。迁移目录后仍保留该标志，但运行凭据与 instance 每次启动重新生成；搬移不能绕过人工保留，也不会恢复旧 session/lease/job。

同一设备自动重连使用 1、2、4、8、10 秒退避上限；检测到多设备或身份变化则暂停。不能恢复成另一 COM/摄像头，也不能自动恢复 agent 的原动作。

串口 `disconnected` 与 `fault` 均触发上述撤销。新建 SerialActor 后先在新执行器上完成恢复屏障，才能回到 idle/human_hold，旧租约不能跨重连复活。采集重开使用独立于旧源 status 的等待状态；旧源关闭成功后设备暂缺仍按退避继续等待，不因五次失败永久放弃。关闭挂起或关闭失败时禁止创建第二个采集源。重开废止旧 basis，旧源迟到的帧/几何回调不得写入新 capture generation。

## 7. 必须验证的竞态

同一人工页面重新接管也会更换租约。输入连接关闭时的清理必须绑定该连接原 lease，不能仅比较 page session；迟到关闭不能暂停同页面的新租约。Esc 退出 Pointer Lock 和鼠标模式切换仅发送有序零状态，保留当前租约；真实失焦/隐藏仍进入 human_hold，必须主动“交还”才清除 latch。

接管发生在：按下后抬起前、文本字符之间、写入前授权检查后、串口写入中、旧 job finally 运行时、旧浏览器失焦时、续租请求返回前、两个接管屏障完成回调乱序时。

这些用例应使用可控时钟和可阻塞 fake transport 精确调度，不能用 sleep 碰运气代替。测试编号与验收见 `docs/development/testing.md`。
