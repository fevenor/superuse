# 设备协议、发现与视频

## 1. 支持的硬件与定位

支持的控制端身份为 CDC `1209:0001`（SK CDC Port），视频 `345F:2131`（SK USB3 Video）。按稳定身份定位，不依赖开发机器的 COM 号或数字索引。固件/硬件版本不同仍需重新验证兼容性。

串口用 pyserial list_ports 枚举，按 VID/PID 与可选序列号定位；相机用 cv2-enumerate-cameras，保留设备路径/后端/VID/PID。用户选择保存的是稳定设备身份，打开前重新映射索引。多台或无法确定配对时要求明确选择，不假设相邻 USB 端口就是一对。

仅枚举设备不代表已打开捕获；不得为了查找采集卡逐个打开包括内置摄像头在内的所有设备。选择失败报错。MSMF 与 DirectShow 的枚举/索引空间不能混用：每种后端独立定位同一设备路径。改后端后须重新映射，不沿用数字索引。

首版优先已验证的 DirectShow 路径；显式配置可选择 MSMF，按硬件验收步骤比较。上游默认 MSMF，因此复用时必须改为配置驱动。后端更换需关闭原句柄，验证确实关闭后再打开；不静默留下第二份捕获。

## 2. HID 输入帧

`57 AB ADDR CMD LEN PAYLOAD SUM8`，ADDR=0；SUM8 是之前所有字节求和低 8 位。最大使用 payload 8 字节。不能把扩展协议校验算法用于 HID。

| CMD | PAYLOAD | 范围 |
|---|---|---|
| 0x02 | modifier,00,k1,k2,k3,k4,k5,k6 | 最多 6 个普通 HID usage；重复键先去重 |
| 0x04 | 02,buttons,xL,xH,yL,yH,wheel | X/Y 0–4095，wheel signed int8 |
| 0x05 | 01,buttons,dx,dy,wheel | 有符号相对坐标/滚轮；有界相对动作、人工状态流与零位移释放 |

0x05 用于显式相对工具和人工相对输入。每包 dx/dy/wheel 限 -127–127，大动作由调度器拆为报告级步骤，保留总计数并逐次校验授权/视频/取消；每轴最多 4096 计数，拖动最多 5s。持久按钮状态仅 human 可用，agent 只有自动抬起的有界点击/拖动。

若目标环境下绝对模式不能正常移动，切换相对模式。相对计数受目标加速和边界影响，不等于图片像素，不能承诺未校准的绝对落点。目标系统不需要安装组件。

modifier 为 HID 左/右 Ctrl、Shift、Alt、GUI 的 bitmask。press_key 接受 physical code 和修饰键枚举 left_ctrl/left_shift/left_alt/left_gui/right_ctrl/right_shift/right_alt/right_gui。Ctrl+Alt+Del 在目标是普通 USB 键组合，不通过控制端的安全桌面 API。

滚轮接口统一正值向上。浏览器 wheel.deltaY 通常正值向下，前端要反向并按 deltaMode（像素/行/页）归一为有界 steps，不能照搬上游 deltaY 到协议。验收应覆盖方向、触控板高频事件和大滚动值。

示例黄金向量应使用 `scripts/check_design.py` 校验的 `examples/protocol-vectors.json`，并在实现后测试编码器生成相同字节。

串口参数默认 2000000、8N1、read_timeout=100ms、write_timeout=250ms。输入无传统 ACK；write 返回短长度或异常属于结果不确定。避免对每包执行无限等待 flush。不要发送 ASCII hello、GET_INFO 0x01、参数修改、复位或配置写入作为生产连接探测。

0x05 的协议声明不能替代目标端验收；硬件验收必须确认零按钮零位移报告能在当前绝对模式释放鼠标，且不移动指针。若不成立，使用最后已知绝对坐标的零按钮 0x04 作为专用替代，新增硬件证据并更新 ADR；不能静默更换。

## 3. 扩展协议与接收循环

如实现只读设备信息，帧结构：`57 AB + <BHHIHI> + payload + checksumLE32`。头字段依次 channel、sn、dst、total、kind、size；固定头 17 字节（含帧头），checksum 4 字节。total=size+6。

校验为所有 checksum 之前字节的加权和低 32 位：`sum(b + (b & 7) * (i & 31) for i,b in enumerate(frame_without_checksum)) & 0xffffffff`，不是标准 CRC32。只读查询 channel=1、kind=1、size=0，响应 kind=0x81。不要把它误当旧 CH9329 GET_INFO。

接收循环处理分包、粘包、噪声、超长声明和坏校验；每次读取有界，缓冲上限 512KiB、声明 payload 上限 256KiB。错误时滑动重同步，不能按恶意长度分配大内存或无限等待。设备日志载荷不默认全量落盘，解析失败不触发输入重试。

查询必须经同一串口 actor 的受控只读路径安排，在恢复/初始化时发送即可；不高频轮询。只允许列入能力的只读消息，首版不暴露任意扩展包、OTA、磁盘方向和设置写操作。

## 4. 采集与分发

CaptureWorker 持有相机并持续读帧，FrameHub 保存最新帧：instance、capture_generation、frame_id、geometry_id、native dimensions、captured_monotonic、captured_at_utc。实际返回尺寸是坐标依据，不能仅相信请求的 width/height/FPS。

初始请求 1920×1080、30fps；实际能力以读到的图像和间隔为准。第一帧等待预算 10 秒，实际首帧耗时与帧率应单独测量，30fps 仅为请求值；现有一次短时采样约 10fps，见[验证状态](validation.md)。

同一 capture_generation 只能有一个读取源。若视频已经由人工页面打开，MCP 从 FrameHub 取图，不重新打开设备。最后一个浏览器离开时，只要 broker 仍运行便维持既有采集策略；不能原样保留上游“最后 Web client 断开就停止 capture thread”逻辑而破坏 MCP。

默认常驻采集，人工默认发布 10fps、JPEG quality 90、最大宽 1920；1080p 源保持原尺寸，较小源不放大。MCP 默认等比缩小至宽 1280、JPEG quality 80（配置可调）。最多缓存 3 个编码 profile，按 `(frame_id,width,quality)` 去重，连续视频订阅共享。人工预览不再复用 MCP 的缩小参数；浏览器同时只解码一帧、保留最新待解码帧。MCP 任意 quality/profile 不开放，避免缓存无界增长。

没有客户端时不进行无用编码；采集仍用于保持新鲜状态。视频发送采用 latest-only；图片编码使用原生 OpenCV/Pillow，避免 Python 像素循环。采集/编码不阻塞控制和事件循环。

Windows OpenCV 路径通常返回已解码图像，上游实现也未在 MSMF/DirectShow 启用 MJPEG 直通。不要把 Linux 可用的 raw MJPEG passthrough 当作 Windows 现成能力。若后续需要直通，须另选 Windows 采集 adapter 并测量，保留统一 FrameHub 输出契约。

## 5. 几何与恢复

MVP full-frame、不自动裁边。收到尺寸变化或设备重开时增加 capture_generation/geometry、废止 basis，并撤销当前租约、中和后回到 idle/human_hold，等待重新观察与申请。不能在拖动中静默重用旧坐标比例。

serial ready 但 video unavailable 时，允许诊断和释放，不允许普通键鼠。采集故障不能伪装成静态旧图继续显示“已连接”。页面覆盖明确“视频失联/画面过期”。恢复时选择原设备身份；未关闭的卡死句柄不能通过无限新建线程重试。

相对工具服从同一新鲜度和失权规则；相对 owner 的 release_inputs/取消使用零位移 0x05，避免在历史绝对坐标释放而跳转。串口未打开/写线程不存在时 fence 直接失败；线程中的回执等待也有超时，失败启动不能留下永久 Event.wait。

## 6. 模拟与适配边界

SimulatedSerial 记录报告和发送顺序，支持短写、超时、断开、阻塞与释放失败注入；SimulatedCapture 生成无敏感内容的时间戳测试图，支持尺寸变化和停滞。UI/状态必须显著标 simulated。

无设备开发不能用空白 JPEG 冒充真实截图；真实模式缺失设备时不自动退回模拟成功。模拟器测试可证明逻辑顺序，不证明硬件 HID 行为或真实帧率。

上游协议/采集只作为实现基座；需调整设备识别、Windows 后端选择、生命周期、并发与状态接口。禁止从 Web/MCP 模块直接 import 可写串口对象。
