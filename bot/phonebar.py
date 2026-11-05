"""投屏窗口旁边的竖向工具条（苹果风白色胶囊，深色模式下是深灰）。

- 分层窗口 + PIL 画图标（Segoe Fluent Icons），鼠标悬停时在外侧显示中文标签；透明的地方点击会穿透。
- 窗口的「所有者」设为 scrcpy 的投屏窗口：永远在投屏窗口上面，投屏窗口最小化时一起隐藏，不会盖住别的软件。
- 每 50ms 跟着投屏窗口移动：默认贴在右侧，右边放不下就贴到左侧。
- 跑在自己的线程里（跨进程的所有者关系会把两个线程的输入队列连在一起，不能放在面板的界面线程上）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import winreg

import win32con
import win32gui
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .dock import BITMAPINFOHEADER, BLENDFUNCTION, gdi32, user32

ICON_FONTS = [r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf"]
TEXT_FONT = r"C:\Windows\Fonts\msyh.ttc"

# (动作, 图标, 标签)；None 是分隔线
BUTTONS = [
    ("back", "\ue72b", "返回"),
    ("home", "\ue80f", "主页"),
    ("recents", "\ue7c4", "多任务"),
    None,
    ("notifications", "\uea8f", "通知栏"),
    ("vol_up", "\ue995", "音量 +"),
    ("vol_down", "\ue993", "音量 −"),
    ("screenshot", "\ue722", "截图（保存并复制）"),
    ("power", "\ue7e8", "锁屏 / 亮屏"),
    None,
    ("pin", "\ue718", "窗口置顶"),
    ("type", "\ue765", "从电脑打字（支持中文）"),
    ("ask", "\ue99a", "让 Bot 操作手机"),
    None,
    ("close", "\ue711", "结束投屏"),
]

WM_MOUSELEAVE = 0x02A3
WM_REPAINT = win32con.WM_APP + 1
TME_LEAVE = 0x2


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


def _dark() -> bool:
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _font(paths: list[str], size: int):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


class PhoneBar:
    def __init__(self, owner: int, on_click, pinned: bool = False, margin: int = 0):
        self.owner = owner
        self.on_click = on_click
        self.pinned = pinned
        self.hover = -1
        self.pressed = -1
        self.hwnd = 0
        self.side = "right"
        self.pos = (0, 0)
        self._closed = False
        self.s = user32.GetDpiForSystem() / 96
        self.dark = _dark()
        s = self.s
        self.btn = round(38 * s)       # 每个按钮的高度（也是宽度）
        self.pad = round(6 * s)        # 胶囊内边距
        self.sep = round(11 * s)       # 分隔线占的高度
        self.bar_w = self.btn + self.pad * 2
        self.label_w = round(150 * s)  # 外侧留给悬停标签的位置
        self.shadow = round(10 * s)    # 阴影外扩
        self.gap = round(12 * s) + margin  # 和投屏画面的距离（margin = 手机边框宽度）
        self.icon_font = _font(ICON_FONTS, round(17 * s))
        self.text_font = _font([TEXT_FONT], round(12 * s))
        self.slots: list[tuple[int, int, int]] = []  # (按钮序号, 上, 下)，相对胶囊顶部
        y = self.pad
        for i, b in enumerate(BUTTONS):
            if b is None:
                y += self.sep
                continue
            self.slots.append((i, y, y + self.btn))
            y += self.btn
        self.bar_h = y + self.pad
        self.win_w = self.bar_w + self.label_w + self.shadow * 2
        self.win_h = self.bar_h + self.shadow * 2
        threading.Thread(target=self._loop, daemon=True, name="phone-bar").start()

    # ------------------------------------------------------------ 画
    def _render(self) -> Image.Image:
        s, sh = self.s, self.shadow
        img = Image.new("RGBA", (self.win_w, self.win_h), (0, 0, 0, 0))
        bx = sh if self.side == "right" else self.win_w - sh - self.bar_w  # 胶囊左上角
        by = sh
        r = self.bar_w // 2
        # 柔和阴影
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((bx, by + round(3 * s), bx + self.bar_w, by + self.bar_h + round(3 * s)),
                                                 radius=r, fill=(0, 0, 0, 70 if self.dark else 38))
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(round(6 * s))))
        d = ImageDraw.Draw(img)
        # ImageDraw 在 RGBA 上是直接替换像素、不混合，所以这里都用和底色混好的实色
        if self.dark:
            bg, line, hover_fill, press_fill = (44, 44, 46, 245), (72, 72, 76, 245), (64, 64, 68, 245), (84, 84, 88, 245)
            ink = (245, 245, 247, 255)
        else:
            bg, line, hover_fill, press_fill = (255, 255, 255, 245), (226, 226, 231, 245), (237, 237, 241, 245), (222, 222, 227, 245)
            ink = (29, 29, 31, 255)
        accent = (255, 149, 0, 255)
        d.rounded_rectangle((bx, by, bx + self.bar_w - 1, by + self.bar_h - 1), radius=r, fill=bg, outline=line, width=max(1, round(s)))
        # 分隔线
        y = by + self.pad
        for b in BUTTONS:
            if b is None:
                m = y + self.sep // 2
                d.line((bx + round(14 * s), m, bx + self.bar_w - round(14 * s), m), fill=line, width=max(1, round(s)))
                y += self.sep
            else:
                y += self.btn
        cx = bx + self.bar_w // 2
        for i, top, bottom in self.slots:
            action, glyph, label = BUTTONS[i]
            cy = by + (top + bottom) // 2
            if i in (self.hover, self.pressed):
                rr = self.btn // 2 - round(2 * s)
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=press_fill if i == self.pressed else hover_fill)
            on = action == "pin" and self.pinned
            color = accent if on else ink
            d.text((cx, cy), "\ue77a" if on else glyph, font=self.icon_font, fill=color, anchor="mm")
            if action in ("vol_up", "vol_down"):  # 小 +/− 角标
                mx, my, k, w = cx + round(10 * s), cy + round(7 * s), round(3 * s), max(1, round(1.4 * s))
                d.line((mx - k, my, mx + k, my), fill=color, width=w)
                if action == "vol_up":
                    d.line((mx, my - k, mx, my + k), fill=color, width=w)
        # 悬停标签（在胶囊外侧，不挡手机画面）
        if 0 <= self.hover < len(BUTTONS) and BUTTONS[self.hover]:
            label = BUTTONS[self.hover][2]
            if BUTTONS[self.hover][0] == "pin" and self.pinned:
                label = "取消置顶"
            top = next(t for i, t, _ in self.slots if i == self.hover)
            cy = by + top + self.btn // 2
            tw = d.textlength(label, font=self.text_font)
            ph, pw = round(24 * s), round(tw + 20 * s)
            lx = bx + self.bar_w + round(6 * s) if self.side == "right" else bx - round(6 * s) - pw
            tip_bg = (29, 29, 31, 235) if not self.dark else (235, 235, 240, 240)
            tip_ink = (255, 255, 255, 255) if not self.dark else (29, 29, 31, 255)
            d.rounded_rectangle((lx, cy - ph // 2, lx + pw, cy + ph // 2), radius=ph // 2, fill=tip_bg)
            d.text((lx + pw / 2, cy), label, font=self.text_font, fill=tip_ink, anchor="mm")
        rch, gch, bch, ach = img.split()  # UpdateLayeredWindow 要预乘 alpha 的 BGRA
        rch, gch, bch = (ImageChops.multiply(c, ach) for c in (rch, gch, bch))
        return Image.merge("RGBA", (bch, gch, rch, ach))

    def _paint(self) -> None:
        if not self.hwnd:
            return
        img = self._render()
        w, h = img.size
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        data = img.tobytes()
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem, bmp)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        x, y = self.pos
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                   mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)

    # ------------------------------------------------------------ 位置
    def _target(self) -> tuple[tuple[int, int], str] | None:
        from .phone import visible_rect
        if not win32gui.IsWindow(self.owner):
            return None
        l, t, r, b = visible_rect(self.owner)
        mon = user32.MonitorFromWindow(self.owner, 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        work = mi.rcWork
        side = "right" if r + self.gap + self.bar_w <= work.right else "left"
        y = (t + b) // 2 - self.bar_h // 2 - self.shadow
        y = max(work.top - self.shadow, min(y, work.bottom - self.bar_h - self.shadow))
        if side == "right":
            x = r + self.gap - self.shadow
        else:
            x = l - self.gap - self.bar_w - self.shadow - self.label_w
        return (x, y), side

    def _follow(self) -> None:
        tgt = self._target()
        if tgt is None:
            self.close()
            return
        pos, side = tgt
        iconic = user32.IsIconic(self.owner)
        if pos != self.pos or side != self.side:
            self.pos, self.side = pos, side
            self._paint()
        visible = bool(user32.IsWindowVisible(self.hwnd))
        if iconic and visible:
            user32.ShowWindow(self.hwnd, 0)
        elif not iconic and not visible:
            user32.ShowWindow(self.hwnd, 8)  # SW_SHOWNA

    # ------------------------------------------------------------ 鼠标
    def _hit(self, x: int, y: int) -> int:
        bx = self.shadow if self.side == "right" else self.win_w - self.shadow - self.bar_w
        if not (bx <= x < bx + self.bar_w):
            return -1
        y -= self.shadow
        for i, top, bottom in self.slots:
            if top <= y < bottom:
                return i
        return -1

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_TIMER:
            try:
                self._follow()
            except Exception:
                pass
            return 0
        if msg == WM_REPAINT:
            self._paint()
            return 0
        if msg == win32con.WM_MOUSEACTIVATE:
            return 3  # MA_NOACTIVATE：点工具条不抢走投屏窗口的键盘焦点
        if msg in (win32con.WM_MOUSEMOVE, win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP):
            x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
            i = self._hit(x, y)
            if msg == win32con.WM_MOUSEMOVE:
                tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                user32.TrackMouseEvent(ctypes.byref(tme))
                if i != self.hover:
                    self.hover = i
                    self._paint()
            elif msg == win32con.WM_LBUTTONDOWN and i >= 0:
                self.pressed = i
                user32.SetCapture(hwnd)
                self._paint()
            elif msg == win32con.WM_LBUTTONUP:
                user32.ReleaseCapture()
                hit = i if i == self.pressed else -1
                self.pressed = -1
                self._paint()
                if hit >= 0:
                    action = BUTTONS[hit][0]
                    threading.Thread(target=self._click, args=(action,), daemon=True).start()
            return 0
        if msg == WM_MOUSELEAVE:
            if self.hover != -1 or self.pressed != -1:
                self.hover = self.pressed = -1
                self._paint()
            return 0
        if msg == win32con.WM_SETCURSOR:
            win32gui.SetCursor(win32gui.LoadCursor(0, win32con.IDC_HAND if self.hover >= 0 else win32con.IDC_ARROW))
            return 1
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _click(self, action: str) -> None:
        try:
            self.on_click(action)
        except Exception as e:
            print("投屏工具条出错：", e)

    def _loop(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = f"MyAIBotPhoneBar{id(self)}"  # 每个工具条一个类名：窗口过程绑定的是这个对象
        wc.lpfnWndProc = self._proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        ex = win32con.WS_EX_LAYERED | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
        # 所有者 = 投屏窗口：跟着它的层级走，它最小化时一起隐藏
        self.hwnd = win32gui.CreateWindowEx(ex, wc.lpszClassName, "手机投屏工具条", win32con.WS_POPUP,
                                            0, 0, self.win_w, self.win_h, self.owner, 0, wc.hInstance, None)
        tgt = self._target()
        if tgt:
            self.pos, self.side = tgt
        self._paint()
        user32.ShowWindow(self.hwnd, 8)
        user32.SetTimer(self.hwnd, 1, 50, None)
        win32gui.PumpMessages()

    # ------------------------------------------------------------ 外部调用
    def set_pinned(self, on: bool) -> None:
        self.pinned = on
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, WM_REPAINT, 0, 0)  # 让工具条自己的线程重画

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass


# ====================================================================== 手机外框
def _blit(hwnd: int, img: Image.Image, pos: tuple[int, int]) -> None:
    """把预乘 alpha 的 BGRA 图画到分层窗口上。"""
    w, h = img.size
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    screen = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    data = img.tobytes()
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mem, bmp)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(*pos)), ctypes.byref(wt.SIZE(w, h)),
                               mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, screen)


def _premultiply(img: Image.Image) -> Image.Image:
    r, g, b, a = img.split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a))


gdi32.CreateRoundRectRgn.restype = wt.HRGN
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]


class PhoneFrame:
    """投屏画面外面的「手机」：深色细边框 + 圆角屏幕 + 柔和阴影（像苹果的 iPhone 镜像）。

    - scrcpy 窗口去掉了标题栏（--window-borderless），这里给它切圆角（SetWindowRgn），
      再在它正下方放一个分层窗口画边框和阴影，跟着它移动、缩放（横屏时也跟着变）。
    - 按住边框可以拖动整台「手机」；点边框会把键盘焦点交给手机画面。
    """

    def __init__(self, screen: int):
        self.screen = screen
        self.s = user32.GetDpiForSystem() / 96
        self.bezel = round(7 * self.s)      # 边框宽度
        self.shadow = round(26 * self.s)    # 阴影外扩
        self.hwnd = 0
        self.rect = None                    # 上次画的时候 scrcpy 窗口的位置大小
        self.drag = None
        self._closed = False
        threading.Thread(target=self._loop, daemon=True, name="phone-frame").start()

    @property
    def margin(self) -> int:
        """工具条要离手机画面多远才不压住边框。"""
        return self.bezel

    def _radius(self, w: int, h: int) -> int:
        return round(min(w, h) * 0.105)    # 手机屏幕的圆角

    def _render(self, w: int, h: int) -> Image.Image:
        s, b, sh = self.s, self.bezel, self.shadow
        W, H = w + 2 * (b + sh), h + 2 * (b + sh)
        r = self._radius(w, h) + b
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((sh, sh + round(8 * s), W - sh, H - sh + round(8 * s)), radius=r,
                                                 fill=(0, 0, 0, 90))
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(round(16 * s))))
        # 4 倍大小画再缩小：圆角边缘是平滑的
        k = 4
        big = Image.new("RGBA", ((w + 2 * b) * k, (h + 2 * b) * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        d.rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=r * k, fill=(58, 58, 60, 255))  # 外圈金属边
        e = max(1, round(1.2 * s)) * k
        d.rounded_rectangle((e, e, big.width - 1 - e, big.height - 1 - e), radius=r * k - e, fill=(12, 12, 13, 255))
        body = big.resize((w + 2 * b, h + 2 * b), Image.LANCZOS)
        img.alpha_composite(body, (sh, sh))
        return _premultiply(img)

    def _apply(self) -> None:
        """scrcpy 窗口位置或大小变了：重新切圆角、重画边框。"""
        from .phone import visible_rect
        l, t, r, b = visible_rect(self.screen)
        rect = (l, t, r - l, b - t)
        if rect == self.rect:
            return
        resized = not self.rect or rect[2:] != self.rect[2:]
        self.rect = rect
        x, y, w, h = rect
        if resized:
            rad = self._radius(w, h)
            wl, wt_, _, _ = win32gui.GetWindowRect(self.screen)
            ox, oy = l - wl, t - wt_
            rgn = gdi32.CreateRoundRectRgn(ox, oy, ox + w + 1, oy + h + 1, rad * 2, rad * 2)
            user32.SetWindowRgn(self.screen, rgn, True)  # 系统接管 rgn，不用自己释放
            self.img = self._render(w, h)
        _blit(self.hwnd, self.img, (x - self.bezel - self.shadow, y - self.bezel - self.shadow))

    def _follow(self) -> None:
        if not win32gui.IsWindow(self.screen):
            self.close()
            return
        iconic = user32.IsIconic(self.screen) or not user32.IsWindowVisible(self.screen)
        visible = bool(user32.IsWindowVisible(self.hwnd))
        if iconic:
            if visible:
                user32.ShowWindow(self.hwnd, 0)
            return
        if not self.drag:
            self._apply()
        if not visible:
            user32.ShowWindow(self.hwnd, 8)
        # 层级：永远紧贴在手机画面下面（插到它后面）；它置顶时边框也跟着置顶
        if user32.GetWindow(self.screen, 2) != self.hwnd:  # GW_HWNDNEXT
            user32.SetWindowPos(self.hwnd, self.screen, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_TIMER:
            try:
                self._follow()
            except Exception:
                pass
            return 0
        if msg == win32con.WM_MOUSEACTIVATE:
            return 3  # MA_NOACTIVATE
        if msg == win32con.WM_LBUTTONDOWN:
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            l, t, _, _ = win32gui.GetWindowRect(self.screen)
            fl, ft, _, _ = win32gui.GetWindowRect(hwnd)
            self.drag = (pt.x, pt.y, l, t, fl, ft)
            user32.SetCapture(hwnd)
            return 0
        if msg == win32con.WM_MOUSEMOVE and self.drag:
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            x0, y0, l, t, fl, ft = self.drag
            dx, dy = pt.x - x0, pt.y - y0
            flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE|NOZORDER|NOACTIVATE
            user32.SetWindowPos(self.screen, 0, l + dx, t + dy, 0, 0, flags)
            user32.SetWindowPos(hwnd, 0, fl + dx, ft + dy, 0, 0, flags)
            return 0
        if msg == win32con.WM_LBUTTONUP:
            if self.drag:
                self.drag = None
                user32.ReleaseCapture()
                self.rect = None
                self._apply()
                _activate(self.screen)
            return 0
        if msg == win32con.WM_SETCURSOR:
            win32gui.SetCursor(win32gui.LoadCursor(0, win32con.IDC_SIZEALL))
            return 1
        if msg == WM_REPAINT:
            self.rect = None
            self._apply()
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _loop(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = f"MyAIBotPhoneFrame{id(self)}"
        wc.lpfnWndProc = self._proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        ex = win32con.WS_EX_LAYERED | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
        self.hwnd = win32gui.CreateWindowEx(ex, wc.lpszClassName, "手机外框", win32con.WS_POPUP,
                                            0, 0, 1, 1, 0, 0, wc.hInstance, None)
        try:
            self._apply()
        except Exception as e:
            print("手机外框出错：", e)
        user32.ShowWindow(self.hwnd, 8)
        self._follow()
        user32.SetTimer(self.hwnd, 1, 30, None)
        win32gui.PumpMessages()

    def refresh(self) -> None:
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, WM_REPAINT, 0, 0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass


def _activate(hwnd: int) -> None:
    import win32api
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
