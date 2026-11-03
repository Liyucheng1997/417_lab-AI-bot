"""图标：你的 3D 数字分身（和面板欢迎页的 3D 形象是同一个模型），淡紫圆角底。

两张原图都在 bot/ui 里（面板直接用，打包后也在）：
  icon.png       半身像，48px 以上用（exe 大图标、安装包、面板加载失败时的占位）
  icon-face.png  只取脸，48px 及以下用（托盘、任务栏小图标、面板标题栏），16px 也认得出
"""
from __future__ import annotations

from PIL import Image

from .config import RES_DIR

UI = RES_DIR / "bot" / "ui"


def make_icon(size: int = 64, face: bool | None = None) -> Image.Image:
    if face is None:
        face = size <= 48
    return Image.open(UI / ("icon-face.png" if face else "icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS)


def save_ico(path: str) -> None:
    """exe / 安装包 / 窗口用的 .ico：每个尺寸单独缩放（小尺寸用大头版）。"""
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [make_icon(n) for n in sizes]
    imgs[-1].save(path, sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
