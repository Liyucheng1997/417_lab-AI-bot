"""贴边停靠：面板贴在屏幕右侧，离开时滑出屏幕，只留一条橙色细把手；鼠标碰到右边缘就滑回来。

- slide()：用 SetWindowPos 逐帧移动窗口（出现带一点回弹，收起干脆利落）
- EdgeHandle：分层（半透明、点击穿透）小窗口，画一条竖向胶囊；鼠标靠近时变宽变亮，任务完成时呼吸闪动
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

import win32con
import win32gui
from PIL import Image, ImageChops, ImageDraw


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SWP_MOVE_ONLY = win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE


def ease_out_back(t: float) -> float:
    c = 0.7  # 回弹幅度（越大越弹）
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def ease_in_cubic(t: float) -> float:
    return t * t * t


def slide(hwnd: int, x0: int, x1: int, y: int, ms: int, ease) -> None:
    start = time.perf_counter()
    while True:
        t = min((time.perf_counter() - start) * 1000 / ms, 1.0)
        user32.SetWindowPos(hwnd, 0, round(x0 + (x1 - x0) * ease(t)), y, 0, 0, SWP_MOVE_ONLY)
        if t >= 1:
            return
        time.sleep(0.007)


def work_area() -> tuple[int, int, int, int]:
    rect = wt.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def mouse_down() -> bool:
    return any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04))


def cursor() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def fullscreen_app() -> bool:
    """前台是全屏程序（视频、游戏、演示）时不弹出面板。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or win32gui.GetClassName(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return False
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return l <= 0 and t <= 0 and r >= sw and b >= sh


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                       wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.CreateCompatibleDC.restype = wt.HDC
# 句柄一律按 64 位传：shellicon 把 GetDC 的返回值设成了 HDC，大于 2^31 的句柄不声明类型会报「int too long to convert」
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.DeleteDC.argtypes = [wt.HDC]


class EdgeHandle:
    """屏幕右边缘的竖向小胶囊（面板收起时显示）。"""

    def __init__(self):
        scale = user32.GetDpiForSystem() / 96
        self.scale = scale
        self.hwnd = 0
        self.visible = False
        self.hot = False
        self.alpha = 0
        self._lock = threading.Lock()
        self._images = {hot: self._render(hot) for hot in (False, True)}
        ready = threading.Event()
        threading.Thread(target=self._loop, args=(ready,), daemon=True, name="edge-handle").start()
        ready.wait(3)

    def _render(self, hot: bool) -> Image.Image:
        s = self.scale
        w, h = round((7 if hot else 5) * s), round((96 if hot else 64) * s)
        img = Image.new("RGBA", (w + round(4 * s), h), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=w // 2, fill=(255, 149, 0, 255))  # 网站的「今日燃烧色」#ff9500
        r, g, b, a = img.split()  # UpdateLayeredWindow 要预乘 alpha 的 BGRA
        r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
        return Image.merge("RGBA", (b, g, r, a))

    def _loop(self, ready: threading.Event) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "MyAIBotEdgeHandle"
        wc.lpfnWndProc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)
        wc.hInstance = win32gui.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOPMOST
              | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
        self.hwnd = win32gui.CreateWindowEx(ex, wc.lpszClassName, "MyAIBotEdge", win32con.WS_POPUP,
                                            0, 0, 1, 1, 0, 0, wc.hInstance, None)
        ready.set()
        win32gui.PumpMessages()

    def _paint(self, alpha: int) -> None:
        img = self._images[self.hot]
        w, h = img.size
        left, top, right, bottom = work_area()
        x, y = right - w + round(4 * self.scale), top + (bottom - top - h) // 2
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        data = img.tobytes()
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem, bmp)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, alpha)), 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                   mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)

    def _fade(self, to: int, ms: int = 160) -> None:
        start, frm = time.perf_counter(), self.alpha
        while True:
            t = min((time.perf_counter() - start) * 1000 / ms, 1.0)
            self.alpha = round(frm + (to - frm) * t)
            self._paint(self.alpha)
            if t >= 1:
                return
            time.sleep(0.012)

    def show(self) -> None:
        with self._lock:
            if self.visible or not self.hwnd:
                return
            self.visible, self.hot = True, False
            self._paint(0)
            user32.ShowWindow(self.hwnd, 8)  # SW_SHOWNA
            self._fade(150)

    def hide(self) -> None:
        with self._lock:
            if not self.visible:
                return
            self.visible = False
            self._fade(0, 100)
            user32.ShowWindow(self.hwnd, 0)

    def set_hot(self, hot: bool) -> None:
        """鼠标靠近右边缘时变宽变亮，像被吸过去一样。"""
        with self._lock:
            if not self.visible or hot == self.hot:
                return
            self.hot = hot
            self._fade(255 if hot else 150, 120)

    def pulse(self, times: int = 3) -> None:
        """任务做完时在边缘呼吸几下，提醒主人来看。"""
        def run():
            for _ in range(times):
                with self._lock:
                    if not self.visible:
                        return
                    self._fade(255, 380)
                    self._fade(90, 380)
            with self._lock:
                if self.visible:
                    self._fade(150, 200)
        threading.Thread(target=run, daemon=True).start()
