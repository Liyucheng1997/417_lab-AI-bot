# 我的 SOP（MySOP）

把开机要启动的 **Umi-OCR + Snipaste + Reference.ahk** 合成一个托盘小程序：`dist\SOP.exe`（约 115 KB，启动约 0.2 秒，空闲内存约 40 MB）。
OCR 引擎只在第一次识别时才启动（约 1 秒），空闲 10 分钟后自动退出，所以开机不会被拖慢。

## 原来的用法照旧

| 功能 | 热键 | 原来是谁 |
|---|---|---|
| 截图 | `F1` | Snipaste |
| 录屏（框选区域，可选录电脑声音，再按一次停止） | `Ctrl+F1` | 新增 |
| 贴图（把剪贴板钉在屏幕上） | `F3` | Snipaste |
| 隐藏/显示所有贴图 | `Shift+F3` | Snipaste |
| 截图识别文字（结果自动复制） | `Win+Alt+C` | Umi-OCR |
| 识别剪贴板里的图片 | `Win+Alt+V` | Umi-OCR |
| `r1`~`r99` → `[1]`~`[99]`，`rr` → `1. 思考快与慢。` | 输入缩写 + 空格/回车/标点 | Reference.ahk |

OCR 用的是和 Umi-OCR 一模一样的 PaddleOCR-json 引擎（已复制到 `dist\engine`），排版方式也沿用你原来的“多栏-无换行”。

**截图时：** 鼠标悬停会自动识别窗口，单击选中窗口，拖拽框选；
`Enter` / `Ctrl+C` / 双击 = 复制，`Ctrl+S` = 保存，`Ctrl+T` / `F3` = 钉在屏幕，`Ctrl+Z` = 撤销，
方向键微调，`Ctrl+A` 全屏，`C` 复制光标处颜色，右键 = 重选，`Esc` = 退出。
工具栏有矩形、椭圆、箭头、画笔、马赛克、文字，以及 **“文”= 直接 OCR 这块区域**。

**贴图窗口：** 拖动移动，滚轮缩放，`Ctrl+滚轮` 调透明度，双击 / `Esc` 关闭，右键菜单里也可以 OCR。

## 第一次使用（迁移）

1. 双击 `dist\SOP.exe`，右下角托盘出现蓝色 **S** 图标。
2. 托盘右键 → 设置 → **接管旧软件（Snipaste / Umi-OCR / AHK）…**
   它会关闭这三个程序、把它们的开机启动快捷方式移到 `dist\backup\startup`、并把本程序设为开机自启。
   软件本身不会被删除；想回退就点 **恢复旧软件的开机启动**。
3. Snipaste 是以管理员身份运行的，可能关不掉，这时在 Snipaste 托盘图标上右键退出，再点本程序的 **重新加载配置**。

## 以后加新的工作流

**不写代码：** 编辑 `dist\config.json` 的 `workflows`，加一项，保存后托盘右键 → 重新加载：

```json
{ "name": "打开论文文件夹", "hotkey": "ctrl+alt+p", "type": "open", "target": "F:\\论文" },
{ "name": "签名",           "hotkey": "ctrl+alt+s", "type": "type", "text": "Yucheng Li\n{date}" },
{ "name": "截图并识字",     "hotkey": "",           "type": "command", "target": "ocr" }
```

- `open`：打开文件/文件夹/网址/程序（可加 `args`、`workingDir`）
- `type`：直接输入文字；`paste`：用剪贴板粘贴长文字；文字里可用 `{date}` `{time}` `{datetime}` `{clipboard}`
- `command`：调用内置命令，多个用逗号隔开。命令列表在托盘 → **热键一览**

短语直接写在 `dist\hotstrings.txt`（AHK 写法 `::缩写::内容`，支持 `:*:` `:?:` `` `n ``），保存即生效。

其它程序或脚本也能调用：`SOP.exe capture`、`SOP.exe ocr`、`SOP.exe pin`……

**写代码加模块：** 在 `src\Modules` 新建一个类实现 `IModule`，在 `Program.BuiltinModules()` 里加一行，然后运行 `build.ps1`。
模块在 `Init` 里用 `ctx.Hotkey(...)` 注册热键（会自动写进 config.json），用 `ctx.Command(...)` 注册命令，用 `ctx.Menu(...)` 加托盘菜单。
（也可以编译成 DLL 放进 `dist\plugins`，启动时自动加载。）

## 编译

只需要 Windows 自带的 .NET Framework 4.8：

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

自检（快捷短语、排版、OCR 引擎）：`dist\SOP.exe --selftest`，结果写在 `dist\logs_selftest.txt`。

## 目录

```
src/Core      框架：配置、热键、命令、托盘、通知、迁移
src/Capture   截图遮罩、贴图窗口
src/Ocr       PaddleOCR-json 引擎封装、排版
src/Modules   各功能模块（截图贴图 / 文字识别 / 快捷短语 / 自定义工作流）
dist/         可直接运行的程序 + config.json + hotstrings.txt + engine
```

## 已知限制

- 本程序以普通权限运行：在“以管理员身份运行”的窗口里，快捷短语不会生效（原来的 AHK 也是普通权限，所以行为一样）。
- Mathpix（公式识别）没有合并：它依赖云端服务，仍保持原样。
