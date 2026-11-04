"""文件图标和缩略图：和资源管理器一样（IShellItemImageFactory），转成 data:image/png 给网页用。

图片 / 视频取缩略图（Windows 有缓存，很快）；其余按扩展名取图标并缓存，快捷方式和 exe 按文件取各自的图标。
"""
from __future__ import annotations

import base64
import ctypes
import io
import os
import threading
import time
from collections import OrderedDict
from ctypes import wintypes

from PIL import Image

ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


IID_IShellItemImageFactory = GUID()
ole32.CLSIDFromString("{bcc18b79-ba16-442f-80c4-8a59c30c463b}", ctypes.byref(IID_IShellItemImageFactory))
shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                               ctypes.POINTER(ctypes.c_void_p)]
shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_uint]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

GetImage = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int, ctypes.POINTER(wintypes.HBITMAP))
Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

SIIGBF_BIGGERSIZEOK, SIIGBF_ICONONLY, SIIGBF_THUMBNAILONLY = 0x1, 0x4, 0x8
PER_FILE = {"lnk", "exe", "url", "appref-ms", "ico", "msi"}
THUMB_CATS = {"image", "video"}

_cache: OrderedDict[str, str] = OrderedDict()
_lock = threading.Lock()
_com = threading.local()


def _bitmap_png(hbm, size: int) -> str:
    bm = BITMAP()
    gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = bm.bmWidth, bm.bmHeight
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    hdc = user32.GetDC(None)
    try:
        gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bih), 0)
    finally:
        user32.ReleaseDC(None, hdc)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
    if img.getchannel("A").getextrema() == (0, 0):  # 照片缩略图常常没有透明通道
        img.putalpha(255)
    if max(w, h) > size:  # BIGGERSIZEOK 时视频会给整帧大小
        img.thumbnail((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=False)
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def _image(path: str, size: int, flags: int) -> str | None:
    if not getattr(_com, "ok", False):
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        _com.ok = True
    factory = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(IID_IShellItemImageFactory),
                                           ctypes.byref(factory)) < 0 or not factory.value:
        return None
    try:
        vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        hbm = wintypes.HBITMAP()
        if GetImage(vtbl[3])(factory, SIZE(size, size), flags, ctypes.byref(hbm)) < 0 or not hbm.value:
            return None
        try:
            return _bitmap_png(hbm, size)
        finally:
            gdi32.DeleteObject(hbm)
    finally:
        Release(vtbl[2])(factory)


def icon_for(path: str, cat: str, ext: str, size: int = 64) -> str | None:
    """返回 data URL；取不到就返回 None（网页里用通用图标）。"""
    if cat == "folder":
        key = "folder"
    elif cat in THUMB_CATS:
        key = "t:" + path.lower()
    elif ext in PER_FILE:
        key = "f:" + path.lower()
    else:
        key = "e:" + ext
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    url = None
    try:
        if cat in THUMB_CATS:
            url = _image(path, size, SIIGBF_BIGGERSIZEOK)
        else:
            url = _image(path, size, SIIGBF_ICONONLY | SIIGBF_BIGGERSIZEOK)
    except Exception as e:
        print("取图标失败：", path, e)
    with _lock:
        _cache[key] = url
        while len(_cache) > 1500:
            _cache.popitem(last=False)
    return url


def icons(items: list, budget: float = 1.2) -> dict:
    """批量取图标；超过 budget 秒就先返回已经取到的，剩下的网页下次再要。"""
    out, t0 = {}, time.time()
    for it in items:
        if time.time() - t0 > budget:
            break
        path, cat, ext = it["path"], it.get("cat", ""), it.get("ext", "")
        if os.path.exists(path):
            out[path] = icon_for(os.path.normpath(path), cat, ext)
    return out
