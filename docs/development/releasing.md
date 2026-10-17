# 发布指南

## 1. 公开源码

从干净检出和锁文件验证，公开目录只包含运行代码、测试、示例、文档及必要许可。内部审查、个人调研、原始日志、截图、真实设备配置和完整上游快照不属于发布内容。

`.gitignore` 排除 runtime、artifacts、captures、logs、build、dist、本机配置、环境文件、凭据与常见密钥文件；已有跟踪文件仍需单独检查。不要把工作目录整包压缩后上传。

```powershell
uv sync --locked --extra app --extra packaging --dev
uv run --locked python scripts/check_design.py
uv run --locked ruff check .
uv run --locked ruff format --check .
node --check web/app.js
node tests/web/browser-input.test.cjs
uv run --locked pytest -q
git add .
uv run --locked python scripts/check_publication.py --staged
git diff --cached --check
uv build
```

测试前正常退出已有 broker。源码包通过显式包含清单构建，并应再次检查 tar/wheel 的文件清单；Git 扫描不能替代产物检查。

发布检查脚本检查索引路径、私钥/常见服务令牌模式、疑似高熵凭据和私人 Windows 目录，仅打印文件名、行号和规则，不打印匹配值。它是启发式辅助；仍需人工检查示例、文档、提交署名和打包清单。合成测试凭据应明显可识别，真实秘密不能靠白名单豁免。

使用 GitHub noreply 地址或其他明确可公开的邮箱提交。若旧历史曾包含秘密，清理当前树并不能清除历史；应按暴露范围轮换凭据，再单独处理历史与远端副本。

## 2. 构建 Windows 包

```powershell
uv run --locked --extra app --extra packaging pyinstaller packaging/superuse.spec --noconfirm
uv run --locked --extra app --extra packaging pytest tests/integration/test_packaging.py -q
```

产物是 `dist/Superuse/`，包含 `Superuse.exe`、`SuperuseMCP.exe` 和共享 `_internal/`。必须整体分发。若自定义 dist 目录，通过 `SUPERUSE_PACKAGE_DIR` 指定待验收目录，避免误测旧包。

不要把开发者的缓存、配置和测试材料加入资源。构建器可能在字节码/诊断中保留构建目录；面向公开二进制发布应使用通用路径的干净构建环境，并审查归档内容。

二进制发布需补齐 Python、OpenCV、MCP SDK、pywin32 等实际纳入依赖的许可证和 notices；锁文件与项目 MIT 不能代替这项检查。当前 spec 带入项目 LICENSE、NOTICE 和两份直接复用上游许可，完整二进制依赖清单仍是发布前工作。

## 3. 发布验收

在无预装 Python 的干净 Windows x64 环境验证托盘、模拟模式、浏览器本地资源、第二实例转发和 stdio MCP。随后在明确选择的真实设备上执行[硬件验收](testing.md)，记录通过项与未测项。

当前不提供安装器、自动更新、开机启动或签名承诺。创建远端仓库、上传 release、推送分支/标签与历史重写是独立的外部发布动作，应使用项目维护者明确选择的目标。
