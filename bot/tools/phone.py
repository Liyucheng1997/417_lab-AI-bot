"""Bot 操作手机的工具（进程内 MCP 服务器 phone，工具名形如 mcp__phone__tap）。

走 adb，不需要投屏窗口开着（只有输入中文时要用投屏窗口的剪贴板同步）。
坐标约定和电脑一样：用「最近一次 screenshot 图片里的像素坐标」，这里换算成手机真实像素。
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import xml.etree.ElementTree as ET
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import phone

MAX_EDGE = 1280
_scale: dict[str, float] = {}       # 截图像素 * scale = 手机像素
_elements: dict[str, list[dict]] = {}

# 面板 App 注册：打开投屏窗口（Bot 要输中文、或主人说「投屏」时用）
open_mirror = None


def _text(s: str, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


def _serial(args: dict) -> str:
    if not phone.available():
        raise RuntimeError("手机投屏组件还没装好：请主人在面板标题栏的「手机」页面里点一下「下载投屏组件」")
    s = str(args.get("serial") or "").strip() or phone.default_serial()
    if not s:
        devs = [d for d in phone.devices() if d["state"] == "device"]
        if not devs:
            raise RuntimeError("没有连着的手机。请主人用数据线连上手机（打开 USB 调试），或在「手机」页面里无线连接")
        names = "；".join(f"{d['name']}（serial={d['serial']}）" for d in devs)
        raise RuntimeError(f"连着好几台手机，请用 serial 参数指定：{names}")
    return s


def _scale_of(serial: str) -> float:
    if serial not in _scale:
        w, h = phone.device_info(serial).get("size") or (1080, 2400)
        _scale[serial] = max(1.0, max(w, h) / MAX_EDGE)
    return _scale[serial]


def _to_phone(serial: str, x: float, y: float) -> tuple[int, int]:
    s = _scale_of(serial)
    return round(x * s), round(y * s)


async def _run(fn, *a):
    return await asyncio.to_thread(fn, *a)


SERIAL = Annotated[str, "手机序列号，只连了一台手机时留空"]


@tool("status", "查看连着哪些手机（名称、系统、电量、USB/无线、是否在投屏）。", {})
async def status(args):
    if not phone.available():
        return _text("投屏组件还没下载。请主人打开面板标题栏的「手机」页面，点「下载投屏组件」（约 11 MB）。")
    devs = await _run(phone.devices)
    if not devs:
        return _text("没有连着的手机。USB：数据线连上，手机打开「开发者选项 → USB 调试」；无线：在面板「手机」页面扫码配对。")
    lines = []
    for d in devs:
        state = {"device": "已连接", "unauthorized": "等待手机上点「允许 USB 调试」", "offline": "离线"}.get(d["state"], d["state"])
        extra = [d.get("os") or "", "无线" if d["wireless"] else "USB",
                 f"电量 {d['battery']}%" if d.get("battery") is not None else "",
                 "正在投屏" if d["serial"] in phone.MIRRORS else ""]
        lines.append(f"{d['name']}  serial={d['serial']}  {state}  " + "  ".join(x for x in extra if x))
    return _text("\n".join(lines))


@tool("screenshot", "截取手机屏幕，看手机现在显示什么。操作前、操作后验证结果时都应该截图。", {"serial": SERIAL})
async def screenshot(args):
    s = _serial(args)

    def grab():
        from PIL import Image
        img = Image.open(io.BytesIO(phone.screencap(s))).convert("RGB")
        scale = max(1.0, max(img.size) / MAX_EDGE)
        _scale[s] = scale
        real = img.size
        if scale > 1:
            img = img.resize((round(img.width / scale), round(img.height / scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), img.size, real
    data, size, real = await _run(grab)
    return {"content": [{"type": "image", "data": data, "mimeType": "image/jpeg"},
                        {"type": "text", "text": f"手机截图 {size[0]}x{size[1]}（手机实际 {real[0]}x{real[1]}）。"
                                                 "之后 tap / swipe 的坐标直接用这张图里的像素坐标。"}]}


def _dump(serial: str) -> list[dict]:
    code, out = phone.adb("exec-out", "uiautomator dump /dev/tty", serial=serial, timeout=20)
    xml = out[out.find("<?xml"):out.rfind(">") + 1] if "<?xml" in out else ""
    if not xml:
        code, out = phone.shell(serial, "uiautomator dump /sdcard/window_dump.xml && cat /sdcard/window_dump.xml", timeout=20)
        xml = out[out.find("<?xml"):out.rfind(">") + 1] if "<?xml" in out else ""
    if not xml:
        raise RuntimeError("读取手机界面失败（界面在动画中或这个 App 不支持），可以改用 screenshot 看图操作")
    root = ET.fromstring(xml)
    s = _scale_of(serial)
    items = []

    def texts(n) -> list[str]:
        out = []
        for c in n.iter("node"):
            t = c.get("text", "").strip() or c.get("content-desc", "").strip()
            if t and t not in out:
                out.append(t)
        return out

    def walk(n, in_button: bool) -> None:
        text, desc = n.get("text", "").strip(), n.get("content-desc", "").strip()
        rid = n.get("resource-id", "").split("/")[-1]
        clickable = n.get("clickable") == "true" or n.get("long-clickable") == "true"
        editable = n.get("class", "").endswith("EditText")
        m = re.findall(r"\d+", n.get("bounds", ""))
        ok = len(m) == 4
        if ok:
            x1, y1, x2, y2 = map(int, m)
            ok = x2 - x1 >= 4 and y2 - y1 >= 4
        # 可点的整行（LinearLayout 之类）用它里面的文字当名字；里面的文字就不再单独列出来
        if ok and (clickable or editable or ((text or desc) and not in_button)):
            label = text or desc
            if clickable and not label:
                label = " ".join(texts(n))[:60]
            label = label or rid or n.get("class", "").split(".")[-1]
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            dup = next((it for it in items if it["label"] == label and abs(it["px"][0] - cx) < 12 and abs(it["px"][1] - cy) < 12), None)
            if dup:  # 一层套一层的同一个控件（搜索框常见）只留最里面的一个
                items.remove(dup)
            items.append({"label": label, "kind": "输入框" if editable else ("按钮" if clickable else "文字"),
                          "x": round((x1 + x2) / 2 / s), "y": round((y1 + y2) / 2 / s),
                          "px": ((x1 + x2) // 2, (y1 + y2) // 2), "checked": n.get("checked") == "true",
                          "selected": n.get("selected") == "true"})
        for c in n.findall("node"):
            walk(c, in_button or (clickable and ok))

    walk(root, False)
    return items


@tool("inspect", "读取手机当前界面上的文字、按钮、输入框（编号 + 截图坐标），比看图找位置更准。"
      "之后可以用 tap_element 按编号点击。", {"serial": SERIAL})
async def inspect(args):
    s = _serial(args)
    items = await _run(_dump, s)
    _elements[s] = items
    if not items:
        return _text("界面上没读到文字或按钮（可能是游戏、视频或自绘界面），请用 screenshot 看图操作")
    lines = [f"[{i}] {it['kind']} 「{it['label'][:40]}」 ({it['x']},{it['y']})"
             + (" 已选中" if it["checked"] or it["selected"] else "") for i, it in enumerate(items[:150])]
    return _text("\n".join(lines))


@tool("tap", "点击手机屏幕上的截图坐标 (x, y)。long=true 表示长按。",
      {"x": int, "y": int, "long": Annotated[bool, "长按"], "serial": SERIAL})
async def tap(args):
    s = _serial(args)
    x, y = _to_phone(s, args["x"], args["y"])
    cmd = f"input swipe {x} {y} {x} {y} 700" if args.get("long") else f"input tap {x} {y}"
    code, out = await _run(phone.shell, s, cmd)
    return _text(f"已{'长按' if args.get('long') else '点击'} ({args['x']},{args['y']})", error=code != 0)


@tool("tap_element", "点击 inspect 列出的第 index 个元素。", {"index": int, "long": Annotated[bool, "长按"], "serial": SERIAL})
async def tap_element(args):
    s = _serial(args)
    items = _elements.get(s) or []
    i = int(args["index"])
    if not 0 <= i < len(items):
        return _text("编号不对，先用 inspect 重新读取界面", error=True)
    x, y = items[i]["px"]
    cmd = f"input swipe {x} {y} {x} {y} 700" if args.get("long") else f"input tap {x} {y}"
    code, out = await _run(phone.shell, s, cmd)
    return _text(f"已点击 [{i}] 「{items[i]['label'][:30]}」", error=code != 0)


@tool("swipe", "在手机上从 (x1,y1) 滑到 (x2,y2)（截图坐标），ms 是滑动时长（默认 300）。"
      "往上翻页：从屏幕下方滑到上方。", {"x1": int, "y1": int, "x2": int, "y2": int,
                                     "ms": Annotated[int, "毫秒，默认 300"], "serial": SERIAL})
async def swipe(args):
    s = _serial(args)
    x1, y1 = _to_phone(s, args["x1"], args["y1"])
    x2, y2 = _to_phone(s, args["x2"], args["y2"])
    code, out = await _run(phone.shell, s, f"input swipe {x1} {y1} {x2} {y2} {int(args.get('ms') or 300)}")
    return _text("已滑动", error=code != 0)


@tool("type_text", "在手机当前聚焦的输入框里输入文字（先点一下输入框）。英文数字直接输入；"
      "中文会通过投屏窗口的剪贴板粘贴（没开投屏会自动打开）。", {"text": str, "serial": SERIAL})
async def type_text(args):
    s = _serial(args)
    r = await _run(phone.type_text, s, args["text"])
    if r.get("need_mirror") and open_mirror:
        await _run(open_mirror, s)
        for _ in range(60):
            await asyncio.sleep(0.25)
            m = phone.MIRRORS.get(s)
            if m and m.hwnd:
                break
        await asyncio.sleep(0.8)
        r = await _run(phone.type_text, s, args["text"])
    if not r.get("ok"):
        return _text(r.get("error") or "输入失败", error=True)
    return _text(f"已输入 {len(args['text'])} 个字")


@tool("press_key", "按手机按键：back 返回、home 主页、recents 多任务、enter 回车/搜索、delete 删除一个字、"
      "power 电源（锁屏/亮屏）、wakeup 亮屏、vol_up / vol_down 音量、notifications 下拉通知栏、settings_panel 下拉快捷开关。",
      {"key": str, "repeat": Annotated[int, "重复次数，默认 1"], "serial": SERIAL})
async def press_key(args):
    s = _serial(args)
    name = str(args["key"]).strip().lower()
    for _ in range(max(1, int(args.get("repeat") or 1))):
        r = await _run(phone.key, s, name)
        if not r["ok"]:
            return _text(r["error"], error=True)
    return _text(f"已按 {name}")


@tool("open_app", "在手机上打开 App：写 App 名称（如 微信、淘宝、设置）或包名（如 com.tencent.mm）。", {"app": str, "serial": SERIAL})
async def open_app(args):
    s = _serial(args)
    r = await _run(phone.open_app, s, args["app"])
    if not r["ok"]:
        return _text(r["error"], error=True)
    return _text(f"已打开 {args['app']}（{r['package']}），等一两秒再截图看看")


@tool("list_apps", "列出手机上装的 App（名称和包名），可用关键字筛选。第一次要几秒。",
      {"keyword": Annotated[str, "筛选关键字，可留空"], "serial": SERIAL})
async def list_apps(args):
    s = _serial(args)
    apps = await _run(phone.list_apps, s)
    kw = str(args.get("keyword") or "").lower()
    rows = [f"{n}  {p}" for n, p in apps if not kw or kw in n.lower() or kw in p.lower()]
    return _text("\n".join(rows[:200]) or "没有匹配的 App")


@tool("mirror", "打开或关闭电脑上的手机投屏窗口（主人能直接用鼠标操作手机）。action: open / close。",
      {"action": str, "serial": SERIAL})
async def mirror(args):
    s = _serial(args)
    if str(args.get("action")) == "close":
        m = phone.MIRRORS.get(s)
        if m:
            await _run(m.stop)
        return _text("已关闭投屏窗口")
    if open_mirror:
        await _run(open_mirror, s)
    return _text("已打开投屏窗口")


@tool("shell", "在手机上执行 adb shell 命令（如 dumpsys、getprop、settings get、pm list、am start），返回输出。",
      {"command": str, "serial": SERIAL})
async def shell(args):
    s = _serial(args)
    code, out = await _run(phone.shell, s, args["command"], 30)
    return _text(out[-6000:] or "（没有输出）", error=code != 0)


ALL_TOOLS = [status, screenshot, inspect, tap, tap_element, swipe, type_text, press_key, open_app, list_apps,
             mirror, shell]


def phone_server():
    return create_sdk_mcp_server(name="phone", version="1.0.0", tools=ALL_TOOLS)
