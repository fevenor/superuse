# Superuse MCP 接入与操作说明

这份文档供用户和 agent 直接读取。它随便携 ZIP 发布，不依赖源码仓库或本机 Python 环境。先根据下表选择接入方式，再配置 MCP 宿主。

## 1. 选择入口

| 场景 | 传输 | MCP 宿主配置 | 是否需要 key |
|---|---|---|---|
| agent 与设备控制软件在同一台 Windows PC | stdio | 启动本目录的 `SuperuseMCP.exe` | 不需要 |
| 同一台 PC，通过 URL 接入 | Streamable HTTP | `http://127.0.0.1:18766/mcp` | 需要 |
| 其他 PC 上的 agent | Streamable HTTP | `http://<控制端局域网IP>:18766/mcp` | 需要，并在托盘开启 LAN |

控制端是连接 SuperKVM 并运行 `Superuse.exe` 的 Windows 主机。被控端不需要安装 Superuse 或联网。远程 agent 只需支持 Streamable HTTP 和自定义 Authorization header；不需要在其主机上运行这两个 Windows EXE。

`18765` 是远程人工浏览器端口，`18766/mcp` 才是 MCP 端点，不能互换。端口可在配置中修改；`0.0.0.0` 是监听设置，不能用作远程连接地址。这里不是旧版 HTTP+SSE `/sse` 入口。

## 2. 启动控制端

1. 将整个 ZIP 解压到可写目录，保留 `Superuse.exe`、`SuperuseMCP.exe`、`_internal/` 和 `data/`。无需安装 Python、uv 或源码依赖。
2. 双击 `Superuse.exe`，程序进入托盘；右键“打开界面”可看设备状态。
3. 首次启动自动创建 `data/config.toml`，默认使用真实设备。设备缺失时程序继续运行并提示连接，不能据此认定服务启动失败。连接预期设备后等待恢复，或选择托盘“重新检测设备”。多个候选需在配置中填写稳定设备身份，字段见 `config.example.toml`。
4. 主程序运行后，再由 MCP 宿主启动 stdio 桥或连接 HTTP。MCP 桥不会自动启动主程序。

同一登录会话只运行一个主程序，多个 agent 各有独立会话，但共用唯一设备。不要启动其他软件抢占同一个串口或视频设备。配置和地址变化后更新宿主配置并重新连接。

## 3. 本机 stdio 配置

打开同目录 `mcp-client.example.json`，替换安装目录后，将其内容按宿主的配置格式导入。通用示例：

```json
{
  "mcpServers": {
    "superuse": {
      "command": "C:\\REPLACE_WITH_INSTALL_DIRECTORY\\SuperuseMCP.exe",
      "args": ["--client-label", "agent-a"]
    }
  }
}
```

`command` 是实际 EXE 的绝对路径；不要在 JSON 字符串内部额外加一层命令行引号，也不要替换成 `Superuse.exe`。路径里的反斜杠按 JSON 写成 `\\`。label 仅用于显示，不是凭据；多个 agent 即使 label 相同，也不会共享租约。

主程序与桥位于同一解压目录时，桥自动找到 `data/runtime/`，不依赖 MCP 宿主的工作目录，不需要设置 cwd、复制 runtime 文件或填写内部 token。只有主程序显式使用自定义 `--runtime-dir` 时，桥也应传入同一个目录，例如追加 `"--runtime-dir", "C:\\REPLACE_WITH_RUNTIME_DIRECTORY"`。

## 4. HTTP 与局域网配置

从托盘正常退出主程序后，编辑 `data/config.toml`，加入或修改以下表；若已存在 `[network]`，编辑原表，不要重复添加：

```toml
[network]
key = "REPLACE_WITH_NETWORK_KEY"
web_port = 18765
mcp_port = 18766
lan_host = "0.0.0.0"
```

将示例 key 换成用户选定的非空值。没有长度、复杂度要求，`"a"` 或 `"123"` 均可；空 key 表示 HTTP MCP/LAN 尚未配置。比较按原值进行，不会去掉空格或忽略大小写。通用宿主示例使用 ASCII key；非 ASCII key 要求宿主能原样发送 UTF-8 字节的 Authorization header。

重新启动 `Superuse.exe` 后：

- 本机 HTTP MCP：`http://127.0.0.1:18766/mcp`，无须开启 LAN。
- 远程连接：托盘勾选“允许局域网访问”，再选择“复制局域网地址”，使用其中实际 IP 的 MCP 地址。每次重启 LAN 默认关闭。
- 远程人工页面：`http://<控制端局域网IP>:18765`，页面输入相同的 key；agent 通过 MCP 请求头提供 key，不经过网页登录。
- 这两个端口使用 HTTP/WS，无须配置 HTTPS、证书或 OAuth。Windows 防火墙若阻止访问，在所用网络上放行控制端 TCP 端口；程序不会自动创建永久规则。

同目录 `mcp-client-http.example.json` 给出通用宿主配置。将 URL 和 key 换成实际值；宿主字段名称可能不同，应使用其 Streamable HTTP 入口和请求头设置：

```json
{
  "mcpServers": {
    "superuse-http": {
      "type": "http",
      "url": "http://127.0.0.1:18766/mcp",
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_NETWORK_KEY"
      }
    }
  }
}
```

远程 agent 将 URL 的 `127.0.0.1` 改成控制端实际 IP。每次 MCP HTTP 请求都必须带 Authorization header；让 MCP 客户端完成 initialize、协议协商和 `Mcp-Session-Id` 会话管理，不要自行指定或复用其他 agent 的会话 ID。关闭/开启 LAN 会使 HTTP MCP 断开并重新建立会话，旧租约与动作不能恢复。

## 5. agent 首次连接与工作流程

先调用 `tools/list`，以服务实际返回的 schema 为准。连接验证只需调用 `get_status`，不会发送键鼠输入；不要为“测试连接”调用输入工具。

正式执行用户授权的被控端任务时，按以下顺序：

1. `get_status()`：确认 `state.source` 为 `real`，串口和视频均为 `ready`；检查是否有人工锁定或其他所有者。
2. `capture_screen()`：观察当前画面并读取紧邻图片的 metadata。没有新鲜画面时停止输入，等待设备恢复。
3. `acquire_control()`：显式申请本 agent 的独占租约。普通输入工具不会自动申请或抢占；人工持有时不要忙循环重试。
4. 用有界的键鼠工具完成一小步，再次 `capture_screen()` 核实结果。短任务完成或需要长时间等待时调用 `release_control()`；异常退出也尽力释放自己的控制权。
5. 人工接管、断网、服务重启或租约失效后，停止旧任务，重新连接、观察并申请；不重放结果未知的输入。

工具参考：

| 工具 | 用途与关键参数 |
|---|---|
| `get_status` | 状态、真实来源、设备健康、所有者和能力；无需租约 |
| `capture_screen` | 新鲜截图及同帧 metadata；`max_width` 默认 1280，可选 320–1920；不会续租 |
| `acquire_control` / `release_control` | 显式申请/释放本 agent 的租约 |
| `press_key` | 物理键码，如 `KeyA`、`Enter`；组合键使用 `modifiers`，准确枚举以 tools/list 为准 |
| `type_text` | US ASCII、换行和 TAB，1–512 字符；不支持任意 Unicode、粘贴或花括号快捷键模板 |
| `mouse_move` / `mouse_click` / `mouse_drag` / `mouse_scroll` | 绝对模式：传当前截图 metadata 的 `basis_id` 和该截图的图片像素坐标 |
| `mouse_move_relative` / `mouse_drag_relative` | `dx`、`dy` 是 HID 相对计数，不是图片像素；小幅移动后看图确认 |
| `mouse_click_relative` / `mouse_scroll_relative` | 在目标当前指针位置点击/滚动；不会隐式移动 |
| `release_inputs` | 中和本 agent 的输入状态，仍保留租约 |
| `cancel_action` | 用 `job_id` 取消自己的作业；不回滚已发生的输入 |

绝对坐标必须依据 agent 实际收到的缩放图片，不能直接使用目标原生分辨率、浏览器 CSS 像素或其他会话的旧 basis。目标不兼容绝对鼠标时使用 `*_relative` 工具；不能累计相对计数假定当前绝对位置。

`completion="written"` 只表示控制端完整写入了报告，不代表目标 UI 已执行成功。成功与否要通过新截图核实。可选只读资源 `superuse://state` 用于查看状态；它不会自动申请控制，也不保证能唤醒空闲 agent。

## 6. 常见故障

| 现象/错误 | 处理 |
|---|---|
| `BROKER_UNAVAILABLE` | 启动 `Superuse.exe`；确认两个 EXE 在同一解压目录，自定义 runtime 参数一致 |
| HTTP 401 | 检查 key 和 `Authorization: Bearer ...`；不得把空 key 当免认证 |
| 远程连接拒绝/超时 | 确认托盘 LAN 开启、IP 正确、端口未占用、防火墙允许；远端的 127.0.0.1 指向远端自身 |
| HTTP 404/405 或收到 HTML | 检查是否误用浏览器端口、遗漏 `/mcp` 或选择了旧 SSE 传输 |
| `DEVICE_UNAVAILABLE` / `RECOVERY_REQUIRED` / 视频不可用 | 连接预期设备、解决占用/匹配问题，等待状态恢复；不能通过改为模拟来恢复发布程序 |
| `CONTROL_REQUIRED` | 观察状态后显式调用 `acquire_control` |
| `CONTROL_BUSY` | 其他 agent 持有控制；等待其释放，不强抢 |
| `HUMAN_CONTROL` | 人工控制或暂停；等待用户在页面/托盘明确“交还给 agent” |
| `CONTROL_REVOKED` / `LEASE_EXPIRED` / `INSTANCE_CHANGED` | 清除旧任务状态，重新连接、截图和申请；不能复活旧租约 |
| `STALE_BASIS` / `VIDEO_STALE` | 获取新鲜截图后重新定位；不要重发旧坐标 |
| `RESULT_UNKNOWN` / 写入失败 | 动作可能部分发生，先看图确认，不能盲目重试 |

运行日志位于 `data/logs/`，配置和设备标识保存在本机。迁移/升级前正常退出，保留自己的 `data/`；旧 runtime 凭据、session、lease 和 job 不跨重启使用。目录迁移与升级规则另见 `PORTABLE.md`。
