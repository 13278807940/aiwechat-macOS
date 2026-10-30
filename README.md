# 中犇微信助手 · Mac 独立预览版

这是新建的 macOS 项目，代码、依赖和打包入口均独立。它不导入原 Windows 的 `main.py`，不依赖 Win32、COM、UIAutomation、Windows DLL 或 PaddleOCR，也不使用 Windows 安装器和更新器。

**当前版本：2.0.3，兼容目标为 macOS 12.0 及以上；分别提供 Apple 芯片 arm64 和 Intel x86_64 包。2.0.2 已通过 macOS 云构建；本版增加旧系统截图分支、全部原生依赖最低系统版本检查及冻结应用启动自检。尚未在 macOS 12/13 和真实微信上验收，仍为预览测试版。**

## 文件与入口

```text
main.py                         Mac 应用入口；--check 可在 Windows 检查代码
mac_assistant/app.py             Qt 窗口、线上页面、宽度与窗口跟随
mac_assistant/platform_macos.py  Mac 窗口、AX 控件、窗口截图、Vision OCR、输入和发送
mac_assistant/controller.py      辅助/自动共用的回复流程、会话核对与停止
mac_assistant/name_cache.py      仅客户名称的本地文件，不保存客户详情
resources/                      线上页面的桥接和客户资料同步适配
.env                            本机配置，不提交到 Git
.env.example                    不含私密参数的配置样例
scripts/                        Mac 打包、Windows 发起构建、源码压缩命令
packaging/BUILD_MACOS.md         完整的构建、安装与权限说明
.github/workflows/              macOS 构建任务；没有自动启动
tests/                          可在 Windows 跑的逻辑与桥接测试
```

## 已写入的功能

- 加载现有线上页面：`https://yufabu.saizhuge.net/wxassistant/mobile/login`；保留线上样式。
- 使用 macOS 辅助功能读取聊天标题；必要时按微信窗口 ID 截图，交给 Apple Vision OCR。macOS 14 及以上使用 ScreenCaptureKit，12/13 使用 CoreGraphics 的单窗口截图接口。没有整个桌面的截图回退。
- 监听联系人变化，通过原有桥接通知客户资料页，保留所在页面，不把主页面重新加载回 AI 回复。
- 辅助与自动回复共用未读定位、消息读取、AI 生成和发送核对流程。默认辅助模式，回复先写草稿，通过页面发送按钮确认；自动模式只有被明确调用时才发送。
- 只有观察到消息红点时才双击消息按钮，并保留点击前的联系人信息；红点被查看消失时，也会检查当前聊天的最新客户文字。数字消息可以进入流程。
- 名称缓存写到 JSON 文件，不设置名称数量和过期时间；它只是名称名单，不能代替当前联系人身份核对。客户详情每次仍由线上页面查询。
- AI 完成后和发送前再次核对联系人、窗口进程和最新消息；自己的发出消息不作为客户问题。无法验证输入框时暂停操作。
- 本地保存分阶段耗时日志；不在日志记录完整客户消息、回复全文或接口密钥。

## 适配边界

本预览版主要适配普通一对一文字聊天。群聊排除依赖可访问的群聊标记，不能仅凭名称带 `(5)` 等数字后缀判断。不同微信版本的 AX 控件、主题、侧栏和气泡布局还需要 Mac 实机校准；仅凭 Windows 单元测试不能确认这些界面的兼容性。

窗口只截取微信，但键盘输入仍需要激活微信。无法读取输入框或已有不属于助手的草稿时会暂停。发送快捷键目前为 Return，需要微信设置一致。页面编辑助手生成的回复可以替换原助手草稿；用户在微信中另写的草稿不会被覆盖。

朋友圈发布、附件发送、知识库写入、自动更新尚未移植。线上如果调用这些本地功能，桥接会明确返回未实现。不会连接 Windows 更新器下载 EXE。请使用页面发送按钮确认辅助回复；直接在微信里手动发送后的连续任务恢复仍需实机补充验证。

## 配置、缓存和日志

Git 仓库只包含 `.env.example`。本机的 `.env` 或独立配置包可提供网页地址、智能客服地址/机器人 ID 和用户标识，不上传到公开仓库。它不包含 Windows 路径、DLL 或 Windows 更新接口。后续只修改本项目的配置。

配置优先级从低到高：包内 `.env` → `~/Library/Application Support/ZhongBenAssistant/.env` → 显式进程环境变量。可把 `.env.example` 复制到该可写目录作为安装后的配置文件。

Mac 本地文件位于：

```text
~/Library/Application Support/ZhongBenAssistant/
  .env                         可选的安装后配置
  wechat_friend_cache.json     仅名称，version + names
  logs/application.log         应用日志
  logs/customer_timing.log     识别、接口与名称保存耗时
  web-profile/                 网页登录会话
  web-cache/                   浏览器资源缓存
```

缓存和日志不写入 `.app` 包，避免安装到 Applications 后出现只读/签名问题。网页登录会话与“客户名称缓存”是两类不同文件。

## Windows 上先检查

Windows 无需安装 Mac 专属模块即可跑纯逻辑测试。使用 Python 3.11：

```powershell
Set-Location 'C:\Users\33343\Desktop\中犇微信助手-Mac独立版'
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe main.py --check
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
node .\tests\test_bridge.cjs
```

检查成功不代表 Mac 窗口识别已通过。不能在 Windows 上直接运行这份 Mac 界面或生成原生 `.app`。

## 打包

先看 [打包指令.md](打包指令.md)。Windows 可发起远程 macOS 构建；有 Mac 时可直接运行 `bash scripts/build_macos.sh`。本目录不含已经构建好的 `.dmg`。

## Mac 实机验收

1. 检查应用能启动、线上登录和客户资料页面可打开。
2. 在系统设置授权本应用的辅助功能及屏幕录制，重启后再次检查。
3. 切换普通联系人，确认客户资料跟随且不跳转主标签。
4. 用红色头像和纯数字消息测试未读定位；遮挡微信窗口测试窗口截图。
5. 辅助模式生成草稿，修改回复并点击页面发送；确认没有发给别的联系人。
6. 在 AI 生成中切换联系人、来新消息、点击停止，确认旧结果不会发送。
7. 校准当前微信版本后，再测试显式开启的自动模式。

只有上述测试通过后，才适合决定是否给客户试用。正式分发还需自己的 Developer ID 签名及公证。
