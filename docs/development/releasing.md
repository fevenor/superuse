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

推荐构建可整体解压迁移的 portable ZIP：

```powershell
uv run --locked --extra app --extra packaging python scripts/build_portable.py
uv run --locked --extra app --extra packaging pytest tests/integration/test_portable_package.py -q
```

输出 `dist/Superuse-<版本>-windows-x64-portable.zip` 及 SHA-256 文件。脚本要求 Windows x64 和 64 位 Python，每次使用新的 PyInstaller 构建目录，附空 `data/`、原始依赖许可与清单；不复制现有安装的配置或 runtime。构建目录中的符号链接和 Windows junction 会在递归进入前拒绝。空 data 目录即为 portable 模式约定，详见[便携模式](../portable.md)。

GitHub Actions 的 `portable-build` 工作流可手动触发，构建并运行 ZIP 迁移测试，随后上传 ZIP 与校验文件作为 workflow artifact；不会自动创建 GitHub Release。

仅构建常规 onedir：

```powershell
uv run --locked --extra app --extra packaging pyinstaller packaging/superuse.spec --noconfirm
uv run --locked --extra app --extra packaging pytest tests/integration/test_packaging.py -q
```

产物是 `dist/Superuse/`，包含 `Superuse.exe`、`SuperuseMCP.exe` 和共享 `_internal/`。必须整体分发。若自定义 dist 目录，通过 `SUPERUSE_PACKAGE_DIR` 指定待验收目录，避免误测旧包。

不要把开发者的缓存、配置和测试材料加入资源。构建器可能在字节码/诊断中保留构建目录；面向公开二进制发布应使用通用路径的干净构建环境，并审查归档内容。

portable 构建汇总当前锁定构建环境内各 distribution 的原始许可文件（保守包含构建/测试工具），并附 Python 运行时许可、vendor-library notices 和 JSON 清单。pySerial wheel 缺失的许可由固定版本的上游原文补齐并校验摘要。项目 MIT 不替代依赖许可，升级依赖后应复核清单与实际包；常规 spec 本身仍只附项目及直接复用上游许可。

## 3. 发布验收

在无预装 Python 的干净 Windows x64 环境验证托盘、模拟模式、浏览器本地资源、第二实例转发和 stdio MCP。随后在明确选择的真实设备上执行[硬件验收](testing.md)，记录通过项与未测项。

当前不提供安装器、自动更新、开机启动或签名承诺。创建远端仓库、上传 release、推送分支/标签与历史重写是独立的外部发布动作，应使用项目维护者明确选择的目标。
