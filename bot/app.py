"""桌面版：屏幕右侧的 Copilot 式面板 + 系统托盘 + 全局快捷键。

启动：双击「启动 Bot.vbs」，或 .venv\\Scripts\\python -m bot
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from .tools import desktop  # 先导入：设置 DPI 感知
from . import sop
from .agent import BotSession
from . import __version__
from .config import CONFIG_PATH, EFFORTS, FROZEN, MEMORY_PATH, MODELS, POLICIES, RES_DIR, ROOT, Config

import pystray
import webview
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageFont

TITLE = "我的 AI Bot"
UI_DIR = RES_DIR / "bot" / "ui"
STARTUP_LINK = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/我的 AI Bot.vbs"
LAUNCHER = ROOT / "启动 Bot.vbs"
user32 = ctypes.windll.user32


def launcher_script() -> str:
    """隐藏控制台窗口启动 Bot 的 VBS（子进程继承隐藏的控制台，不会闪黑框）。exe 版直接启动 exe。"""
    if FROZEN:
        command = f'"""{sys.executable}"""'
    else:
        command = f'"""{ROOT / ".venv" / "Scripts" / "python.exe"}"" -m bot"'
    return (f'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.CurrentDirectory = "{ROOT}"\r\n'
            f'sh.Run {command}, 0, False\r\n')


def write_launcher(path: Path) -> None:
    path.write_text(launcher_script(), encoding="utf-16")  # 中文路径必须用 UTF-16


MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
VKEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "`": 0xC0, "backquote": 0xC0,
         **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def hotkey_loop(spec: str, callback) -> None:
    """用 Win32 RegisterHotKey 注册全局快捷键（在本线程收 WM_HOTKEY）。"""
    mods, vk = 0x4000, 0  # MOD_NOREPEAT
    for part in spec.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in VKEYS:
            vk = VKEYS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
    if not vk or not user32.RegisterHotKey(None, 1, mods, vk):
        print(f"快捷键 {spec} 注册失败（可能被其他软件占用），请在 config.yaml 里换一个")
        return
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            try:
                callback()
            except Exception as e:
                print("快捷键处理出错：", e)


def make_icon(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4, fill=(0, 113, 227, 255))
    try:
        font = ImageFont.truetype("seguisb.ttf", int(size * 0.62))
    except OSError:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2 + 1), "B", font=font, fill="white", anchor="mm")
    return img


class Api:
    """暴露给网页的方法（window.pywebview.api.xxx）。"""

    def __init__(self, app: "BotApp"):
        self._app = app

    def state(self):
        cfg = self._app.cfg
        return {"model": cfg.model, "models": MODELS, "policy": cfg.permission_policy,
                "policies": POLICIES, "effort": cfg.effort, "efforts": EFFORTS,
                "hotkey": cfg.hotkey, "autostart": STARTUP_LINK.exists(),
                "busy": self._app.bot.busy if self._app.bot else False}

    def send(self, text: str):
        self._app.submit(self._app.bot.send(text))

    def stop(self):
        self._app.submit(self._app.bot.interrupt())

    def new_chat(self):
        self._app.submit(self._app.bot.start())

    def permission(self, req_id: str, decision: str):
        self._app.resolve_permission(req_id, decision)

    def set_model(self, model: str):
        self._app.submit(self._app.bot.set_model(model))

    def set_policy(self, policy: str):
        self._app.cfg.save_keys(permission_policy=policy)

    def set_effort(self, effort: str):
        self._app.cfg.save_keys(effort=effort)  # 新对话生效

    def history(self):
        from claude_agent_sdk import list_sessions
        rows = list_sessions(directory=self._app.cfg.work_dir, limit=40)
        return [{"id": s.session_id, "title": s.custom_title or s.summary or s.first_prompt or "（无标题）",
                 "time": datetime.fromtimestamp(s.last_modified / 1000).strftime("%m-%d %H:%M")}
                for s in rows]

    def resume(self, session_id: str):
        from claude_agent_sdk import get_session_messages
        out = []
        for m in get_session_messages(session_id, directory=self._app.cfg.work_dir):
            content = (m.message or {}).get("content") if isinstance(m.message, dict) else None
            if m.parent_tool_use_id:
                continue
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            else:
                text = ""
            if text.strip() and not text.startswith("<"):
                out.append({"role": m.type, "text": text})
        self._app.submit(self._app.bot.start(resume=session_id))
        return out

    def set_autostart(self, on: bool):
        if on:
            write_launcher(STARTUP_LINK)
        elif STARTUP_LINK.exists():
            STARTUP_LINK.unlink()
        return STARTUP_LINK.exists()

    def open_path(self, which: str):
        os.startfile(str({"config": CONFIG_PATH, "memory": MEMORY_PATH, "folder": ROOT}[which]))

    def hide(self):
        self._app.hide()

    def sop_cmd(self, cmd: str):
        if cmd == "restart":
            sop.restart()
        else:
            sop.send(cmd)

    def toggle_pin(self):
        return self._app.toggle_pin()


class BotApp:
    def __init__(self):
        self.cfg = Config.load()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="bot-loop").start()
        self.bot: BotSession | None = None
        self.window: webview.Window | None = None
        self.hwnd = 0
        self.visible = True
        self.pinned = True
        self.auto_hidden = False
        self._pending: dict[str, asyncio.Future] = {}
        self._events: queue.Queue = queue.Queue()
        self.tray: pystray.Icon | None = None

    # ------------------------------------------------------------ 线程桥
    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def emit(self, ev: dict) -> None:
        self._events.put(ev)
        if ev["type"] == "status" and ev["state"] == "idle" and self.auto_hidden:
            self.auto_hidden = False
            self.show()

    def _pump(self) -> None:
        """把事件批量推给网页；合并连续的文字增量，避免卡顿。"""
        while True:
            batch = [self._events.get()]
            time.sleep(0.03)
            while not self._events.empty():
                batch.append(self._events.get_nowait())
            merged: list[dict] = []
            for ev in batch:
                if ev["type"] == "text_delta" and merged and merged[-1]["type"] == "text_delta":
                    merged[-1]["text"] += ev["text"]
                else:
                    merged.append(dict(ev))
            try:
                self.window.evaluate_js(f"window.bot && bot.onEvents({json.dumps(merged, ensure_ascii=False)})")
            except Exception:
                pass

    async def ask_permission(self, req: dict) -> str:
        fut = self.loop.create_future()
        self._pending[req["id"]] = fut
        self.emit(req)
        if not self.visible:
            self.auto_hidden = False
            self.show()
        try:
            return await fut
        finally:
            self._pending.pop(req["id"], None)

    def resolve_permission(self, req_id: str, decision: str) -> None:
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            self.loop.call_soon_threadsafe(fut.set_result, decision)

    # ------------------------------------------------------------ 窗口
    def _place(self) -> None:
        rect = ctypes.wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA（物理像素）
        scale = user32.GetDpiForWindow(self.hwnd) / 96 if self.hwnd else 1
        margin = int(12 * scale)
        w = int(self.cfg.panel_width * scale)
        h = rect.bottom - rect.top - margin * 2
        x = rect.right - w - margin
        try:
            self.window.on_top = self.pinned
        except Exception:
            pass
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST if self.pinned else win32con.HWND_NOTOPMOST,
                              x, rect.top + margin, w, h, win32con.SWP_SHOWWINDOW)

    def _on_shown(self) -> None:
        for _ in range(50):
            self.hwnd = win32gui.FindWindow(None, TITLE)
            if self.hwnd:
                break
            time.sleep(0.1)
        if not self.hwnd:
            return
        # Win11 圆角；截图时不拍到面板自己（WDA_EXCLUDEFROMCAPTURE）
        pref = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)
        if not os.environ.get("BOT_SHOW_IN_CAPTURE"):  # 调试截图时可设此变量让面板可见
            user32.SetWindowDisplayAffinity(self.hwnd, 0x11)
        self._place()
        threading.Thread(target=self._keep_on_top, daemon=True, name="on-top").start()

    def _keep_on_top(self) -> None:
        """WebView 的显示/隐藏有时会丢掉置顶状态，定时补回来。"""
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        while True:
            time.sleep(1.5)
            try:
                if self.pinned and self.visible and not (
                        win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST):
                    win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            except Exception:
                pass

    def _bring_front(self) -> None:
        if not self.hwnd:
            return
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)  # 按一下 Alt，系统才允许抢前台
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass

    def show(self) -> None:
        self.window.show()
        self.visible = True
        self._place()
        self._bring_front()
        self.window.evaluate_js("window.bot && bot.focusInput()")

    def hide(self) -> None:
        self.window.hide()
        self.visible = False

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def toggle_pin(self) -> bool:
        self.pinned = not self.pinned
        self._place()
        return self.pinned

    def _before_pointer(self, x: int, y: int) -> None:
        """Bot 要点击的位置被面板挡住时，先把面板收起来，任务结束后自动恢复。"""
        if not (self.visible and self.hwnd):
            return
        l, t, r, b = win32gui.GetWindowRect(self.hwnd)
        if l <= x <= r and t <= y <= b:
            self.auto_hidden = True
            self.hide()
            time.sleep(0.25)

    def _on_closing(self):
        self.hide()
        return False  # 关闭按钮只是隐藏

    def on_sop_message(self, msg: dict) -> None:
        """截图工具栏的「AI」/ OCR 结果的「发给 AI」：叫出面板，把内容填进输入框，等主人补一句问题。"""
        print("SOP → Bot:", msg.get("cmd"))
        if msg.get("cmd") == "ask_image":
            text = f"请看这张截图：{msg['path']}\n"
        elif msg.get("cmd") == "ask_text":
            text = f'下面是我截图识别出的文字：\n"""\n{msg["text"]}\n"""\n'
        else:
            return
        self.auto_hidden = False
        self.show()
        self.window.evaluate_js(f"window.bot && bot.prefill({json.dumps(text, ensure_ascii=False)})")

    # ------------------------------------------------------------ 托盘
    def _sop_menu(self) -> pystray.Menu:
        """截图 / OCR / 快捷短语（sop/bin/SOP.exe）的菜单，热键显示读自它的 config.json。"""
        def item(label, cmd, hk=None):
            if hk:
                label = f"{label}　{sop.hotkey(*hk).upper()}"
            return pystray.MenuItem(label, lambda: sop.send(cmd))
        return pystray.Menu(
            item("截图", "capture", ("capture", "hotkeySnip", "F1")),
            item("贴图（剪贴板）", "pin", ("capture", "hotkeyPin", "F3")),
            item("截图识字", "ocr", ("ocr", "hotkeyScreenshot", "win+alt+c")),
            item("识别剪贴板图片", "ocr-clipboard", ("ocr", "hotkeyClipboard", "win+alt+v")),
            item("上次识别结果", "ocr-last"),
            item("关闭所有贴图", "pins-close"),
            pystray.Menu.SEPARATOR,
            item("编辑快捷短语（r1 → [1]）", "hotstrings-edit"),
            item("编辑热键与工作流（config.json）", "edit-config"),
            item("热键一览", "hotkeys"),
            pystray.MenuItem("重启这个模块", lambda: sop.restart()),
        )

    def _tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏", lambda: self.toggle(), default=True),
            pystray.MenuItem("新对话", lambda: (self.submit(self.bot.start()), self.window.evaluate_js("bot.clear()"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("截图与识字", self._sop_menu(), visible=sop.available()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开配置文件", lambda: os.startfile(str(CONFIG_PATH))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.quit()),
        )
        self.tray = pystray.Icon("my-ai-bot", make_icon(), f"{TITLE} v{__version__}（{self.cfg.hotkey}）", menu)
        self.tray.run_detached()

    def quit(self) -> None:
        try:
            self.submit(self.bot.stop()).result(timeout=5)
        except Exception:
            pass
        sop.stop()
        if self.tray:
            self.tray.stop()
        self.window.destroy()

    # ------------------------------------------------------------ 启动
    def run(self) -> None:
        if not FROZEN and not LAUNCHER.exists():  # exe 版双击 exe 即可
            write_launcher(LAUNCHER)
        self.bot = BotSession(self.cfg, self.emit, self.ask_permission)
        desktop.before_pointer = self._before_pointer
        self.window = webview.create_window(
            TITLE, str(UI_DIR / "index.html"), js_api=Api(self), width=self.cfg.panel_width, height=800,
            frameless=True, easy_drag=False, on_top=True, background_color="#f5f5f7", text_select=True,
        )
        self.window.events.closing += self._on_closing
        self.window.events.shown += lambda: threading.Thread(target=self._on_shown, daemon=True).start()
        threading.Thread(target=self._pump, daemon=True, name="ui-pump").start()
        self.submit(self.bot.start())
        threading.Thread(target=hotkey_loop, args=(self.cfg.hotkey, self.toggle), daemon=True,
                         name="hotkey").start()
        sop.listen(self.on_sop_message)
        sop.start()  # 截图 / OCR / 快捷短语，几百毫秒就绪，不等面板
        self._tray()
        webview.start(debug="--debug" in sys.argv)


def main() -> None:
    # 只允许运行一个实例；重复启动时把已有面板叫出来
    ctypes.windll.kernel32.CreateMutexW(None, False, "MyAIBot.SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = win32gui.FindWindow(None, TITLE)
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(hwnd)
        return
    (ROOT / "data").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(ROOT / "data" / "app.log", "a", encoding="utf-8", buffering=1)
    print(f"\n==== 启动 v{__version__} {datetime.now():%Y-%m-%d %H:%M:%S} ====")
    BotApp().run()
