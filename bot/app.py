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
from . import dock, fileindex, shellicon, sop, suggest, updater, usage
from .agent import BotSession
from .brand import make_icon
from . import __version__
from .config import CONFIG_PATH, EFFORTS, FROZEN, MEMORY_PATH, MODELS, POLICIES, RES_DIR, ROOT, Config

import pystray
import webview
import win32api
import win32con
import win32gui

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


class Api:
    """暴露给网页的方法（window.pywebview.api.xxx）。"""

    def __init__(self, app: "BotApp"):
        self._app = app

    def state(self):
        cfg = self._app.cfg
        return {"model": cfg.model, "models": MODELS, "policy": cfg.permission_policy,
                "policies": POLICIES, "effort": cfg.effort, "efforts": EFFORTS,
                "hotkey": cfg.hotkey, "search_hotkey": cfg.search_hotkey, "autostart": STARTUP_LINK.exists(),
                "busy": self._app.bot.busy if self._app.bot else False,
                "version": __version__, "update": self._app.update_info,
                "pinned": self._app.pinned, "nickname": cfg.nickname,
                "look": {"fluid_color": cfg.fluid_color, "fluid_opacity": cfg.fluid_opacity,
                         "avatar_size": cfg.avatar_size}}

    def set_look(self, look: dict):
        """背景墨迹颜色 / 浓淡、3D 形象大小（面板里拖滑块时调用）。"""
        cfg = self._app.cfg
        color = str(look.get("fluid_color", cfg.fluid_color))
        cfg.save_keys(fluid_color=f'"{color}"', fluid_opacity=round(float(look.get("fluid_opacity", 0.8)), 2),
                      avatar_size=int(look.get("avatar_size", 180)))
        cfg.fluid_color = color  # 文件里带引号（# 开头的颜色在 YAML 里要引起来），内存里不带

    def check_update(self):
        return self._app.check_update()

    def do_update(self):
        self._app.start_update()

    def usage(self, force: bool = False):
        return self._app.read_usage(force)

    def set_admin_key(self, key: str):
        """先用这个密钥读一次 API 用量，读得到才保存。返回值里不含密钥。"""
        key = key.strip()
        info = usage.api_usage(key, credit=self._app.cfg.api_credit)
        if "error" in info:
            return {"ok": False, "error": info["error"]}
        usage.set_admin_key(key)
        self._app.usage_cache = None
        return {"ok": True}

    def clear_admin_key(self):
        usage.clear_admin_key()
        self._app.usage_cache = None

    def open_url(self, which: str):
        import webbrowser
        webbrowser.open({"billing": usage.BILLING_URL, "usage": usage.USAGE_URL, "admin_keys": usage.ADMIN_KEYS_DOC}[which])

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

    def set_hold(self, on: bool):
        self._app._hold = bool(on)

    def set_searching(self, on: bool):
        self._app._searching = bool(on)

    def suggestions(self, refresh: bool = False):
        return self._app.suggestions(refresh)

    # ---------------- 文件搜索
    def search(self, query: str, cat: str = "all", sort: str = "smart", offset: int = 0):
        return self._app.files.search(query, cat, sort, int(offset))

    def search_icons(self, items: list):
        return shellicon.icons(items)

    def search_status(self):
        return self._app.files.status()

    def search_rebuild(self):
        self._app.files.rebuild()

    def search_open(self, path: str, query: str = ""):
        """打开文件；没固定面板的话顺手收起来，把位置让给打开的软件。"""
        try:
            os.startfile(path)
        except OSError as e:
            return {"ok": False, "error": f"打不开：{e.strerror or e}"}
        self._app.files.record_open(path, query)
        if not self._app.pinned:
            self._app.hide()
        return {"ok": True}

    def search_reveal(self, path: str, query: str = ""):
        """在资源管理器里打开所在文件夹并选中它。"""
        import subprocess
        if not os.path.exists(path):
            return {"ok": False, "error": "文件已经不在了"}
        subprocess.Popen(f'explorer /select,"{path}"')
        self._app.files.record_open(path, query, 0.5)
        if not self._app.pinned:
            self._app.hide()
        return {"ok": True}

    def search_copy(self, path: str, kind: str = "path"):
        """复制路径（文字），或复制文件本身（可以直接粘贴到微信、资源管理器）。"""
        import struct
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            if kind == "file":
                files = (path + chr(0) * 2).encode("utf-16-le")
                win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, struct.pack("IiiII", 20, 0, 0, 0, 1) + files)
                effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
                win32clipboard.SetClipboardData(effect, struct.pack("I", 1))  # DROPEFFECT_COPY
            else:
                win32clipboard.SetClipboardText(path, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def search_props(self, path: str):
        """系统的「属性」对话框。"""
        from win32com.shell import shell, shellcon
        shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_INVOKEIDLIST, lpVerb="properties", lpFile=path, nShow=1)


class BotApp:
    def __init__(self):
        self.cfg = Config.load()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="bot-loop").start()
        self.bot: BotSession | None = None
        self.window: webview.Window | None = None
        self.hwnd = 0
        self.visible = True
        self.pinned = self.cfg.dock == "pinned"  # 固定显示；否则鼠标离开就自动收到屏幕边上
        self.auto_hidden = False  # Bot 操作鼠标时临时收起的
        self._armed = False       # 鼠标进过面板之后才会自动收起（快捷键呼出时不会马上缩回去）
        self._hold = False        # 网页说先别收（菜单开着、输入框里有字）
        self._winq: queue.Queue = queue.Queue()  # 显示 / 收起请求，全在 window-ops 线程里按顺序执行
        self._win_thread = threading.Thread(target=self._run_window_ops, daemon=True, name="window-ops")
        self._win_thread.start()
        self._animating = False
        self._searching = False   # 网页里文件搜索开着
        self._suggesting = False
        self._quitting = False
        self.edge: dock.EdgeHandle | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._events: queue.Queue = queue.Queue()
        self.tray: pystray.Icon | None = None
        self.update_info: dict | None = None  # 有新版本时：updater.check() 的结果
        self._updating = False
        self.usage_cache: dict | None = None
        self._usage_lock = threading.Lock()
        self.files = fileindex.FileSearch(self.emit, self.cfg.search_exclude, self.cfg.search_extensions)
        fileindex.instance = self.files  # 给 Bot 的 find_files 工具用

    # ------------------------------------------------------------ 线程桥
    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def emit(self, ev: dict) -> None:
        self._events.put(ev)
        if ev["type"] == "status" and ev["state"] == "idle":
            if self.auto_hidden:
                self.auto_hidden = False
                self.show_later()
            elif not self.visible and self.edge:
                self.edge.pulse()  # 收着的时候做完了：边缘把手呼吸几下

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
            self.show_later(activate=True)
        try:
            return await fut
        finally:
            self._pending.pop(req["id"], None)

    def resolve_permission(self, req_id: str, decision: str) -> None:
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            self.loop.call_soon_threadsafe(fut.set_result, decision)

    # ------------------------------------------------------------ 窗口（贴边停靠）
    def _geometry(self) -> tuple[int, int, int, int]:
        """面板展开时的位置和大小（物理像素）：贴着屏幕右侧，上下留边。"""
        left, top, right, bottom = dock.work_area()
        scale = user32.GetDpiForWindow(self.hwnd) / 96 if self.hwnd else 1
        margin = int(12 * scale)
        w = int(self.cfg.panel_width * scale)
        return right - w - margin, top + margin, w, bottom - top - margin * 2

    def _place(self, show: bool = False) -> None:
        x, y, w, h = self._geometry()
        flags = win32con.SWP_NOACTIVATE | (win32con.SWP_SHOWWINDOW if show else 0)
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, x, y, w, h, flags)

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
        # 加上最小化按钮样式：任务栏点一下 / Win+D 才能最小化（随后当作收起处理）
        style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_STYLE)
        win32gui.SetWindowLong(self.hwnd, win32con.GWL_STYLE, style | win32con.WS_MINIMIZEBOX)
        self._place(show=True)
        threading.Thread(target=self._keep_on_top, daemon=True, name="on-top").start()
        threading.Thread(target=self._dock_loop, daemon=True, name="dock").start()
        # 启动时先露个面，鼠标不过来的话几秒后自己滑到边上
        threading.Timer(3, lambda: setattr(self, "_armed", True)).start()

    def _keep_on_top(self) -> None:
        """WebView 的显示/隐藏有时会丢掉置顶状态，定时补回来。"""
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        while True:
            time.sleep(1.5)
            try:
                if self.visible and not (
                        win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST):
                    win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            except Exception:
                pass

    def _dock_loop(self) -> None:
        """每 40ms 看一次鼠标：碰到屏幕右边缘就滑出面板；离开面板一会儿就收起（固定模式除外）。"""
        edge_since = out_since = misplaced_since = 0.0
        scale = user32.GetDpiForSystem() / 96
        while True:
            time.sleep(0.04)
            try:
                x, y = dock.cursor()
                left, top, right, bottom = dock.work_area()
                now = time.monotonic()
                if self.visible and user32.IsIconic(self.hwnd):
                    # 被 Windows 最小化了（任务栏按钮、Win+D）：当成收起
                    self._window_op(self._minimized)
                    time.sleep(0.2)
                    continue
                if not self.visible:
                    mid = (top + bottom) / 2
                    near = x >= right - 30 * scale and abs(y - mid) < 180 * scale
                    self.edge.set_hot(near)
                    # 碰到屏幕最右边，或者停在把手上
                    at_edge = x >= right - 2 * scale or (near and x >= right - 12 * scale)
                    if (at_edge and top <= y < bottom and not self.auto_hidden
                            and not dock.mouse_down() and not dock.fullscreen_app()):
                        edge_since = edge_since or now
                        # Bot 自己在操作鼠标时要停留久一点才弹出，免得挡住它
                        if now - edge_since >= (0.6 if self.bot and self.bot.busy else 0.12):
                            edge_since = 0
                            self.show(activate=False)
                    else:
                        edge_since = 0
                    continue
                l, t, r, b = win32gui.GetWindowRect(self.hwnd)
                # 自愈：展开着却不在该在的位置（被打断的动画、分辨率 / 缩放变了）超过 1 秒就摆回去
                gx, gy, gw, gh = self._geometry()
                if not self._animating and (abs(l - gx) > 4 or abs(t - gy) > 4 or abs(r - l - gw) > 4):
                    misplaced_since = misplaced_since or now
                    if now - misplaced_since > 1.0:
                        misplaced_since = 0
                        print("面板位置不对，摆回停靠位置：", (l, t, r, b), "→", (gx, gy))
                        self._window_op(self._place)
                else:
                    misplaced_since = 0
                pad = int(8 * scale)
                if (l - pad <= x <= r + pad and t - pad <= y <= b + pad) or x >= right - 2 * scale:
                    self._armed, out_since = True, 0
                    continue
                if self.pinned or not self._armed or self._hold or self._pending or dock.mouse_down():
                    out_since = 0
                    continue
                out_since = out_since or now
                if now - out_since >= 0.7:
                    out_since = 0
                    self.hide()
            except Exception as e:
                print("停靠出错：", e)
                time.sleep(1)

    def _unminimize(self, x: int, y: int, w: int, h: int) -> None:
        """被最小化过的窗口：直接恢复到屏幕外的起点（不激活），接着再滑进来。"""
        _, _, pt_min, pt_max, _ = win32gui.GetWindowPlacement(self.hwnd)
        win32gui.SetWindowPlacement(self.hwnd, (0, 4, pt_min, pt_max, (x, y, x + w, y + h)))  # SW_SHOWNOACTIVATE

    def _bring_front(self) -> None:
        if not self.hwnd:
            return
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)  # 按一下 Alt，系统才允许抢前台
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass

    # 所有显示 / 收起都放到 window-ops 这一个线程里按顺序做；托盘、快捷键、网页按钮、停靠循环只往队列里放请求。
    # 以前是各线程加锁后直接滑动窗口：跨线程移动窗口时，Windows 会让等待中的线程处理自己的消息，
    # 托盘线程于是在同一线程里重入（再点一下托盘 → 收起），自己等自己的锁，面板卡在半路、再也叫不动。
    def _run_window_ops(self) -> None:
        while True:
            fn, done = self._winq.get()
            try:
                fn()
            except Exception as e:
                print("窗口操作出错：", e)
            finally:
                self._animating = False
                if done:
                    done.set()

    def _window_op(self, fn, wait: bool = False) -> None:
        if threading.current_thread() is self._win_thread:
            fn()
            return
        done = threading.Event() if wait else None
        self._winq.put((fn, done))
        if done:
            done.wait(3)

    def show(self, activate: bool = True) -> None:
        self._window_op(lambda: self._show(activate))

    def show_later(self, activate: bool = False) -> None:
        self.show(activate)

    def hide(self, animate: bool = True, wait: bool = False) -> None:
        self._window_op(lambda: self._hide(animate), wait)

    def toggle(self) -> None:
        self._window_op(lambda: self._hide() if self.visible else self._show(True))

    def open_search(self) -> None:
        """搜索快捷键：叫出面板并打开文件搜索；已经在搜索就收起。"""
        def run():
            if self.visible and self._searching:
                self._hide()
                return
            self.auto_hidden = False
            self._show(True)
            self.emit({"type": "search_open"})
        self._window_op(run)

    def _show(self, activate: bool) -> None:
        """从屏幕右边滑进来（带一点回弹）。activate=False：鼠标碰边缘呼出时不抢键盘焦点。"""
        if not self.hwnd:
            return
        if not self.visible:
            x, y, w, h = self._geometry()
            right = dock.work_area()[2]
            if user32.IsIconic(self.hwnd):
                self._unminimize(right, y, w, h)
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, right, y, w, h, win32con.SWP_NOACTIVATE)
            user32.ShowWindow(self.hwnd, 5 if activate else 4)  # SW_SHOW / SW_SHOWNOACTIVATE
            self.visible, self._armed, self._animating = True, False, True
            threading.Thread(target=self.edge.hide, daemon=True).start()
            self.emit({"type": "shown"})
            dock.slide(self.hwnd, right, x, y, 320, dock.ease_out_back)
            self._animating = False
        self._place()  # 最后再精确摆一次（也顺便纠正任何偏移）
        if activate:
            self._bring_front()
            self.emit({"type": "focus"})

    def _hide(self, animate: bool = True) -> None:
        """滑出屏幕右边，只留边缘的小把手。"""
        if not self.visible or not self.hwnd:
            return
        self.visible = False
        if animate:
            self._animating = True
            l, t, r, b = win32gui.GetWindowRect(self.hwnd)
            dock.slide(self.hwnd, l, dock.work_area()[2] + 2, t, 180, dock.ease_in_cubic)
            self._animating = False
        user32.ShowWindow(self.hwnd, 0)
        self.emit({"type": "hidden"})  # 网页里暂停流体背景、3D 形象
        threading.Thread(target=self.edge.show, daemon=True).start()

    def _minimized(self) -> None:
        if self.visible and user32.IsIconic(self.hwnd):
            self.visible = False
            user32.ShowWindow(self.hwnd, 0)
            self.emit({"type": "hidden"})
            threading.Thread(target=self.edge.show, daemon=True).start()

    def toggle_pin(self) -> bool:
        self.pinned = not self.pinned
        self._armed = True
        self.cfg.save_keys(dock="pinned" if self.pinned else "auto")
        return self.pinned

    def _capture_guard(self, on: bool) -> None:
        """面板平时能被截图（主人自己截图要用）；只有 Bot 自己截屏的那一下对截图隐身，免得看到、点到自己。"""
        for hwnd in (self.hwnd, self.edge.hwnd if self.edge else 0):
            if hwnd:
                user32.SetWindowDisplayAffinity(hwnd, 0x11 if on else 0)  # WDA_EXCLUDEFROMCAPTURE / WDA_NONE
        if on:
            time.sleep(0.05)  # 等 DWM 下一帧生效

    def _before_pointer(self, x: int, y: int) -> None:
        """Bot 要点击的位置被面板挡住时，先把面板收起来，任务结束后自动恢复。"""
        if not (self.visible and self.hwnd):
            return
        l, t, r, b = win32gui.GetWindowRect(self.hwnd)
        if l <= x <= r and t <= y <= b:
            self.auto_hidden = True
            self.hide(animate=False, wait=True)
            time.sleep(0.15)

    def _on_closing(self):
        """任务栏右键「关闭窗口」/ Alt+F4：退出整个程序（和托盘的「退出」一样）。"""
        if self._quitting:
            return True
        threading.Thread(target=self.quit, daemon=True).start()
        return False  # 先取消这次关闭，quit() 收拾好后台再真正关窗口

    # ------------------------------------------------------------ 猜你想做
    def suggestions(self, refresh: bool = False) -> dict | None:
        """先返回缓存；缓存旧了（或要求换一批）就在后台让 Haiku 重新猜，猜好了推给网页。"""
        cached = suggest.load_cached()
        stale = not cached or time.time() - cached.get("ts", 0) > 2 * 3600
        if self.cfg.smart_suggestions and (refresh or stale) and not self._suggesting:
            self._suggesting = True

            def done(fut):
                self._suggesting = False
                try:
                    self.emit({"type": "suggestions", **fut.result()})
                except Exception as e:
                    print("猜你想做失败：", e)
                    self.emit({"type": "suggestions_failed"})
            self.submit(suggest.generate(self.cfg)).add_done_callback(done)
        return cached

    def on_sop_message(self, msg: dict) -> None:
        """截图工具栏的「AI」/ OCR 结果的「发给 AI」：叫出面板，把内容填进输入框，等主人补一句问题。"""
        print("SOP → Bot:", msg.get("cmd"))
        if msg.get("cmd") == "dump":  # 诊断用：把所有线程的调用栈写进日志
            import traceback
            for tid, frame in sys._current_frames().items():
                print(f"--- thread {tid}\n" + "".join(traceback.format_stack(frame)))
            return
        if msg.get("cmd") == "show":  # 又双击了一次 exe / 开始菜单
            self.auto_hidden = False
            self.show()
            return
        if msg.get("cmd") == "ask_image":
            text = f"请看这张截图：{msg['path']}\n"
        elif msg.get("cmd") == "ask_text":
            text = f'下面是我截图识别出的文字：\n"""\n{msg["text"]}\n"""\n'
        else:
            return
        self.auto_hidden = False
        self.show()
        self.window.evaluate_js(f"window.bot && bot.prefill({json.dumps(text, ensure_ascii=False)})")

    # ------------------------------------------------------------ 用量
    def read_usage(self, force: bool = False) -> dict:
        """订阅额度 + API 用量，同时读；60 秒内重复打开用缓存。"""
        with self._usage_lock:
            if not force and self.usage_cache and time.time() - self.usage_cache["ts"] < 60:
                return self.usage_cache
            sub_future = self.submit(usage.subscription())
            api = usage.api_usage(credit=self.cfg.api_credit)
            try:
                sub = sub_future.result(timeout=40)
            except Exception as e:
                sub = {"error": f"读取订阅用量失败：{e}"}
            self.usage_cache = {"sub": sub, "api": api,
                                "ts": time.time(), "time": datetime.now().strftime("%H:%M")}
            return self.usage_cache

    # ------------------------------------------------------------ 更新
    def check_update(self) -> dict:
        info = updater.check()
        print("检查更新：", {k: v for k, v in info.items() if k != "notes"})
        if info["available"]:
            self.update_info = info
            self.emit({"type": "update", "state": "available", "version": info["version"],
                       "notes": info["notes"], "size": info.get("size", 0)})
            if self.tray:
                self.tray.update_menu()
        return info

    def _update_loop(self) -> None:
        """启动 10 秒后查一次，之后每 6 小时查一次。"""
        time.sleep(10)
        while self.cfg.check_updates and not self.update_info:
            self.check_update()
            time.sleep(6 * 3600)

    def start_update(self) -> None:
        """下载更新包 → 后台静默安装 → 退出；安装程序装完会重新启动 Bot。"""
        if self._updating or not self.update_info:
            return
        self._updating = True

        def run():
            last = -1

            def progress(done, total):
                nonlocal last
                pct = int(done * 100 / total) if total else 0
                if pct != last:
                    last = pct
                    self.emit({"type": "update", "state": "downloading", "pct": pct})

            try:
                path = updater.download(self.update_info, progress)
                self.emit({"type": "update", "state": "installing"})
                updater.install(path)
                time.sleep(1)
                self.quit()
            except Exception as e:
                print("更新失败：", e)
                self._updating = False
                self.emit({"type": "update", "state": "error", "message": f"更新失败：{e}"})

        threading.Thread(target=run, daemon=True, name="update").start()

    def _tray_check_update(self) -> None:
        def run():
            info = self.check_update()
            if info["available"]:
                self.auto_hidden = False
                self.show()
            elif self.tray:
                self.tray.notify(info.get("error") or f"已是最新版本 v{__version__}", TITLE)
        threading.Thread(target=run, daemon=True).start()

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
            pystray.MenuItem(lambda _: f"更新到 v{self.update_info['version']}" if self.update_info else "",
                             lambda: (self.show(), self.start_update()), visible=lambda _: bool(self.update_info)),
            pystray.MenuItem(f"检查更新（当前 v{__version__}）", lambda: self._tray_check_update()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.quit()),
        )
        self.tray = pystray.Icon("my-ai-bot", make_icon(64, face=True), f"{TITLE} v{__version__}（{self.cfg.hotkey}）", menu)
        self.tray.run_detached()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.submit(self.bot.stop()).result(timeout=5)
        except Exception:
            pass
        sop.stop()
        if self.tray:
            self.tray.stop()
        # 保险：窗口关了但进程卡在退出阶段时强制结束，否则更新安装程序会一直等它
        threading.Timer(5, lambda: os._exit(0)).start()
        self.window.destroy()

    # ------------------------------------------------------------ 启动
    def run(self) -> None:
        if not FROZEN and not LAUNCHER.exists():  # exe 版双击 exe 即可
            write_launcher(LAUNCHER)
        if FROZEN and STARTUP_LINK.exists() and STARTUP_LINK.read_text(encoding="utf-16") != launcher_script():
            write_launcher(STARTUP_LINK)  # 开机自启指向当前这份程序（比如从源码版换成安装版）
        self.bot = BotSession(self.cfg, self.emit, self.ask_permission)
        desktop.before_pointer = self._before_pointer
        desktop.capture_guard = self._capture_guard
        self.window = webview.create_window(
            TITLE, str(UI_DIR / "index.html"), js_api=Api(self), width=self.cfg.panel_width, height=800,
            frameless=True, easy_drag=False, on_top=True, background_color="#f5f5f7", text_select=True,
        )
        self.window.events.closing += self._on_closing
        self.edge = dock.EdgeHandle()
        self.window.events.shown += lambda: threading.Thread(target=self._on_shown, daemon=True).start()
        threading.Thread(target=self._pump, daemon=True, name="ui-pump").start()
        self.submit(self.bot.start())
        threading.Thread(target=hotkey_loop, args=(self.cfg.hotkey, self.toggle), daemon=True,
                         name="hotkey").start()
        if self.cfg.search_hotkey:
            threading.Thread(target=hotkey_loop, args=(self.cfg.search_hotkey, self.open_search), daemon=True,
                             name="search-hotkey").start()
        self.files.start()
        sop.listen(self.on_sop_message)
        sop.start()  # 截图 / OCR / 快捷短语，几百毫秒就绪，不等面板
        self._tray()
        if FROZEN:
            threading.Thread(target=self._update_loop, daemon=True, name="update-check").start()
        # 窗口 / 任务栏图标用多尺寸的 icon.ico（和 exe、托盘同一个形象），高 DPI 下也清楚
        webview.start(debug="--debug" in sys.argv, icon=str(UI_DIR / "icon.ico"))


def main() -> None:
    # 只允许运行一个实例；重复启动时把已有面板叫出来
    ctypes.windll.kernel32.CreateMutexW(None, False, "MyAIBot.SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ipc = win32gui.FindWindow(None, sop.BOT_WINDOW)
        if ipc:
            user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY：让已运行的那个可以抢到前台
            sop._send_copydata(ipc, json.dumps({"cmd": "show"}))
        return
    (ROOT / "data").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(ROOT / "data" / "app.log", "a", encoding="utf-8", buffering=1)
    print(f"\n==== 启动 v{__version__} {datetime.now():%Y-%m-%d %H:%M:%S} ====")
    import logging  # pywebview 等库的报错也写进 app.log
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="%(name)s %(levelname)s: %(message)s")
    BotApp().run()
    print(f"==== 退出 {datetime.now():%H:%M:%S} ====")
    os._exit(0)  # 窗口已关闭：不等残留的后台线程（托盘、SDK 子进程管道），立即结束进程
