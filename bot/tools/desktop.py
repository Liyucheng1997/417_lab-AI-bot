"""操作电脑桌面的工具：截图、鼠标键盘、窗口、控件树、剪贴板、打开软件。

坐标约定：所有鼠标坐标都用「最近一次 screenshot 图片里的像素坐标」，
这里负责换算成真实屏幕坐标，模型不需要关心缩放和多显示器偏移。
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any, Callable

from claude_agent_sdk import tool

# 让本进程按物理像素工作，截图坐标和鼠标坐标才能对上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import mss
import psutil
import pyautogui
import pyperclip
from PIL import Image

pyautogui.FAILSAFE = True  # 鼠标甩到屏幕左上角可紧急中止
pyautogui.PAUSE = 0.05

MAX_SHOT_EDGE = 1600


class _ShotState:
    left = 0
    top = 0
    scale = 1.0  # 截图像素 * scale = 屏幕像素


shot = _ShotState()

# 面板 App 注册的钩子：鼠标要点到面板所在区域时先把面板藏起来
before_pointer: Callable[[int, int], None] | None = None
# 面板 App 注册的钩子：Bot 自己截图时临时让面板对截图不可见（True=开始截图，False=截完）
capture_guard: Callable[[bool], None] | None = None

# inspect_window 最近一次列出的控件
_elements: list[dict] = []


def _to_screen(x: float, y: float) -> tuple[int, int]:
    return int(shot.left + x * shot.scale), int(shot.top + y * shot.scale)


def _to_shot(x: float, y: float) -> tuple[int, int]:
    return int((x - shot.left) / shot.scale), int((y - shot.top) / shot.scale)


def _text(s: str, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


def _pointer(x: int, y: int) -> None:
    if before_pointer:
        try:
            before_pointer(x, y)
        except Exception:
            pass


# ---------------------------------------------------------------- 截图

def _guard(on: bool) -> None:
    if capture_guard:
        try:
            capture_guard(on)
        except Exception:
            pass


def _grab(monitor: int) -> tuple[str, str]:
    with mss.mss() as sct:
        mons = sct.monitors  # [0] 是所有屏幕拼起来的虚拟屏
        mon = mons[monitor] if 0 <= monitor < len(mons) else mons[1]
        _guard(True)
        try:
            raw = sct.grab(mon)
        finally:
            _guard(False)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = max(img.width, img.height) / MAX_SHOT_EDGE
    scale = max(scale, 1.0)
    if scale > 1.0:
        img = img.resize((round(img.width / scale), round(img.height / scale)), Image.LANCZOS)
    shot.left, shot.top, shot.scale = mon["left"], mon["top"], scale
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    info = (f"截图尺寸 {img.width}x{img.height}（显示器 {monitor}，屏幕实际 {mon['width']}x{mon['height']}）。"
            f"之后的鼠标坐标请直接用这张图里的像素坐标。")
    return base64.b64encode(buf.getvalue()).decode(), info


@tool("screenshot", "截取屏幕画面，看到用户电脑当前的样子。操作鼠标前、操作后验证结果时都应该截图。"
      "monitor: 1=主显示器(默认)，2/3=其他显示器，0=所有显示器拼在一起。",
      {"monitor": Annotated[int, "显示器编号，默认 1"]})
async def screenshot(args):
    data, info = await asyncio.to_thread(_grab, int(args.get("monitor", 1) or 1))
    return {"content": [{"type": "image", "data": data, "mimeType": "image/jpeg"},
                        {"type": "text", "text": info}]}


@tool("screen_info", "列出显示器数量、分辨率和当前鼠标位置。", {})
async def screen_info(args):
    with mss.mss() as sct:
        lines = [f"显示器 {i}: {m['width']}x{m['height']} 位置({m['left']},{m['top']})"
                 for i, m in enumerate(sct.monitors) if i > 0]
    x, y = pyautogui.position()
    lines.append(f"鼠标屏幕坐标 ({x},{y})，换算到最近截图坐标为 {_to_shot(x, y)}")
    return _text("\n".join(lines))


@tool("cursor_position", "获取鼠标当前位置（截图坐标）。", {})
async def cursor_position(args):
    x, y = pyautogui.position()
    return _text(f"{_to_shot(x, y)}")


# ---------------------------------------------------------------- 鼠标

@tool("click", "在截图坐标 (x, y) 处点击鼠标。button: left/right/middle；clicks: 1 单击, 2 双击。",
      {"x": int, "y": int, "button": Annotated[str, "left/right/middle，默认 left"],
       "clicks": Annotated[int, "点击次数，默认 1"]})
async def click(args):
    x, y = _to_screen(args["x"], args["y"])
    _pointer(x, y)
    await asyncio.to_thread(pyautogui.click, x, y, clicks=int(args.get("clicks") or 1),
                            interval=0.08, button=args.get("button") or "left")
    return _text(f"已在 ({args['x']},{args['y']}) 点击")


@tool("move_mouse", "把鼠标移动到截图坐标 (x, y)，用于悬停显示提示等。", {"x": int, "y": int})
async def move_mouse(args):
    x, y = _to_screen(args["x"], args["y"])
    _pointer(x, y)
    await asyncio.to_thread(pyautogui.moveTo, x, y, duration=0.15)
    return _text("已移动")


@tool("drag", "按住左键从 (x1,y1) 拖到 (x2,y2)（截图坐标）。",
      {"x1": int, "y1": int, "x2": int, "y2": int})
async def drag(args):
    x1, y1 = _to_screen(args["x1"], args["y1"])
    x2, y2 = _to_screen(args["x2"], args["y2"])
    _pointer(x1, y1)

    def run():
        pyautogui.moveTo(x1, y1, duration=0.1)
        pyautogui.dragTo(x2, y2, duration=0.5, button="left")
    await asyncio.to_thread(run)
    return _text("已拖动")


@tool("scroll", "在截图坐标 (x, y) 处滚动鼠标滚轮。amount 正数向上，负数向下（每格约 3 行）。",
      {"x": int, "y": int, "amount": int})
async def scroll(args):
    x, y = _to_screen(args["x"], args["y"])
    _pointer(x, y)

    def run():
        pyautogui.moveTo(x, y)
        pyautogui.scroll(int(args["amount"]) * 120)
    await asyncio.to_thread(run)
    return _text("已滚动")


# ---------------------------------------------------------------- 键盘

def _paste(text: str) -> None:
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    if old is not None:
        pyperclip.copy(old)


@tool("type_text", "在当前焦点位置输入文字（支持中文，长文本走剪贴板粘贴）。输入前先确认焦点在正确的输入框。",
      {"text": str})
async def type_text(args):
    text = args["text"]
    if text.isascii() and len(text) <= 200 and "\n" not in text:
        await asyncio.to_thread(pyautogui.write, text, interval=0.01)
    else:
        await asyncio.to_thread(_paste, text)
    return _text(f"已输入 {len(text)} 个字符")


@tool("press_keys", "按键或组合键。例如 'enter'、'ctrl+c'、'alt+tab'、'win+r'、'ctrl+shift+esc'。"
      "多个按键用空格隔开会依次按下，例如 'down down enter'。",
      {"keys": str, "repeat": Annotated[int, "重复次数，默认 1"]})
async def press_keys(args):
    seq = str(args["keys"]).lower().split()
    alias = {"win": "winleft", "windows": "winleft", "cmd": "winleft", "control": "ctrl",
             "return": "enter", "escape": "esc", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}

    def run():
        for _ in range(int(args.get("repeat") or 1)):
            for combo in seq:
                keys = [alias.get(k, k) for k in combo.split("+")]
                pyautogui.hotkey(*keys)
                time.sleep(0.05)
    await asyncio.to_thread(run)
    return _text(f"已按下 {args['keys']}")


# ---------------------------------------------------------------- 窗口

def _windows() -> list:
    import pygetwindow as gw
    return [w for w in gw.getAllWindows() if w.title.strip() and w.visible and w.width > 50]


@tool("list_windows", "列出当前打开的所有窗口标题及位置。", {})
async def list_windows(args):
    ws = await asyncio.to_thread(_windows)
    lines = [f"- {w.title}  {'[最小化]' if w.isMinimized else ''}{'[当前]' if w.isActive else ''}" for w in ws]
    return _text("\n".join(lines) or "没有可见窗口")


def _focus(title: str) -> str:
    import win32con
    import win32gui
    ws = [w for w in _windows() if title.lower() in w.title.lower()]
    if not ws:
        return ""
    w = ws[0]
    hwnd = w._hWnd
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    pyautogui.press("alt")  # 绕过 Windows 对 SetForegroundWindow 的限制
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        w.activate()
    time.sleep(0.3)
    return w.title


@tool("focus_window", "把标题包含指定文字的窗口切到前台（会自动还原最小化）。", {"title": str})
async def focus_window(args):
    t = await asyncio.to_thread(_focus, args["title"])
    return _text(f"已切换到：{t}") if t else _text(f"没找到标题包含「{args['title']}」的窗口", error=True)


# ---------------------------------------------------------------- 控件树（UI Automation）

INTERACTIVE = {"ButtonControl", "EditControl", "MenuItemControl", "ListItemControl", "TabItemControl",
               "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "HyperlinkControl",
               "TreeItemControl", "DataItemControl", "SplitButtonControl", "MenuBarControl",
               "DocumentControl", "TextControl", "ImageControl"}


def _inspect(title: str, max_items: int) -> str:
    import uiautomation as auto
    global _elements
    with auto.UIAutomationInitializerInThread():
        if title:
            ws = [w for w in _windows() if title.lower() in w.title.lower()]
            if not ws:
                return f"没找到标题包含「{title}」的窗口"
            root = auto.ControlFromHandle(ws[0]._hWnd)
        else:
            root = auto.GetForegroundControl().GetTopLevelControl()
        _elements = []
        lines = [f"窗口：{root.Name}"]
        deadline = time.time() + 8
        for ctrl, depth in auto.WalkControl(root, includeTop=False, maxDepth=25):
            if time.time() > deadline or len(_elements) >= max_items:
                lines.append("…（控件太多，已截断；可指定更具体的窗口或用截图）")
                break
            try:
                if ctrl.ControlTypeName not in INTERACTIVE or ctrl.IsOffscreen:
                    continue
                name = (ctrl.Name or "").strip()
                if not name and ctrl.ControlTypeName in ("TextControl", "ImageControl"):
                    continue
                r = ctrl.BoundingRectangle
                if r.width() <= 0 or r.height() <= 0:
                    continue
                cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            except Exception:
                continue
            _elements.append({"cx": cx, "cy": cy, "name": name})
            sx, sy = _to_shot(cx, cy)
            kind = ctrl.ControlTypeName.replace("Control", "")
            lines.append(f"[{len(_elements) - 1}] {kind} 「{name[:60]}」 中心({sx},{sy})")
        return "\n".join(lines)


@tool("inspect_window", "读取窗口里的按钮、输入框、菜单等控件（名称+编号+截图坐标），比看截图找位置更准。"
      "title 留空表示当前前台窗口。之后可用 click_element 按编号点击。",
      {"title": Annotated[str, "窗口标题包含的文字，可留空"],
       "max_items": Annotated[int, "最多列出多少个控件，默认 150"]})
async def inspect_window(args):
    out = await asyncio.to_thread(_inspect, args.get("title") or "", int(args.get("max_items") or 150))
    return _text(out)


@tool("click_element", "点击 inspect_window 列出的第 index 个控件。", {"index": int, "clicks": Annotated[int, "1 或 2"]})
async def click_element(args):
    i = int(args["index"])
    if not 0 <= i < len(_elements):
        return _text("编号无效，请先调用 inspect_window", error=True)
    e = _elements[i]
    _pointer(e["cx"], e["cy"])
    await asyncio.to_thread(pyautogui.click, e["cx"], e["cy"], clicks=int(args.get("clicks") or 1), interval=0.08)
    return _text(f"已点击「{e['name']}」")


# ---------------------------------------------------------------- 剪贴板

@tool("get_clipboard", "读取剪贴板里的文字。", {})
async def get_clipboard(args):
    return _text(pyperclip.paste() or "（剪贴板为空或不是文字）")


@tool("set_clipboard", "把文字放进剪贴板。", {"text": str})
async def set_clipboard(args):
    pyperclip.copy(args["text"])
    return _text("已复制到剪贴板")


# ---------------------------------------------------------------- 打开软件 / 文件 / 网址

START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path.home() / "Desktop",
    Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Desktop",
]


def _find_shortcut(name: str) -> Path | None:
    name = name.lower()
    best = None
    for d in START_MENU_DIRS:
        if not d.exists():
            continue
        for p in d.rglob("*.lnk"):
            stem = p.stem.lower()
            if stem == name:
                return p
            if name in stem and (best is None or len(stem) < len(best.stem)):
                best = p
    return best


def _open(target: str) -> str:
    t = target.strip().strip('"')
    if t.startswith(("http://", "https://", "mailto:", "ms-settings:")) or os.path.exists(t):
        os.startfile(t)
        return f"已打开 {t}"
    exe = shutil.which(t)
    if exe:
        subprocess.Popen([exe], creationflags=subprocess.DETACHED_PROCESS)
        return f"已启动 {exe}"
    lnk = _find_shortcut(t)
    if lnk:
        os.startfile(str(lnk))
        return f"已通过快捷方式启动 {lnk.stem}"
    subprocess.Popen(f'start "" "{t}"', shell=True)
    return f"已尝试用 start 打开 {t}（若没反应，说明系统找不到这个程序）"


@tool("open", "打开软件、文件、文件夹或网址。可以直接写软件名（如 微信、记事本、notepad、chrome、Excel），"
      "会自动在开始菜单和桌面快捷方式里查找；也可以写完整路径或 https 网址。",
      {"target": str})
async def open_target(args):
    msg = await asyncio.to_thread(_open, args["target"])
    await asyncio.sleep(1.0)
    return _text(msg + "。软件启动需要时间，可以 wait 一下再截图确认。")


@tool("list_apps", "列出开始菜单和桌面上可以打开的软件名称（可用关键字筛选）。",
      {"keyword": Annotated[str, "筛选关键字，可留空"]})
async def list_apps(args):
    kw = (args.get("keyword") or "").lower()

    def run():
        names = set()
        for d in START_MENU_DIRS:
            if d.exists():
                names.update(p.stem for p in d.rglob("*.lnk") if kw in p.stem.lower())
        return sorted(names)
    names = await asyncio.to_thread(run)
    return _text("\n".join(names[:300]) or "没有匹配的软件")


# ---------------------------------------------------------------- 其他

@tool("wait", "等待若干秒（等软件启动、网页加载）。", {"seconds": float})
async def wait(args):
    await asyncio.sleep(min(float(args["seconds"]), 30))
    return _text("等待完成")


@tool("list_processes", "列出正在运行的程序（按内存占用排序）。", {"keyword": Annotated[str, "筛选关键字，可留空"]})
async def list_processes(args):
    kw = (args.get("keyword") or "").lower()

    def run():
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                if kw and kw not in (p.info["name"] or "").lower():
                    continue
                rows.append((p.info["memory_info"].rss, p.info["pid"], p.info["name"]))
            except Exception:
                pass
        rows.sort(reverse=True)
        return "\n".join(f"{n}  pid={pid}  {m // 2**20}MB" for m, pid, n in rows[:60])
    return _text(await asyncio.to_thread(run) or "没有匹配的进程")


@tool("find_files", "按文件名在整台电脑上搜主人的文档、图片、视频、音乐、压缩包、软件快捷方式（毫秒级，"
      "比 Glob / dir 快得多；不含系统文件、程序文件和代码文件）。多个关键词用空格分开；"
      "支持 *.pdf 通配、ext:docx;pdf 限定类型、!词 排除、带 \\ 的路径片段（如 下载\\ 发票）。"
      "结果按主人的使用习惯排序（常打开的在前），返回完整路径。",
      {"query": str, "limit": Annotated[int, "最多返回几条，默认 30"]})
async def find_files(args):
    from .. import fileindex
    fs = fileindex.instance
    if fs is None:
        return _text("文件索引不可用", error=True)
    if not fs.ix.built and fs.state != "ready":
        return _text("文件索引还在建立中（约 20 秒），请稍后再试，或先用 Glob", error=True)
    r = await asyncio.to_thread(fs.search, args.get("query", ""), "all", "smart", 0, int(args.get("limit") or 30))
    if not r["items"]:
        return _text("没有找到。可以换个关键词、少写几个字，或者用 Glob 在具体文件夹里找（代码等文件不在索引里）。")
    lines = [f"{it['path']}  {it['size']}  {it['time']}".rstrip() for it in r["items"]]
    more = f"\n（共 {r['total']} 个，只列出前 {len(lines)} 个）" if r["total"] > len(lines) else ""
    return _text("\n".join(lines) + more)


ALL_TOOLS = [find_files, screenshot, screen_info, cursor_position, click, move_mouse, drag, scroll, type_text,
             press_keys, list_windows, focus_window, inspect_window, click_element, get_clipboard,
             set_clipboard, open_target, list_apps, wait, list_processes]
