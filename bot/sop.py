"""截图 / 贴图 / OCR / 快捷短语（替代 Snipaste、Umi-OCR、Reference.ahk）。

真正干活的是 sop/bin/SOP.exe（C# 写的常驻小程序，启动 0.2 秒，热键与原来那三个软件一致）。
Bot 启动时在后台拉起它（--embedded：不显示自己的托盘图标，菜单并入 Bot 托盘），Bot 退出时它跟着退出。

两个方向的通信都用 WM_COPYDATA：
  Bot → SOP：send("capture") 等命令，发到隐藏窗口 MySOP_IPC_Window
  SOP → Bot：截图工具栏的「AI」按钮、OCR 结果的「发给 AI」，发到本进程的隐藏窗口 MyAIBot_IPC
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from typing import Callable

import win32gui

from .config import ROOT

SOP_DIR = ROOT / "sop" / "bin"
SOP_EXE = SOP_DIR / "SOP.exe"
SOP_CONFIG = SOP_DIR / "config.json"
SOP_WINDOW = "MySOP_IPC_Window"
BOT_WINDOW = "MyAIBot_IPC"
WM_COPYDATA = 0x004A
user32 = ctypes.windll.user32


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", ctypes.c_uint32), ("lpData", ctypes.c_void_p)]


user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
user32.SendMessageW.restype = ctypes.c_ssize_t


def _send_copydata(hwnd: int, text: str) -> None:
    data = text.encode("utf-16-le")
    buf = ctypes.create_string_buffer(data, len(data))
    cds = COPYDATASTRUCT(0, len(data), ctypes.cast(buf, ctypes.c_void_p))
    user32.SendMessageW(hwnd, WM_COPYDATA, 0, ctypes.byref(cds))


def available() -> bool:
    return SOP_EXE.exists()


def running() -> bool:
    return bool(win32gui.FindWindow(None, SOP_WINDOW))


def start(*command: str) -> None:
    """在后台启动 SOP（已在运行则什么都不做）。"""
    if not available():
        print(f"没有找到 {SOP_EXE}，截图/OCR/快捷短语不可用")
        return
    subprocess.Popen([str(SOP_EXE), "--embedded", "--parent", str(os.getpid()), *command], cwd=str(SOP_DIR))


def send(cmd: str) -> None:
    """执行 SOP 命令：capture / pin / ocr / ocr-clipboard / hotkeys / reload / quit …"""
    hwnd = win32gui.FindWindow(None, SOP_WINDOW)
    if hwnd:
        _send_copydata(hwnd, cmd)
    elif cmd != "quit":
        start(cmd)  # 没在运行（比如崩了）：带着命令重新拉起


def stop() -> None:
    if running():
        send("quit")


def restart() -> None:
    stop()
    for _ in range(30):
        if not running():
            break
        time.sleep(0.1)
    start()


def hotkey(section: str, key: str, default: str) -> str:
    """从 SOP 的 config.json 读热键，用来在菜单里显示。"""
    try:
        return json.loads(SOP_CONFIG.read_text(encoding="utf-8"))[section][key] or default
    except Exception:
        return default


def listen(callback: Callable[[dict], None]) -> None:
    """建一个隐藏窗口接收 SOP 发来的消息（在独立线程里跑消息循环）。"""

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_COPYDATA:
            cds = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
            text = ctypes.wstring_at(cds.lpData, cds.cbData // 2)
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"cmd": text}
            threading.Thread(target=callback, args=(payload,), daemon=True).start()
            return 1
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def loop():
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "MyAIBotIPC"
        wc.lpfnWndProc = wndproc
        wc.hInstance = win32gui.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        win32gui.CreateWindow(wc.lpszClassName, BOT_WINDOW, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        win32gui.PumpMessages()

    threading.Thread(target=loop, daemon=True, name="sop-ipc").start()
