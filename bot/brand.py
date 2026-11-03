"""图标：黑色圆角方块 + 白色机器人头 + 蓝色天线点，和自家其他软件（Lumen IDE、听音、AI Reader…）同一套样式。

图标是代码画的（render），`python -m bot.brand` 重新生成 bot/ui 里的三个文件（面板直接用，打包后也在）：
  icon.png       留白和其他软件一致，48px 以上用（exe 大图标、安装包、面板欢迎页占位）
  icon-face.png  留白更窄、图形更大，48px 及以下用（托盘、任务栏小图标、面板标题栏），16px 也认得出
  icon.ico       窗口 / 任务栏 / 快捷方式用的多尺寸 .ico
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from .config import RES_DIR

UI = RES_DIR / "bot" / "ui"

BG, FG, BLUE = (29, 29, 31, 255), (255, 255, 255, 255), (0, 113, 227, 255)


def render(size: int = 1024, margin: int = 64) -> Image.Image:
    """按 1024 画布的坐标画，4 倍超采样后缩到 size。margin 是底板离画布边的留白。"""
    ss = 4
    k = size * ss / 1024
    im = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def box(x0, y0, x1, y1):
        return [round(v * k) for v in (x0, y0, x1, y1)]

    def r(v):
        return round(v * k)

    side = 1024 - 2 * margin
    d.rounded_rectangle(box(margin, margin, 1024 - margin, 1024 - margin), radius=r(side * 0.2), fill=BG)

    # 机器人头部按底板大小等比缩放，居中
    g = side / 896 * 0.92
    cx, cy = 512, 520

    def gbox(x0, y0, x1, y1):
        return box(cx + x0 * g, cy + y0 * g, cx + x1 * g, cy + y1 * g)

    d.rounded_rectangle(gbox(-14, -250, 14, -130), radius=r(14 * g), fill=FG)        # 天线杆
    d.ellipse(gbox(-48, -312, 48, -216), fill=BLUE)                                   # 天线头：蓝点
    d.rounded_rectangle(gbox(-330, -20, -276, 130), radius=r(27 * g), fill=FG)       # 左耳
    d.rounded_rectangle(gbox(276, -20, 330, 130), radius=r(27 * g), fill=FG)         # 右耳
    d.rounded_rectangle(gbox(-262, -150, 262, 270), radius=r(130 * g), fill=FG)      # 头
    for ex in (-104, 104):
        d.rounded_rectangle(gbox(ex - 38, 0, ex + 38, 124), radius=r(38 * g), fill=BG)  # 眼睛
    return im.resize((size, size), Image.LANCZOS)


def make_icon(size: int = 64, face: bool | None = None) -> Image.Image:
    if face is None:
        face = size <= 48
    return Image.open(UI / ("icon-face.png" if face else "icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS)


def save_ico(path: str) -> None:
    """exe / 安装包 / 窗口用的 .ico：每个尺寸单独缩放（小尺寸用窄留白版）。"""
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [make_icon(n) for n in sizes]
    imgs[-1].save(path, sizes=[(n, n) for n in sizes], append_images=imgs[:-1])


if __name__ == "__main__":
    render(512, 64).save(UI / "icon.png")
    render(512, 24).save(UI / "icon-face.png")
    save_ico(str(UI / "icon.ico"))
    print("已生成", UI / "icon.png", UI / "icon-face.png", UI / "icon.ico")
