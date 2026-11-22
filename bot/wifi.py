"""分享当前 WiFi：读出电脑正连着的 WiFi（含密码），生成手机相机能直接扫的二维码和一段口令。

二维码是通用的 WIFI:T:WPA;S:..;P:..;; 格式；口令（WF1.xxx）和「WiFi 共享器」网页版通用：
https://liyucheng1997.github.io/469_lab-WIFI-sharing/
"""
from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WifiError(Exception):
    pass


def _run(args: list[str] | str) -> str:
    p = subprocess.run(args, capture_output=True, timeout=15, creationflags=NO_WINDOW)
    return (p.stdout + p.stderr).decode("utf-8", errors="replace")


def parse_interfaces(out: str) -> dict | None:
    def field(name: str) -> str:
        m = re.search(rf"^\s+{name}\s+: (.*)$", out, re.M)
        return m.group(1).strip() if m else ""

    ssid = field("SSID")
    return {"ssid": ssid, "profile": field("Profile") or ssid} if ssid else None


def parse_profile_xml(xml: str) -> dict:
    def tag(src: str, name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", src, re.S)
        return unescape(m.group(1)) if m else ""

    block = re.search(r"<SSID>(.*?)</SSID>", xml, re.S)
    auth, enc = tag(xml, "authentication"), tag(xml, "encryption")
    kind = "WPA"  # WPA/WPA2/WPA3 都按 WPA 分享，手机兼容性最好
    if auth in ("open", "OWE"):
        kind = "WEP" if enc == "WEP" else "nopass"
    elif auth == "shared":
        kind = "WEP"
    return {
        "ssid": tag(block.group(1), "name") if block else tag(xml, "name"),
        "password": "" if kind == "nopass" else tag(xml, "keyMaterial"),
        "type": kind,
        "hidden": tag(xml, "nonBroadcast") == "true",
    }


def read_current() -> dict:
    """电脑当前连接的 WiFi：{ssid, password, type, hidden}。读不到时抛 WifiError（带给用户看的说明）。"""
    # chcp 65001 让 netsh 输出 UTF-8 的英文，避开中文系统下的编码和翻译差异
    out = _run("cmd /d /c chcp 65001>nul & netsh wlan show interfaces")
    cur = parse_interfaces(out)
    if not cur:
        if re.search("location", out, re.I):
            raise WifiError("Windows 需要打开「定位服务」才能读取 WiFi 信息（设置 → 隐私和安全性 → 定位）")
        if re.search("not running|no wireless interface", out, re.I):
            raise WifiError("这台电脑没有可用的无线网卡")
        raise WifiError("电脑当前没有连接 WiFi（可能在用网线）")
    with tempfile.TemporaryDirectory(prefix="wifi-share-") as tmp:
        _run(["netsh", "wlan", "export", "profile", f"name={cur['profile']}", "key=clear", f"folder={tmp}"])
        files = list(Path(tmp).glob("*.xml"))
        if not files:
            raise WifiError(f"已连接「{cur['ssid']}」，但读不到它保存的密码")
        info = parse_profile_xml(files[0].read_text(encoding="utf-8", errors="replace"))
    info["ssid"] = info["ssid"] or cur["ssid"]
    if info["type"] != "nopass" and not info["password"]:
        raise WifiError(f"「{info['ssid']}」用的是账号登录之类的方式，没有可以分享的密码")
    return info


def qr_text(w: dict) -> str:
    def esc(s: str) -> str:
        return re.sub(r'([\\;,:"])', r"\\\1", s)

    out = f"WIFI:T:{w['type']};S:{esc(w['ssid'])};"
    if w["type"] != "nopass":
        out += f"P:{esc(w['password'])};"
    if w.get("hidden"):
        out += "H:true;"
    return out + ";"


def share_code(w: dict) -> str:
    raw = json.dumps([w["ssid"], w["password"], w["type"], 1 if w.get("hidden") else 0],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "WF1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def qr_png(text: str) -> str:
    import qrcode
    q = qrcode.QRCode(border=2, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text.encode("utf-8"))
    img = q.make_image(fill_color="black", back_color="white").get_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def share() -> dict:
    """给界面用：成功 {ok, ssid, password, type, qr, message}，失败 {ok: False, error}。"""
    try:
        w = read_current()
    except WifiError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # netsh 超时等
        return {"ok": False, "error": f"读取 WiFi 失败：{e}"}
    code = share_code(w)
    return {
        "ok": True, "ssid": w["ssid"], "password": w["password"], "type": w["type"],
        "qr": qr_png(qr_text(w)),
        "message": f"【WiFi口令】{code}\n复制这段文字，打开「WiFi 共享器」即可加入「{w['ssid']}」\n"
                   "https://liyucheng1997.github.io/469_lab-WIFI-sharing/",
    }
