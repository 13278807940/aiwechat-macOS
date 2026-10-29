# Mac 打包与 Windows 发起构建

这个目录是独立的 Mac 项目。不会改动原 Windows 助手。Windows 可编辑、测试纯逻辑和打源码包；`.app` / `.dmg` 必须在 macOS 上构建。本项目通过 GitHub Actions 的 macOS 机器完成这一步。

当前代码面向 macOS 14 或更新版本；`arm64` 用于 Apple 芯片，`x86_64` 用于 Intel，二者分别构建。构建成功不等于已完成微信实机兼容性验证。需要在 Mac 上验收屏幕录制、辅助功能、联系人切换、OCR 和人工确认发送。

## 路线一：你用 Windows，由 GitHub 的 Mac 构建

前提：有 GitHub 账号、一个你能写入的仓库、GitHub Actions 已启用，账号有可用的 macOS 构建额度。公开仓库和私有仓库的计费不同，触发前查看自己的 Actions 额度。此处仅提供配置，没有替你创建仓库或启动构建。

1. 把 **此文件夹的内容作为新仓库的根目录**，不要再套一层目录。新建仓库推荐设为私有。原 GitLab 仓库不能直接执行 `.github/workflows`；可以单独使用 GitHub 构建仓库，或由管理员提供自己的 macOS runner。
2. 安装 [Git](https://git-scm.com/downloads/win) 和 [GitHub CLI](https://cli.github.com/)，然后在 PowerShell 执行 `gh auth login`。
3. 在新文件夹提交代码并推送。下面的仓库地址请替换为你的实际地址：

```powershell
Set-Location 'C:\Users\33343\Desktop\中犇微信助手-Mac独立版'
git init -b main
git add .
git status --short
git commit -m "Add standalone macOS WeChat assistant"
git remote add origin https://github.com/你的账号/你的Mac助手仓库.git
git push -u origin main
```

`.env` 已在 `.gitignore` 中排除，**不要使用 `git add -f .env`**。当前目录如果有 `.env`，它只用于本地开发。检查 `git status` 后再提交。

4. 如果安装包需要内置你自己的 `.env`，在 GitHub 仓库 Settings → Secrets and variables → Actions 新建名为 `MAC_APP_ENV` 的 **Repository secret**，值填完整 `.env` 内容。工作流会写入并打包它，不会打印。注意：打入程序的密钥可以被安装包使用者提取；适合分发的配置应只含客户端公开参数，私密服务密钥应由登录后端管理。未配置 secret 时，安装包仍会包含 `.env.example` 默认配置。
5. Windows PowerShell 发起 Apple 芯片构建并等待、下载：

```powershell
$ps7 = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
& $ps7 -NoProfile -File '.\scripts\Build-MacFromWindows.ps1' `
  -Repo '你的账号/你的Mac助手仓库' -Ref main -Architecture arm64
```

有系统 PowerShell 7 时也可把 `$ps7` 改为 `pwsh`。如果脚本提示有未提交更改，先提交；如已提交但尚未推送，可加 `-Push`。脚本不会自动新增或提交代码。

Intel 版本改为 `-Architecture x86_64`，两个版本都要则用 `-Architecture both`。结果在当前项目的 **下载的安装包** 文件夹，含 `.dmg`、保留 Mac 符号链接的 `.app.zip` 和 SHA-256 校验文件。不要在 Windows 解开 `.app.zip` 后重新压缩，直接把 `.dmg` 交给 Mac 用户。

不想用脚本时，也可以进入 GitHub 仓库 Actions → Build macOS installer → Run workflow，选架构。完成后从该次运行的 Artifacts 下载结果。工作流需要先存在于默认分支。

## 路线二：有 Mac 或自己的 macOS runner

在 Mac 上安装对应架构的 Python 3.11，并在项目目录执行：

```bash
PYTHON_BIN=python3.11 bash scripts/build_macos.sh
```

Apple 芯片使用 arm64 Python，Intel 使用 x86_64 Python。不要在 Apple 芯片上用 Rosetta 的 Python 构建 arm64 包。输出在 `release/`。脚本自动创建 `.venv`、安装依赖、跑纯逻辑测试、生成 `.app`、校验签名，再用系统 `hdiutil` 创建 `.dmg`。

要只运行源码：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-macos.txt
cp .env.example .env
python main.py
```

## 签名、安装和授权

默认使用 PyInstaller 的临时 ad-hoc 签名，是用于测试的构建，**没有 Apple Developer ID 签名或公证**。因此不能承诺像已公证的商业安装包一样直接打开。正式分发应使用自己的 Developer ID 证书和 Apple 公证流程；本项目没有代管证书。不要关闭 Gatekeeper 或全局降低系统安全设置。

有签名证书的构建机可设 `MAC_CODESIGN_IDENTITY` 为 Developer ID Application 证书名称后构建。公证仍需在生成后通过自己的 Apple 账号进行，再把公证票据装订到包上；请由发行负责人完成。

Mac 用户打开 `.dmg`，将助手拖到 Applications 后再运行。第一次运行需要在系统设置的“隐私与安全性”中授权“屏幕与系统音频录制”和“辅助功能”。源码运行时授权对象可能是 Terminal/Python，安装后是助手本身；这两者的授权互不替代。具体菜单文案可能随 macOS 版本变化。

## Windows 只打源码压缩包

```powershell
python .\scripts\source_archive.py
```

输出在 **源码包** 文件夹。会包含 `.github` 工作流等隐藏代码文件，排除 `.env`、缓存、日志、虚拟环境与旧构建产物。这个 ZIP 只是用于传输代码，不能当作 Mac 安装程序。

## 构建依据

- [PyInstaller：不是交叉编译器](https://pypi.org/project/pyinstaller/6.16.0/)
- [GitHub 官方 macOS runner 架构和标签](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
- [PySide6 6.8.3 的 Python 版本与 Mac wheel](https://pypi.org/project/PySide6/6.8.3/)
- [PyObjC ScreenCaptureKit 11.1 的 universal2 wheel](https://pypi.org/project/pyobjc-framework-ScreenCaptureKit/11.1/)

本配置核对于 2026-10-28。Runner 标签和服务额度由托管平台决定，后续服务端变更时需按官方说明调整。
