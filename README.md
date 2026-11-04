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

1. 启动后面板从屏幕右侧滑出来打个招呼，几秒后自己收到边上，只留一条细细的把手；托盘里的图标是你的 3D 数字分身头像（和面板里的形象、exe、桌面图标统一）。
2. **鼠标碰一下屏幕右边缘**就滑出来（不抢键盘焦点），鼠标离开一会儿又收回去；输入框里有字、菜单开着、
   等你确认时不会收。标题栏的图钉可以改成「固定显示」。**`Ctrl+Alt+B`** 也能随时呼出 / 收起。
   任务在你看不见时做完，边缘的把手会呼吸几下提醒你。全屏看视频、玩游戏时碰边缘不会弹出。
3. 直接说要做什么，比如：
   - 「把桌面上所有截图移到 D:\图片\截图，按月份分文件夹」
   - 「打开微信，给文件传输助手发：明天 9 点开会」
   - 「用 Excel 打开 D:\账单.xlsx，算一下 C 列总和」
   - 「看看我屏幕上这个报错是什么意思」

欢迎页的「猜你想做」由 Haiku 根据你最近的对话、长期记忆和当前时间生成（缓存 2 小时，可点「换一批」，
`config.yaml` 的 `smart_suggestions: false` 换回固定的几条）；`nickname` 设置问候语里怎么称呼你。

欢迎页顶部是网站同款 3D 数字分身（头跟着鼠标转，Bot 工作时会东张西望）；背景鼠标划过会留下单色墨迹。
「设置 → 墨迹颜色与形象大小」里可以换墨迹颜色（默认紫色）、调浓淡（0 = 关闭）、调 3D 形象大小，
对应 `config.yaml` 的 `fluid_color` / `fluid_opacity` / `avatar_size`。

任务栏右键「关闭窗口」或托盘「退出」都会退出程序；面板上的「—」只是收到边上。

面板功能：历史对话（可恢复继续聊）、新对话、固定 / 自动收起、模型切换（Opus / Sonnet / Haiku）、
权限策略、思考深度、深色模式、开机自启、编辑长期记忆。`Esc` 停止当前任务或收起面板。

**搜索文件**（标题栏的放大镜，或 **`Ctrl+Alt+F`**）：替代 Everything，输入文件名立刻出结果。

- **只索引你自己会打开的文件**：文档（PDF、Word、Excel、PPT、WPS、Markdown、电子书……）、图片、视频、音乐、
  压缩包，以及开始菜单 / 桌面上的软件快捷方式（可以当启动器用）。系统目录、Program Files、AppData、
  装好的软件目录（含 .dll）、缓存、node_modules / .git、代码项目的 dist / build 等一律不收，结果干净。
- **按你的习惯排序**：在这里打开过的文件（次数、最近一次、当时搜的词）、Windows「最近使用」里的文件、
  你常用文件夹里的文件、最近改过的文件排在前面；同一个词搜过并打开过的，下次直接排第一。没输入时显示最近打开的。
- **实时更新**：新建、删除、改名几秒内就能搜到；索引缓存在 `dataile_index.pkl`，下次启动秒开，
  启动一分半后在后台全盘核对一次（约 20 秒，几十万个文件）。
- 语法：多个词空格分开（其余的词也可以出现在文件夹名里）；`*.pdf`、`ext:docx;xlsx` 按类型；`!备份` 排除；
  `下载\ 发票` 限定文件夹。`↑↓` 选择、`Enter` 打开、`Ctrl+Enter` 打开所在位置、`Tab` 切换类型。
  右键：复制文件（可直接粘贴到微信、文件夹）、复制路径、让 Bot 处理这个文件、属性。
- Bot 自己找文件时也会先查这个索引（`find_files` 工具），比在磁盘上一层层翻快得多。
- `config.yaml`：`search_hotkey` 换快捷键（留空不用）、`search_exclude` 排除文件夹、`search_extensions` 加扩展名。

**用量**（标题栏的柱状图按钮）：

- **Claude 订阅**：5 小时会话、本周额度的已用百分比和重置时间，和 Claude Code 的 `/usage` 一致
  （后台执行 `/usage`，不调用模型、不耗额度）。
- **API**：最近 7 天（含今天）花了多少钱、用了多少 token、缓存复用了多少，以及每天花费的柱状图，来自
  [Usage and Cost Analytics API](https://platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api)。
  需要在 Console 创建 Admin API key（`sk-ant-admin01-…`，组织账号才有），在卡片里粘贴一次即可，
  密钥只存在 Windows 凭据管理器（`MyAIBot/AnthropicAdminKey`），不写进配置文件。
  公开 API 不提供预付费余额，卡片里的「去官网充值」会打开 Console 账单页。

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
- 面板平时可以正常截图；只有 Bot 自己截屏的那一瞬间临时对截图隐身（`WDA_EXCLUDEFROMCAPTURE`），Bot 不会看到或误点自己；
  它要点的位置被面板挡住时，面板会自动收起，任务结束再出现。
- 紧急刹车：点面板的停止键，或把鼠标猛甩到屏幕**左上角**（pyautogui 失控保护）。

## 结构

```
bot/
  agent.py        引擎：ClaudeSDKClient 会话、事件流、权限回调、系统提示
  permissions.py  风险分级规则
  tools/desktop.py 桌面操作工具（进程内 MCP 服务器 desktop）
  app.py          桌面面板：pywebview 窗口、托盘、全局快捷键
  dock.py         贴边停靠：滑入滑出动画、屏幕边缘把手
  brand.py        图标：3D 数字分身头像（ui/icon.png、ui/icon-face.png），生成托盘 / exe / 窗口图标
  suggest.py      欢迎页「猜你想做」
  updater.py      检查 GitHub Releases、下载更新包、静默安装
  sop.py          启动 / 调用截图与识字模块（和 SOP.exe 之间用 WM_COPYDATA 通信）
  ui/             面板界面（同 liyucheng.me：流体墨迹背景 fluid.js、磨砂玻璃、#ff9500 点缀）
    avatar.js     欢迎页的 3D 数字分身（取自网站 avatar3d-doll.js，模型在 ui/avatar/）
    vendor/three/ three.js r160（MIT），本地加载，不依赖网络
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
