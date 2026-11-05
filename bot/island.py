"""灵动岛：屏幕顶部正中的一颗液态玻璃胶囊（像 iPhone 的灵动岛 + macOS 的 Liquid Glass）。

- 平时只是一条细细的玻璃短线；Bot 干活、放歌时变成小胶囊；鼠标停上去展开，带弹簧动效。
- 玻璃是自己画的：截下胶囊后面的屏幕（窗口设了 WDA_EXCLUDEFROMCAPTURE，截不到自己），
  模糊 + 提饱和度 + 边缘折射 + 高光描边，再盖一层墨黑 / 白色的薄色。
  截屏一次十几毫秒，所以背景大约一秒刷新一次，动画时复用最近一次的背景。
- 分层窗口，大小跟着胶囊每帧变化；鼠标不在胶囊上时窗口是「点击穿透」的，不挡下面的标签页。
- 内容：Bot 正在做什么（可停止）、做完 / 出错、等你确认；系统正在播放的音乐（听音、Spotify、浏览器……
  走 Windows 的媒体控制接口，可切歌）；空闲时展开是时间和快捷入口（问 Bot、搜索文件、手机投屏）。
- 跑在自己的线程里，全屏看视频、玩游戏时自动隐藏。
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import math
import threading
import time

import numpy as np
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import dock
from .dock import BITMAPINFOHEADER, gdi32, user32
from .phonebar import _blit, _dark

gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
ICON_FONTS = [r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf"]

ORANGE = (255, 149, 0)      # 网站的「今日燃烧色」
GREEN = (52, 199, 89)
RED = (255, 69, 58)

# 逻辑像素（乘 DPI 缩放）
TOP = 6            # 胶囊离屏幕顶部
MARGIN = 14        # 胶囊四周留给阴影的位置
MAX_W, MAX_H = 384, 132

LABELS = {
    "screenshot": "截图", "click": "点击", "type_text": "输入文字", "press_keys": "按键", "scroll": "滚动",
    "drag": "拖动", "move_mouse": "移动鼠标", "open": "打开软件", "list_windows": "查看窗口",
    "focus_window": "切换窗口", "inspect_window": "读取界面", "click_element": "点击控件",
    "get_clipboard": "读剪贴板", "set_clipboard": "写剪贴板", "wait": "等待", "list_apps": "查找软件",
    "list_processes": "查看进程", "screen_info": "屏幕信息", "find_files": "找文件", "PowerShell": "运行命令",
    "Bash": "运行命令", "Read": "读文件", "Write": "写文件", "Edit": "改文件", "Glob": "找文件",
    "Grep": "搜索内容", "WebSearch": "网页搜索", "WebFetch": "读网页", "TodoWrite": "列计划",
    "Agent": "子任务", "Task": "子任务", "ToolSearch": "加载工具", "Skill": "用技能",
    "phone_status": "手机状态", "phone_screenshot": "手机截图", "phone_inspect": "读取手机界面",
    "phone_tap": "点手机", "phone_tap_element": "点手机控件", "phone_swipe": "手机滑动",
    "phone_type_text": "手机打字", "phone_press_key": "手机按键", "phone_open_app": "打开手机 App",
    "phone_list_apps": "查手机 App", "phone_mirror": "手机投屏", "phone_shell": "手机命令",
}


def tool_label(name: str) -> str:
    short = name
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        short = parts[2] if len(parts) == 3 else name
        if parts[1] == "phone" and not short.startswith("phone_"):
            short = "phone_" + short
    return LABELS.get(short, short)


def _font(paths, size):
    for p in paths if isinstance(paths, list) else [paths]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ====================================================================== 正在播放（Windows 媒体控制）
class Media:
    """读系统「正在播放」（SMTC）：凡是接入了 Windows 媒体控制的播放器都行。"""

    def __init__(self, on_change):
        self.on_change = on_change
        self.info: dict | None = None
        self._art_key = None
        self._loop = asyncio.new_event_loop()
        self._session = None
        self._kick = None
        threading.Thread(target=self._run, daemon=True, name="island-media").start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            print("灵动岛：读取正在播放失败：", e)

    async def _main(self) -> None:
        from winrt.windows.media.control import \
            GlobalSystemMediaTransportControlsSessionManager as Manager
        mgr = await Manager.request_async()
        self._kick = asyncio.Event()
        while True:
            try:
                await self._poll(mgr)
            except Exception as e:
                print("灵动岛：正在播放出错：", e)
            try:
                await asyncio.wait_for(self._kick.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    async def _poll(self, mgr) -> None:
        sessions = list(mgr.get_sessions())
        cur = mgr.get_current_session()
        pick = None
        for s in sessions:  # 有正在播的就用它；否则用系统认为的「当前」
            if s.get_playback_info().playback_status == 4:
                pick = s
                break
        pick = pick or cur
        self._session = pick
        if pick is None:
            if self.info is not None:
                self.info = None
                self.on_change()
            return
        props = await pick.try_get_media_properties_async()
        status = pick.get_playback_info().playback_status
        tl = pick.get_timeline_properties()
        title = (props.title or "").strip()
        if not title:
            if self.info is not None:
                self.info = None
                self.on_change()
            return
        pos = tl.position.total_seconds()
        dur = (tl.end_time - tl.start_time).total_seconds()
        try:  # 时间线是「上次更新」时的位置，正在播就往后推
            if status == 4 and tl.last_updated_time:
                pos += (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
        except Exception:
            pass
        key = (pick.source_app_user_model_id, title, props.artist)
        art = self.info.get("art") if self.info and self._art_key == key else None
        if self._art_key != key:
            self._art_key = key
            art = await self._read_art(props)
        info = {"title": title, "artist": (props.artist or "").strip(), "playing": status == 4,
                "pos": max(0.0, min(pos, dur or pos)), "dur": dur, "at": time.perf_counter(),
                "app": pick.source_app_user_model_id, "art": art}
        old = self.info
        self.info = info
        if not old or old.get("art") is not art or any(old.get(k) != info[k] for k in ("title", "artist", "playing")) \
                or abs(old.get("pos", 0) + (info["at"] - old["at"] if old.get("playing") else 0) - info["pos"]) > 2:
            self.on_change()

    async def _read_art(self, props):
        if props.thumbnail is None:
            return None
        try:
            import io
            from winrt.windows.storage.streams import DataReader
            stream = await props.thumbnail.open_read_async()
            size = stream.size
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            buf = reader.read_buffer(size)
            data = bytes(memoryview(buf))
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:
            print("灵动岛：读取封面失败：", e)
            return None

    def control(self, action: str) -> None:
        s = self._session
        if s is None:
            return

        async def run():
            try:
                if action == "toggle":
                    await s.try_toggle_play_pause_async()
                elif action == "next":
                    await s.try_skip_next_async()
                elif action == "prev":
                    await s.try_skip_previous_async()
            except Exception as e:
                print("灵动岛：控制播放失败：", e)
            await asyncio.sleep(0.15)
            if self._kick:
                self._kick.set()
        asyncio.run_coroutine_threadsafe(run(), self._loop)

    def position(self) -> float:
        i = self.info
        if not i:
            return 0
        p = i["pos"] + (time.perf_counter() - i["at"] if i["playing"] else 0)
        return min(p, i["dur"]) if i["dur"] else p


# ====================================================================== 弹簧
class Spring:
    def __init__(self, v: float, k: float = 340, c: float = 27):
        self.x = self.target = v
        self.v = 0.0
        self.k, self.c = k, c

    def step(self, dt_: float) -> bool:
        if self.x == self.target and self.v == 0:
            return False
        a = self.k * (self.target - self.x) - self.c * self.v
        self.v += a * dt_
        self.x += self.v * dt_
        if abs(self.target - self.x) < 0.25 and abs(self.v) < 4:
            self.x, self.v = self.target, 0.0
        return True


# ====================================================================== 灵动岛
class Island:
    def __init__(self, actions: dict, panel_visible=lambda: False):
        self.actions = actions                # panel / search / phone / stop
        self.panel_visible = panel_visible
        s = self.s = user32.GetDpiForSystem() / 96
        self.dark = _dark()
        self.hwnd = 0
        self.enabled = True
        self.suspended = False                # Bot 要点的地方被灵动岛挡住：任务期间先藏起来
        self.fullscreen = False
        self.shown = False
        # 状态（别的线程写，岛的线程读）
        self.work: dict | None = None         # {"since", "label", "summary"}
        self.perm: dict | None = None
        self.flash: dict | None = None        # {"ok", "text", "until"}
        self.media = Media(self._dirty)
        # 动画
        w, h, r = self._layout_size("line")
        self.sw, self.sh, self.sr = Spring(w * s), Spring(h * s), Spring(r * s)
        self.mode = "line"
        self.mode_t = 0.0
        self.hover = False
        self.expanded = False
        self.hover_t = 0.0
        self.leave_t = 0.0
        self.pressed = None
        self.buttons: list[tuple[tuple[int, int, int, int], str]] = []
        self.hot = None
        self.click_through = True
        self.dirty = True
        self.last_frame = 0.0
        self.last_tick = time.perf_counter()
        self.last_anim = 0.0
        # 背景
        self.cap_ver = 0
        self.cap_t = 0.0
        self.cap_fg = None
        self.backdrop: np.ndarray | None = None
        self._glass_cache = None
        self._mask_cache: dict = {}
        self._art_cache = None
        self._cw = None
        self.interval = 15
        # 字体
        self.f_title = _font([FONT_BOLD, FONT], round(14 * s))
        self.f_text = _font(FONT, round(13 * s))
        self.f_small = _font(FONT, round(11 * s))
        self.f_tiny = _font(FONT, round(10 * s))
        self.f_clock = _font([FONT_BOLD, FONT], round(26 * s))
        self.f_icon = _font(ICON_FONTS, round(16 * s))
        self.f_icon_big = _font(ICON_FONTS, round(20 * s))
        self.f_icon_small = _font(ICON_FONTS, round(12 * s))
        self._geom()
        threading.Thread(target=self._loop, daemon=True, name="island").start()

    # ------------------------------------------------------------ 外部调用（任意线程）
    def _dirty(self) -> None:
        self.dirty = True

    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        now = time.perf_counter()
        if t == "status":
            if ev["state"] == "thinking":
                if not self.work:
                    self.work = {"since": now, "label": "思考中", "summary": ""}
                self.flash = None
            elif ev["state"] == "idle":
                self.work = None
                self.perm = None
                self.suspended = False
        elif t == "tool_use" and self.work is not None:
            self.work["label"] = tool_label(ev.get("name", ""))
            self.work["summary"] = " ".join(str(ev.get("summary") or "").split())
            self.perm = None
        elif t == "text_delta" and self.work is not None:
            if self.work["label"] != "正在回复":
                self.work["label"], self.work["summary"] = "正在回复", ""
        elif t == "permission":
            self.perm = ev
        elif t == "tool_result":
            self.perm = None
        elif t == "result":
            self.perm = None
            if self.work and not self.panel_visible():
                secs = ev.get("duration") or (now - self.work["since"])
                if ev.get("ok"):
                    self.flash = {"ok": True, "text": f"做完了 · 用时 {_fmt(secs)}", "until": now + 4}
                else:
                    self.flash = {"ok": False, "text": "出错了，点开看看", "until": now + 6}
        elif t == "error" and not self.panel_visible():
            self.flash = {"ok": False, "text": str(ev.get("message", "出错了"))[:40], "until": now + 6}
        else:
            return
        self.dirty = True

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self.dirty = True

    def contains(self, x: int, y: int) -> bool:
        if not self.shown:
            return False
        l, t, r, b = self._pill_rect()
        return l - 4 <= x <= r + 4 and t - 4 <= y <= b + 4

    def avoid(self, x: int, y: int) -> None:
        """Bot 要点的位置在灵动岛上：这次任务期间先藏起来（任务结束自动回来）。"""
        if self.contains(x, y):
            self.suspended = True
            self.dirty = True
            time.sleep(0.12)

    # ------------------------------------------------------------ 几何
    def _geom(self) -> None:
        s = self.s
        left, top, right, _ = dock.work_area()
        self.cx = (left + right) / 2
        self.top = top
        self.cap_w = round((MAX_W + MARGIN * 2) * s)
        self.cap_h = round((MAX_H + TOP + MARGIN) * s)
        self.cap_x = round(self.cx - self.cap_w / 2)
        self.cap_y = top

    def _pill_rect(self) -> tuple[int, int, int, int]:
        w, h = self.sw.x, self.sh.x
        t = self.top + TOP * self.s
        return round(self.cx - w / 2), round(t), round(self.cx + w / 2), round(t + h)

    def _layout_size(self, mode: str) -> tuple[float, float, float]:
        """各个样子的（宽, 高, 圆角），逻辑像素。"""
        if mode == "line":
            return 112, 7, 3.5
        if mode == "compact":
            return self._compact_w(), 34, 17
        return {"idle_x": (340, 82, 30), "bot_x": (372, 92, 30), "music_x": (372, 128, 32),
                "alert": (340, 54, 27), "flash": (300, 44, 22)}[mode]

    def _compact_w(self) -> float:
        m = self.media.info
        key = (self.work and self.work["label"], m and m["title"], m and m["playing"])
        if self._cw and self._cw[0] == key:
            return self._cw[1]
        w = self._compact_w_calc()
        self._cw = (key, w)
        return w

    def _compact_w_calc(self) -> float:
        s = self.s
        d = ImageDraw.Draw(Image.new("L", (1, 1)))
        m = self.media.info
        if self.work:
            tw = min(d.textlength(self.work["label"], font=self.f_text) / s, 170)
            return max(150, 14 + 18 + 9 + tw + (36 if m and m["playing"] else 16))
        if m:
            tw = min(d.textlength(m["title"], font=self.f_text) / s, 150)
            return max(150, 6 + 24 + 9 + tw + 12 + 20 + 14)
        return 150

    def _pick_mode(self, now: float) -> str:
        if self.flash and now > self.flash["until"]:
            self.flash = None
        m = self.media.info
        if self.perm:
            return "alert"
        if self.flash:
            return "flash"
        if self.expanded:
            if self.work:
                return "bot_x"
            if m:
                return "music_x"
            return "idle_x"
        if self.work or (m and m["playing"]):
            return "compact"
        return "line"

    # ------------------------------------------------------------ 背景截图
    def _capture(self) -> None:
        w, h = self.cap_w, self.cap_h
        sdc = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(sdc)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, sdc, self.cap_x, self.cap_y, 0x00CC0020)  # SRCCOPY
        raw = ctypes.string_at(bits, w * h * 4)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, sdc)
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).convert("RGB")
        # 毛玻璃：缩小 → 模糊 → 提饱和度 → 放回去（比直接大半径模糊快得多）
        k = 3
        small = img.resize((max(1, w // k), max(1, h // k)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(2.2 * self.s))
        small = ImageEnhance.Color(small).enhance(1.6)
        blurred = small.resize((w, h), Image.BILINEAR)
        arr = np.asarray(blurred, dtype=np.float32)
        if self.backdrop is not None and self.backdrop.shape == arr.shape \
                and float(np.abs(self.backdrop[::8, ::8] - arr[::8, ::8]).mean()) < 0.6:
            return  # 背景没变：不用重画玻璃
        self.backdrop = arr
        self.cap_ver += 1
        self.dirty = True

    # ------------------------------------------------------------ 玻璃
    def _glass(self, box: tuple[int, int, int, int]):
        """按当前胶囊形状算玻璃：返回（RGBA 图, 胶囊内部的蒙版）。box 是要画的区域（屏幕坐标）。"""
        s = self.s
        w, h, r = self.sw.x, self.sh.x, min(self.sr.x, self.sh.x / 2)
        key = (box, round(w * 2), round(h * 2), round(r * 2), self.cap_ver, self.dark)
        if self._glass_cache and self._glass_cache[0] == key:
            return self._glass_cache[1], self._glass_cache[2]
        bl, bt, br, bb = box
        X = np.arange(bl, br, dtype=np.float32)[None, :] + 0.5
        Y = np.arange(bt, bb, dtype=np.float32)[:, None] + 0.5
        cx, cy = self.cx, self.top + TOP * s + h / 2
        px, py = X - cx, Y - cy
        qx = np.abs(px) - (w / 2 - r)
        qy = np.abs(py) - (h / 2 - r)
        mx, my = np.maximum(qx, 0), np.maximum(qy, 0)
        outer = np.sqrt(mx * mx + my * my)
        sdf = outer + np.minimum(np.maximum(qx, qy), 0) - r
        # 外法线方向
        corner = (qx > 0) & (qy > 0)
        safe = np.where(outer > 1e-3, outer, 1)
        nx = np.where(corner, mx / safe, (qx >= qy).astype(np.float32)) * np.sign(px)
        ny = np.where(corner, my / safe, (qy > qx).astype(np.float32)) * np.sign(py)
        nx, ny = np.broadcast_to(nx, sdf.shape), np.broadcast_to(ny, sdf.shape)
        inside = np.clip(0.5 - sdf, 0, 1)
        # 折射：越靠边缘，取样点越往外偏（边缘像透镜一样把外面的景象弯进来）
        band = max(2.0, min(h / 2, 16 * s))
        strength = min(11 * s, h * 0.45)
        bend = strength * (1 - np.clip(-sdf / band, 0, 1)) ** 2
        sx = np.clip((X + nx * bend - self.cap_x).astype(np.int32), 0, self.cap_w - 1)
        sy = np.clip((Y + ny * bend - self.cap_y).astype(np.int32), 0, self.cap_h - 1)
        if self.backdrop is not None:
            col = self.backdrop[sy, sx]
        else:
            col = np.full(sdf.shape + (3,), 128, np.float32)
        # 薄色：深色是墨黑玻璃，浅色是白玻璃
        if self.dark:
            tint, amt = np.array([14, 14, 16], np.float32), 0.6
        else:
            tint, amt = np.array([250, 250, 252], np.float32), 0.62
        col = col * (1 - amt) + tint * amt
        # 顶部一层很淡的光
        sheen = np.clip(1 - (Y - (cy - h / 2)) / max(h, 1), 0, 1) ** 2 * (16 if self.dark else 10)
        col = col + sheen[..., None]
        # 高光描边：上边亮、下边一点反光
        rim = np.exp(-((sdf + 1.1 * s) / (0.9 * s)) ** 2)
        dl = nx * -0.38 + ny * -0.925
        spec = rim * (np.clip(dl, 0, 1) * 0.95 + np.clip(-dl, 0, 1) * 0.5)
        glow = np.clip(1 + sdf / (6 * s), 0, 1) * (sdf < 0) * (0.10 if self.dark else 0.06)
        light = np.clip(spec + glow, 0, 1)[..., None]
        col = col + (255 - col) * light
        if not self.dark:  # 白底上也看得出边
            line = np.exp(-((sdf + 0.2 * s) / (0.7 * s)) ** 2)
            col = col * (1 - 0.13 * line[..., None])
        col = np.clip(col, 0, 255)
        # 阴影（只在胶囊外面）
        thin = h < 12 * s
        sh_amt = 0 if thin else (0.32 if self.dark else 0.2)
        shadow = sh_amt * np.exp(-np.maximum(sdf, 0) / (4.2 * s)) * (1 - inside)
        shadow = np.where(shadow < 0.02, 0, shadow)
        alpha = inside + shadow
        rgb = col * (inside / np.maximum(alpha, 1e-4))[..., None]
        out = np.dstack([rgb, alpha[..., None] * 255]).astype(np.uint8)
        img = Image.fromarray(out, "RGBA")
        mask = Image.fromarray((inside * 255).astype(np.uint8), "L")
        self._glass_cache = (key, img, mask)
        return img, mask

    # ------------------------------------------------------------ 画内容
    def _mask(self, kind: str, w: int, h: int, r: float = 0, width: int = 0) -> Image.Image:
        """抗锯齿的形状蒙版（4 倍大小画再缩小），按尺寸缓存。"""
        key = (kind, w, h, round(r, 1), width)
        m = self._mask_cache.get(key)
        if m is None:
            k = 4
            big = Image.new("L", (w * k, h * k), 0)
            d = ImageDraw.Draw(big)
            if kind == "ellipse":
                d.ellipse((0, 0, w * k - 1, h * k - 1), fill=255)
            elif kind == "ring":
                d.ellipse((0, 0, w * k - 1, h * k - 1), outline=255, width=width * k)
            else:
                d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=r * k, fill=255)
            m = big.resize((w, h), Image.LANCZOS)
            if len(self._mask_cache) > 200:
                self._mask_cache.clear()
            self._mask_cache[key] = m
        return m

    def _fill(self, img, kind, x, y, w, h, color, opacity=1.0, r=0) -> None:
        x, y, w, h = round(x), round(y), max(1, round(w)), max(1, round(h))
        m = self._mask(kind, w, h, r)
        if opacity < 1:
            m = m.point(lambda v: round(v * opacity))
        img.paste(Image.new("RGBA", (w, h), color + (255,)), (x, y), m)

    def _fit(self, d, text: str, font, maxw: float) -> str:
        if d.textlength(text, font=font) <= maxw:
            return text
        while text and d.textlength(text + "…", font=font) > maxw:
            text = text[:-1]
        return text + "…"

    def _spinner(self, img, cx, cy, r, now) -> None:
        s = self.s
        size = round(r * 2)
        k = 4
        big = Image.new("L", (size * k, size * k), 0)
        d = ImageDraw.Draw(big)
        width = max(1, round(2.2 * s)) * k
        d.ellipse((0, 0, size * k - 1, size * k - 1), outline=60, width=width)
        a = (now * 300) % 360
        d.arc((0, 0, size * k - 1, size * k - 1), a, a + 110, fill=255, width=width)
        m = big.resize((size, size), Image.LANCZOS)
        img.paste(Image.new("RGBA", (size, size), ORANGE + (255,)), (round(cx - r), round(cy - r)), m)

    def _bars(self, img, x, cy, h, now, playing) -> None:
        """跳动的音乐柱。"""
        s = self.s
        bw, gap = max(2, round(2.6 * s)), round(2.4 * s)
        for i in range(4):
            if playing:
                v = 0.35 + 0.65 * abs(math.sin(now * (3.1 + i * 0.9) + i * 1.7))
            else:
                v = 0.25
            bh = max(bw, round(h * v))
            self._fill(img, "rrect", x + i * (bw + gap), cy - bh / 2, bw, bh, ORANGE, r=bw / 2)

    def _art(self, img, x, y, size, r) -> None:
        m = self.media.info
        size = round(size)
        art = m.get("art") if m else None
        if art is not None:
            if not self._art_cache or self._art_cache[0] != (id(art), size):
                w, h = art.size
                side = min(w, h)
                sq = art.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
                self._art_cache = ((id(art), size), sq.resize((size, size), Image.LANCZOS).convert("RGBA"))
            img.paste(self._art_cache[1], (round(x), round(y)), self._mask("rrect", size, size, r))
        else:  # 没有封面：橙色方块 + 音符
            self._fill(img, "rrect", x, y, size, size, ORANGE, r=r)
            d = ImageDraw.Draw(img)
            f = self.f_icon_big if size > 30 * self.s else self.f_icon_small
            d.text((x + size / 2, y + size / 2), "\uec4f", font=f, fill=(255, 255, 255), anchor="mm")

    def _button(self, img, cx, cy, r, glyph, action, font=None, filled=False) -> None:
        s = self.s
        ink, _, faint = self._inks()
        hot = self.hot == action
        if filled:
            self._fill(img, "ellipse", cx - r, cy - r, r * 2, r * 2, ORANGE)
        elif hot or self.pressed == action:
            self._fill(img, "ellipse", cx - r, cy - r, r * 2, r * 2, faint, 0.28 if self.pressed == action else 0.16)
        ImageDraw.Draw(img).text((cx, cy), glyph, font=font or self.f_icon, fill=(255, 255, 255) if filled else ink,
                                 anchor="mm")
        self.buttons.append(((round(cx - r - 2 * s), round(cy - r - 2 * s), round(cx + r + 2 * s),
                              round(cy + r + 2 * s)), action))

    def _inks(self):
        if self.dark:
            return (245, 245, 247), (160, 160, 168), (255, 255, 255)
        return (29, 29, 31), (110, 110, 115), (0, 0, 0)

    def _content(self, img, ox, oy, mode, now) -> None:
        """在胶囊里画内容。(ox, oy) 是目标大小的胶囊左上角在 img 里的位置。"""
        s = self.s
        d = ImageDraw.Draw(img)
        ink, sub, faint = self._inks()
        W, H, _ = self._layout_size(mode)
        W, H = W * s, H * s
        m = self.media.info
        if mode == "compact":
            cy = oy + H / 2
            if self.work:
                self._spinner(img, ox + 14 * s + 9 * s, cy, 9 * s, now)
                label = self._fit(d, self.work["label"], self.f_text, 170 * s)
                d.text((ox + 41 * s, cy), label, font=self.f_text, fill=ink, anchor="lm")
                if m and m["playing"]:
                    self._bars(img, ox + W - 30 * s, cy, 14 * s, now, True)
            elif m:
                self._art(img, ox + 6 * s, cy - 12 * s, 24 * s, 7 * s)
                title = self._fit(d, m["title"], self.f_text, 150 * s)
                d.text((ox + 39 * s, cy), title, font=self.f_text, fill=ink, anchor="lm")
                self._bars(img, ox + W - 32 * s, cy, 14 * s, now, m["playing"])
        elif mode == "idle_x":
            t = dt.datetime.now()
            d.text((ox + 24 * s, oy + 36 * s), t.strftime("%H:%M"), font=self.f_clock, fill=ink, anchor="ls")
            wk = "一二三四五六日"[t.weekday()]
            d.text((ox + 25 * s, oy + 58 * s), f"{t.month}月{t.day}日 周{wk}", font=self.f_small, fill=sub, anchor="ls")
            for i, (glyph, label, action) in enumerate([("\ue99a", "问 Bot", "panel"), ("\ue721", "搜文件", "search"),
                                                        ("\ue8ea", "投屏", "phone")]):
                bx = ox + W - (42 + (2 - i) * 62) * s
                self._button(img, bx, oy + 33 * s, 19 * s, glyph, action)
                d.text((bx, oy + 66 * s), label, font=self.f_tiny, fill=sub, anchor="mm")
        elif mode == "bot_x":
            w = self.work or {"since": now, "label": "", "summary": ""}
            self._spinner(img, ox + 30 * s, oy + 30 * s, 11 * s, now)
            d.text((ox + 52 * s, oy + 30 * s), "Bot 正在工作", font=self.f_title, fill=ink, anchor="lm")
            el = _fmt(now - w["since"])
            tw = d.textlength("Bot 正在工作", font=self.f_title)
            d.text((ox + 52 * s + tw + 10 * s, oy + 30 * s), el, font=self.f_small, fill=sub, anchor="lm")
            step = w["label"] + ("：" + w["summary"] if w["summary"] else "")
            d.text((ox + 20 * s, oy + 66 * s), self._fit(d, step, self.f_text, W - 84 * s), font=self.f_text,
                   fill=sub, anchor="lm")
            self._button(img, ox + W - 34 * s, oy + 46 * s, 17 * s, "\ue71a", "stop", font=self.f_icon_small,
                         filled=True)
        elif mode == "music_x" and m:
            art = 56 * s
            self._art(img, ox + 16 * s, oy + 16 * s, art, 12 * s)
            tx = ox + 16 * s + art + 12 * s
            maxw = W - (tx - ox) - 58 * s
            d.text((tx, oy + 34 * s), self._fit(d, m["title"], self.f_title, maxw), font=self.f_title, fill=ink,
                   anchor="ls")
            d.text((tx, oy + 56 * s), self._fit(d, m["artist"] or "未知歌手", self.f_text, maxw), font=self.f_text,
                   fill=sub, anchor="ls")
            self._button(img, ox + W - 32 * s, oy + 36 * s, 16 * s, "\ue99a", "panel", font=self.f_icon_small)
            # 进度条
            pos, dur = self.media.position(), m["dur"]
            by = oy + 88 * s
            bx0, bx1 = ox + 58 * s, ox + W - 58 * s
            self._fill(img, "rrect", bx0, by - 2 * s, bx1 - bx0, 4 * s, faint, 0.18, r=2 * s)
            if dur > 0:
                pw = max(4 * s, (bx1 - bx0) * min(1, pos / dur))
                self._fill(img, "rrect", bx0, by - 2 * s, pw, 4 * s, ORANGE, r=2 * s)
                d.text((bx0 - 10 * s, by), _fmt(pos), font=self.f_tiny, fill=sub, anchor="rm")
                d.text((bx1 + 10 * s, by), "-" + _fmt(dur - pos), font=self.f_tiny, fill=sub, anchor="lm")
            cy = oy + 112 * s
            cx = ox + W / 2
            self._button(img, cx - 54 * s, cy, 15 * s, "\ue892", "prev")
            self._button(img, cx, cy, 17 * s, "\ue769" if m["playing"] else "\ue768", "toggle", font=self.f_icon_big)
            self._button(img, cx + 54 * s, cy, 15 * s, "\ue893", "next")
        elif mode == "alert":
            p = self.perm or {}
            cy = oy + H / 2
            self._fill(img, "ellipse", ox + 12 * s, cy - 15 * s, 30 * s, 30 * s, ORANGE)
            d.text((ox + 27 * s, cy), "\ue7ba", font=self.f_icon_small, fill=(255, 255, 255), anchor="mm")
            d.text((ox + 52 * s, cy - 8 * s), "Bot 等你确认", font=self.f_title, fill=ink, anchor="lm")
            what = " ".join(str(p.get("reason") or p.get("short") or "").split())
            d.text((ox + 52 * s, cy + 11 * s), self._fit(d, str(what), self.f_small, W - 120 * s), font=self.f_small,
                   fill=sub, anchor="lm")
            d.text((ox + W - 18 * s, cy), "点开 ›", font=self.f_small, fill=ORANGE, anchor="rm")
        elif mode == "flash":
            f = self.flash or {"ok": True, "text": ""}
            cy = oy + H / 2
            color = GREEN if f["ok"] else RED
            self._fill(img, "ellipse", ox + 10 * s, cy - 13 * s, 26 * s, 26 * s, color)
            d.text((ox + 23 * s, cy), "\ue73e" if f["ok"] else "\ue711", font=self.f_icon_small, fill=(255, 255, 255),
                   anchor="mm")
            d.text((ox + 46 * s, cy), self._fit(d, f["text"], self.f_text, W - 62 * s), font=self.f_text, fill=ink,
                   anchor="lm")

    # ------------------------------------------------------------ 一帧
    def _frame(self, now: float) -> None:
        s = self.s
        self.buttons = []
        pl, pt, pr, pb = self._pill_rect()
        M = round(MARGIN * s)
        box = (max(self.cap_x, pl - M), self.cap_y, min(self.cap_x + self.cap_w, pr + M),
               min(self.cap_y + self.cap_h, pb + M))
        glass, inside = self._glass(box)
        img = glass.copy()
        fade = min(1.0, max(0.0, (now - self.mode_t - 0.06) / 0.2))
        if self.mode != "line" and fade > 0:
            W, _, _ = self._layout_size(self.mode)
            ox = self.cx - W * s / 2 - box[0]
            oy = self.top + TOP * s - box[1]
            layer = glass.copy()
            self._content(layer, ox, oy, self.mode, now)
            if fade < 1:
                layer = Image.blend(glass, layer, fade)
            img = Image.composite(layer, glass, inside)
            img.putalpha(glass.getchannel("A"))
            self.buttons = [((l + box[0], t + box[1], r + box[0], b + box[1]), a) for (l, t, r, b), a in self.buttons]
        r, g, b, a = img.convert("RGBa").split()  # 预乘 alpha（PIL 在 C 里做，快）
        _blit(self.hwnd, Image.merge("RGBA", (b, g, r, a)), (box[0], box[1]))
        self.last_frame = now

    def _animated(self, now: float) -> bool:
        """内容里有没有在动的东西（转圈、音乐柱、进度条、时钟）。"""
        m = self.media.info
        return self.mode in ("bot_x", "music_x") or (self.mode == "compact" and (self.work or (m and m["playing"])))

    # ------------------------------------------------------------ 定时器
    def _tick(self) -> None:
        now = time.perf_counter()
        dt_ = min(0.05, now - self.last_tick)
        self.last_tick = now
        # 低频检查：全屏、深浅色、DPI / 工作区、前台窗口变化（变了就重新截背景）
        if now - getattr(self, "_slow_t", 0) > 0.5:
            self._slow_t = now
            fs = dock.fullscreen_app()
            if fs != self.fullscreen:
                self.fullscreen, self.dirty = fs, True
            dark = _dark()
            if dark != self.dark:
                self.dark, self.dirty = dark, True
                self._glass_cache = None
            left, top, right, _ = dock.work_area()
            if (left + right) / 2 != self.cx or top != self.top:
                self._geom()
                self.backdrop = None
                self.cap_t = 0
            fg = user32.GetForegroundWindow()
            try:
                fgr = (fg, win32gui.GetWindowRect(fg)) if fg else None
            except Exception:
                fgr = None
            if fgr != self.cap_fg:
                self.cap_fg = fgr
                self.cap_t = 0
        want = self.enabled and not self.suspended and not (self.fullscreen and not self.perm)
        if not want:
            if self.shown:
                user32.ShowWindow(self.hwnd, 0)
                self.shown = False
            return
        # 鼠标：停在岛上一小会儿就展开，离开一会儿收回
        x, y = dock.cursor()
        pl, pt, pr, pb = self._pill_rect()
        if self.mode == "line":
            over = pl - 24 * self.s <= x <= pr + 24 * self.s and self.top <= y <= pb + 6 * self.s
        else:
            over = pl - 2 <= x <= pr + 2 and self.top <= y <= pb + 2
        if over and not self.hover:
            self.hover, self.hover_t = True, now
        elif not over and self.hover:
            self.hover, self.leave_t = False, now
        if self.hover and not self.expanded and now - self.hover_t > 0.16 and not dock.mouse_down():
            self.expanded, self.dirty = True, True
        if not self.hover and self.expanded and now - self.leave_t > 0.45 and self.pressed is None:
            self.expanded, self.dirty = False, True
        # 鼠标在胶囊上才接收点击，其余时候整个窗口点击穿透
        through = not (over and self.mode != "line")
        if through != self.click_through:
            self.click_through = through
            ex = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
            ex = ex | win32con.WS_EX_TRANSPARENT if through else ex & ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, ex)
        hot = None
        if not through:
            for (l, t, r, b), a in self.buttons:
                if l <= x <= r and t <= y <= b:
                    hot = a
                    break
        if hot != self.hot:
            self.hot, self.dirty = hot, True
        # 目标形状
        mode = self._pick_mode(now)
        if mode != self.mode:
            self.mode, self.mode_t, self.dirty = mode, now, True
            self.cap_t = min(self.cap_t, now - 0.9)  # 展开的时候顺便刷新一下背景
        w, h, r = self._layout_size(mode)
        self.sw.target, self.sh.target, self.sr.target = w * self.s, h * self.s, r * self.s
        moving = self.sw.step(dt_) | self.sh.step(dt_) | self.sr.step(dt_)
        # 背景：形状静止时大约一秒刷一次（小短线时更慢），动画期间不截
        gap = 3.0 if mode == "line" else 1.5
        if not moving and now - self.cap_t > gap:
            self.cap_t = now
            try:
                self._capture()
            except Exception as e:
                print("灵动岛：截取背景失败：", e)
        fade_in = now - self.mode_t < 0.3
        anim = self._animated(now) and now - self.last_anim > 1 / 12
        if anim:
            self.last_anim = now
        if moving or fade_in or anim or self.dirty or now - self.last_frame > 1.0:
            self.dirty = False
            try:
                self._frame(now)
            except Exception as e:
                print("灵动岛：绘制出错：", e)
            if not self.shown:
                user32.ShowWindow(self.hwnd, 8)  # SW_SHOWNA
                self.shown = True
        # 定时器：形状在动 / 鼠标在附近时 15ms 一跳，平时 40ms（省电）
        near = abs(x - self.cx) < 260 * self.s and y < self.top + 170 * self.s
        interval = 15 if (moving or fade_in or self.hover or near) else 40
        if interval != self.interval:
            self.interval = interval
            user32.SetTimer(self.hwnd, 1, interval, None)
        if now - getattr(self, "_top_t", 0) > 2:  # 别的置顶窗口可能盖过来
            self._top_t = now
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

    # ------------------------------------------------------------ 窗口
    def _click(self, action: str | None) -> None:
        if action in ("prev", "toggle", "next"):
            self.media.control(action)
            return
        if action is None:  # 点在胶囊本身
            if self.mode in ("compact", "line"):
                self.expanded, self.dirty = True, True
                return
            if self.mode == "music_x":
                return
            action = "panel"
        if action == "panel":
            self.flash = None
        fn = self.actions.get(action)
        if fn:
            threading.Thread(target=fn, daemon=True).start()

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_TIMER:
            try:
                self._tick()
            except Exception as e:
                print("灵动岛出错：", e)
            return 0
        if msg == win32con.WM_MOUSEACTIVATE:
            return 3  # MA_NOACTIVATE：点灵动岛不抢键盘焦点
        if msg == win32con.WM_LBUTTONDOWN:
            self.pressed = self.hot or "__body"
            user32.SetCapture(hwnd)
            self.dirty = True
            return 0
        if msg == win32con.WM_LBUTTONUP:
            user32.ReleaseCapture()
            pressed, self.pressed = self.pressed, None
            self.dirty = True
            if pressed and (pressed == self.hot or (pressed == "__body" and self.hover and self.hot is None)):
                self._click(None if pressed == "__body" else pressed)
            return 0
        if msg == win32con.WM_RBUTTONUP:
            self._click("panel")
            return 0
        if msg == win32con.WM_SETCURSOR:
            win32gui.SetCursor(win32gui.LoadCursor(0, win32con.IDC_HAND if self.hot else win32con.IDC_ARROW))
            return 1
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _loop(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "MyAIBotIsland"
        wc.lpfnWndProc = self._proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOPMOST
              | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
        self.hwnd = win32gui.CreateWindowEx(ex, wc.lpszClassName, "灵动岛", win32con.WS_POPUP,
                                            0, 0, 1, 1, 0, 0, wc.hInstance, None)
        # 截屏时排除自己：玻璃要截的是它后面的东西
        user32.SetWindowDisplayAffinity(self.hwnd, 0x11)
        user32.SetTimer(self.hwnd, 1, 15, None)
        win32gui.PumpMessages()

    def close(self) -> None:
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
