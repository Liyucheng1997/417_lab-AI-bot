# 我的 AI Bot

常驻在 Windows 右侧的私人 AI 助手（类似 Windows Copilot），由 **Claude Opus 5.5** 驱动，
**直接使用你的 Claude 订阅额度**（复用本机 Claude Code 的登录，不需要 API Key）。

它能像你本人一样操作这台电脑：

| 能力 | 怎么做到的 |
| --- | --- |
| 运行命令、脚本，调用任何命令行软件 | Claude Code 内置的 PowerShell / Bash 工具 |
| 读写、搜索、整理文件 | Read / Write / Edit / Glob / Grep |
| 上网搜索、读网页 | WebSearch / WebFetch |
| 看屏幕、点鼠标、敲键盘、操作任意图形软件 | 自带的 `desktop` 工具（截图、点击、输入、按键、滚动、拖动） |
| 精确找按钮和输入框 | Windows UI Automation 读取窗口控件树（`inspect_window` → `click_element`） |
| 按名字打开软件 | 自动搜索开始菜单 / 桌面快捷方式（如「微信」「Excel」「记事本」） |
| 用你在 Claude Code 里装的 MCP 工具、技能、插件 | 加载 `~/.claude` 的用户设置 |
| 记住你的偏好 | `data/memory.md` 长期记忆，Bot 会自己更新 |

## 安装

前提：Windows 10/11，已安装 [Claude Code](https://code.claude.com/docs/en/setup)，并在终端运行过 `claude` 登录订阅账号。

**方式一：安装包（推荐）**

1. 在 [Releases](../../releases) 下载 `MyAIBot-Setup-v<版本>.exe`，双击安装（不需要管理员权限，含 OCR 引擎，无需装 Python）。
   默认装到 `D:\my softwares\My AI Bot`，开始菜单里有「我的 AI Bot」，在「设置 → 应用」里可以卸载。
2. 配置 `config.yaml`、数据 `data\`、对话工作目录 `workspace\`、截图识字模块 `sop\` 都在安装目录里，更新时不会被覆盖。

**更新**：Bot 启动 10 秒后会自动检查 GitHub Releases（之后每 6 小时一次，`config.yaml` 的 `check_updates` 可关），
有新版本时面板顶部出现提示条，点「立即更新」即可：下载几十 MB 的更新包 → 静默安装 → 自动重启，不用重新安装。
也可以在面板「设置 → 检查更新」或托盘菜单里手动检查。仓库是私有的，检查更新用本机 `gh` 的登录（`gh auth login`），
或设置环境变量 `GITHUB_TOKEN`。

**方式二：从源码运行**

1. 双击 **`安装.bat`**（创建 `.venv` 并安装依赖）。截图识字模块需要：运行 `sop\build.ps1` 编译 `SOP.exe`，
   并把安装版目录（或 v1.0.0 Release 压缩包）里的 `sop\bin\engine` 文件夹拷到同样位置（OCR 引擎约 300MB，不放在仓库里）。
2. 双击 **`启动 Bot.vbs`**。

自己打包安装包（需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，`choco install innosetup`）：
`powershell -ExecutionPolicy Bypass -File build_exe.ps1`，产物 `dist\MyAIBot-Setup-v<版本>.exe`（完整安装包）
和 `dist\MyAIBot-Update-v<版本>.exe`（更新包，不含 OCR 引擎）。

发布新版本：改 `bot\__init__.py` 的 `__version__`，提交并推送，然后
`powershell -ExecutionPolicy Bypass -File release.ps1 -Notes "改了什么"`（打包并用 `gh` 创建 Release），
已安装的客户端就会提示更新。

## 使用

1. 启动后屏幕右侧出现面板，托盘里有蓝色「B」图标。
2. **`Ctrl+Alt+B`** 随时呼出 / 收起面板（可在 `config.yaml` 修改）。
3. 直接说要做什么，比如：
   - 「把桌面上所有截图移到 D:\图片\截图，按月份分文件夹」
   - 「打开微信，给文件传输助手发：明天 9 点开会」
   - 「用 Excel 打开 D:\账单.xlsx，算一下 C 列总和」
   - 「看看我屏幕上这个报错是什么意思」

面板功能：历史对话（可恢复继续聊）、新对话、置顶开关、模型切换（Opus / Sonnet / Haiku）、
权限策略、思考深度、深色模式、开机自启、编辑长期记忆。`Esc` 停止当前任务或收起面板。

终端版（调试用）：

```bash
.venv\Scripts\python -m bot.cli
```

## 截图与识字（替代 Snipaste / Umi-OCR / Reference.ahk）

Bot 启动时会在后台一起启动 `sop\bin\SOP.exe`（C# 写的小程序，约 0.2 秒就绪），热键和原来那三个软件一样：

| 热键 | 功能 |
| --- | --- |
| `F1` | 截图（自动识别窗口、标注、马赛克、复制 / 保存 / 钉图） |
| `F3` / `Shift+F3` | 把剪贴板贴到屏幕 / 隐藏或显示所有贴图 |
| `Win+Alt+C` | 截图识别文字，结果自动复制（与 Umi-OCR 同一个 PaddleOCR 引擎） |
| `Win+Alt+V` | 识别剪贴板里的图片 |
| `r1`…`r99` + 空格 | 输入 `[1]`…`[99]`（短语在 `sop\bin\hotstrings.txt`，AHK 写法） |

和 AI 打通：截图工具栏的 **AI** 按钮、贴图右键的「问 AI」、OCR 结果窗口的「发给 AI」，
都会把内容放进 Bot 的输入框，补一句问题按 Enter 即可。

菜单在托盘「B」→ **截图与识字**，以及面板设置里。自定义工作流（打开文件夹、输入固定文字、调用命令）
写在 `sop\bin\config.json` 的 `workflows` 里，说明见 `sop\README.md`。
改了 C# 代码后运行 `powershell -ExecutionPolicy Bypass -File sop\build.ps1`，再在托盘点「重启这个模块」。
（原来的 Snipaste、Umi-OCR、Reference.ahk 已于 2026-11-02 卸载，这个模块不依赖它们。）

## 安全

`config.yaml` 里的 `permission_policy`（面板左下角也能切）：

- **smart（默认）**：查看、截图、日常鼠标键盘操作直接做；删除文件、改注册表、关机重启、
  安装卸载、结束进程、修改系统目录、外部工具的写操作（发送 / 删除 / 创建…）会弹出确认卡片。
- **full**：全部自动执行。
- **strict**：除了查看，每一步都要你确认。

另外：

- 发消息、付款、对外发布等不可撤回的事，系统提示要求 Bot 先用文字征求你同意。
- 面板对截图不可见（`WDA_EXCLUDEFROMCAPTURE`），Bot 不会看到或误点自己；
  它要点的位置被面板挡住时，面板会自动收起，任务结束再出现。
- 紧急刹车：点面板的停止键，或把鼠标猛甩到屏幕**左上角**（pyautogui 失控保护）。

## 结构

```
bot/
  agent.py        引擎：ClaudeSDKClient 会话、事件流、权限回调、系统提示
  permissions.py  风险分级规则
  tools/desktop.py 桌面操作工具（进程内 MCP 服务器 desktop）
  app.py          桌面面板：pywebview 窗口、托盘、全局快捷键
  updater.py      检查 GitHub Releases、下载更新包、静默安装
  sop.py          启动 / 调用截图与识字模块（和 SOP.exe 之间用 WM_COPYDATA 通信）
  ui/             面板界面（苹果式简约设计语言）
  cli.py          终端版
config.yaml       配置
data/             日志、长期记忆（memory.md）
workspace/        Bot 的默认工作目录；对话历史按此目录保存
installer/        Inno Setup 安装包脚本（MyAIBot.iss）
sop/              截图 / 贴图 / OCR / 快捷短语模块：src（C# 源码）、bin（SOP.exe、OCR 引擎、配置）
```

## 关于订阅额度

Bot 基于 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)，它在后台启动本机的 Claude Code，
因此用的是你 `claude` 登录的账号和订阅额度（启动时会清掉 `ANTHROPIC_API_KEY`，避免误走 API 计费）。
额度接近上限时面板会提示。这是给自己电脑用的个人工具；Anthropic 不允许把订阅登录做成给他人使用的产品。
