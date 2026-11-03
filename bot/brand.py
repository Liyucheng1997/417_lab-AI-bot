"""Logo：个人网站 liyucheng.me 首页「时间与我」时钟的缩小版。

三道渐变弧（外：日 黄→橙→红，中：分 紫→粉，内：秒 青→蓝），弧头带发光小圆点，
中心是带彩虹环的白色玻璃球。颜色与网站 me.html 的 drawClock 完全一致。
界面里的 Logo 用 canvas 画同一个钟，并且按真实时间走（bot/ui/app.js 的 drawLogo）。
这里用 PIL 画静态版，给托盘、exe、安装包用。
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter

DAY = ["#ffcc00", "#ff9500", "#ff3b30"]
MIN = ["#af52de", "#ff2d55"]
SEC = ["#5ac8fa", "#007aff"]
RAINBOW = ["#5ac8fa", "#af52de", "#ff2d55", "#ff9500", "#ffcc00", "#34c759", "#5ac8fa"]
TRACK = (29, 29, 31, 20)  # rgba(29,29,31,.08)


def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _mix(stops: list[str], t: float) -> tuple[int, int, int, int]:
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    a, b, f = _rgb(stops[i]), _rgb(stops[i + 1]), t - i
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3)) + (255,)


def _arc(d: ImageDraw.ImageDraw, c: float, r: float, w: float, frac: float, stops: list[str]) -> tuple[float, float]:
    """从 12 点方向顺时针画一段渐变弧（圆头），返回弧头坐标。"""
    box = (c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2)
    steps = max(int(frac * 360 * 2), 2)
    for i in range(steps):
        a0 = -90 + 360 * frac * i / steps
        d.arc(box, a0, a0 + 360 * frac / steps + 0.6, fill=_mix(stops, i / steps), width=round(w))
    for t, ang in ((0.0, -90), (1.0, -90 + 360 * frac)):
        x, y = c + r * math.cos(math.radians(ang)), c + r * math.sin(math.radians(ang))
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=_mix(stops, t))
    ang = math.radians(-90 + 360 * frac)
    return c + r * math.cos(ang), c + r * math.sin(ang)


def _head(img: Image.Image, x: float, y: float, size: float, color: str) -> None:
    """弧头：白色光点 + 彩色发光 + 彩色芯（同网站的 head()）。"""
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((x - size, y - size, x + size, y + size), fill=_rgb(color) + (150,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.7)))
    d = ImageDraw.Draw(img)
    d.ellipse((x - size, y - size, x + size, y + size), fill=(255, 255, 255, 255))
    s = size * 0.55
    d.ellipse((x - s, y - s, x + s, y + s), fill=_rgb(color) + (255,))


def _orb(img: Image.Image, c: float, r: float, ring_w: float) -> None:
    """中心玻璃球：投影 + 偏左上的高光径向渐变 + 一圈彩虹环。"""
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((c - r, c - r + r * 0.12, c + r, c + r + r * 0.12), fill=(0, 0, 0, 60))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(r * 0.18)))
    d = ImageDraw.Draw(img)
    rr = r + ring_w * 0.9
    _arc(d, c, rr, ring_w, 0.999, RAINBOW)
    hx, hy = c - r * 0.35, c - r * 0.4  # 高光中心
    n = 60
    for i in range(n):  # 从外往里，圆心逐渐挪向高光点，颜色 #e8e8ee → #f4f4f7 → #fff
        t = i / (n - 1)
        rad = r * (1 - t) + r * 0.1 * t
        cx, cy = c + (hx - c) * t, c + (hy - c) * t
        col = _mix(["#e8e8ee", "#f4f4f7", "#ffffff"], min(1, t / 0.75))
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=col)


def _render(size: int) -> Image.Image:
    s = 2048  # 2 倍超采样
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.225, fill=(255, 255, 255, 255))
    c = s / 2
    if size <= 32:  # 托盘这种小尺寸：一道粗的日弧 + 玻璃球，太多圈会糊
        rings = [(s * 0.36, s * 0.115, 0.7, DAY, None)]
        orb_r, ring_w = s * 0.165, s * 0.05
    elif size <= 48:
        rings = [(s * 0.37, s * 0.075, 0.7, DAY, s * 0.05), (s * 0.255, s * 0.06, 0.42, MIN, None)]
        orb_r, ring_w = s * 0.13, s * 0.03
    else:
        rings = [(s * 0.375, s * 0.052, 0.7, DAY, s * 0.042), (s * 0.29, s * 0.04, 0.42, MIN, s * 0.032),
                 (s * 0.215, s * 0.032, 0.78, SEC, s * 0.027)]
        orb_r, ring_w = s * 0.115, s * 0.018
    for r, w, frac, stops, head in rings:
        d.ellipse((c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2), outline=TRACK, width=round(w))
        hx, hy = _arc(d, c, r, w, frac, stops)
        if head:
            _head(img, hx, hy, head, stops[-1])
            d = ImageDraw.Draw(img)
    _orb(img, c, orb_r, ring_w)
    # 白底在浅色任务栏上要有边：一圈极淡的描边
    ImageDraw.Draw(img).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.225, outline=(0, 0, 0, 22), width=round(s * 0.008))
    return img.resize((size, size), Image.LANCZOS)


def make_icon(size: int = 64) -> Image.Image:
    return _render(size)


def save_ico(path: str) -> None:
    """exe / 安装包图标：每个尺寸单独渲染（小尺寸用简化版）。"""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [make_icon(n) for n in sizes]
    imgs[-1].save(path, sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
