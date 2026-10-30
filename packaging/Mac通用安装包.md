# 一个安装包支持两种 Mac 芯片

通用 `.pkg` 支持 macOS 12.0 及以上。安装时自动识别 Apple 芯片或 Intel，只安装对应组件到 `/Applications/中犇微信助手.app`，用户不需要选择芯片。配置、客户名称缓存、网页登录会话和日志仍在用户目录，安装包不覆盖这些数据。安装需要管理员授权，请先退出旧版助手。

内部保留两个完整的原生应用，使用 Apple Installer 的组件选择、安装回滚及收据机制。没有合并 PyInstaller 的可执行文件；[PyInstaller 官方说明](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)指出，不能靠 `lipo` 拼接两个已构建的 PyInstaller 可执行文件来得到可靠的 universal2 程序。

## 云构建

1. 先运行原来的 `Build macOS installer`，选择 `both`，两种架构必须构建成功。记录该次运行 ID。
2. 运行 `Build combined Mac installer`，填入上一步的 `source_run` 和匹配的 `app_version`。
3. 任务会校验源运行身份、两种程序的校验值及最低系统要求，生成一个 `.pkg`。
4. 同一个 `.pkg` 随后在 Apple 芯片和 Intel 的 macOS runner 上分别安装两次，验证组件选择、安装收据、签名和安装后的真实启动。整个任务成功后，再分发包。

本次源程序运行 ID 为 `40983976547`，应用版本为 `2.0.3`。在独立 Mac 仓库根目录执行：

```powershell
gh workflow run build-combined-macos.yml --repo 13278807940/aiwechat-macOS --ref main `
  -f source_run=40983976547 -f app_version=2.0.3 -f request_id=manual
```

安装包在该次任务的 `ZhongBenAssistant-Mac-Combined-运行ID` artifact。两个 `Mac-Combined-Verification-芯片-运行ID` artifact 是安装验证报告。程序未改变时可以重新打通用安装包；程序改变时必须先重新构建两种原生应用，不要继续填写旧的 `source_run`。

当前应用只有 ad-hoc 测试签名，`.pkg` 尚未使用 Developer ID Installer 签名或 Apple 公证，仍为预览测试包。云端安装验证使用 macOS 15，不能代替 macOS 12/13 上真实微信的权限、截图、识别和回复测试。
