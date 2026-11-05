"""手机投屏：电脑上出现一个实时显示、可以直接用鼠标键盘操作的手机窗口（类似华为多屏协同、苹果 iPhone 镜像）。

Android（含鸿蒙 4 及以前）：scrcpy（Genymobile 开源）+ adb。
  - 连接：USB 数据线；无线调试扫码配对 / 配对码（Android 11+）；USB 连着时一键「切换到无线」（adb tcpip 5555）。
  - 投屏窗口就是 scrcpy 的窗口：画面、声音、鼠标键盘都走它；电脑和手机的剪贴板自动同步；
    拖文件进窗口 = 传到手机 Download，拖 APK = 安装；右键 = 返回，中键 = 主页。
  - 窗口旁边贴着一条苹果风的竖向工具条（phonebar.py）：返回、主页、多任务、通知、音量、截图、锁屏、置顶、问 Bot。
  - scrcpy 第一次用时从 GitHub 下载（约 11 MB，按官方 SHA256SUMS 校验）；安装包里已经带着，不用等。
iPhone：苹果不允许 Windows 控制 iPhone，只能用 AirPlay「屏幕镜像」看画面，需要 uxplay-windows 这个接收端。

Bot 自己操作手机的工具在 tools/phone.py（截图、读界面、点击、滑动、输入、按键、打开 App）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import win32con
import win32gui
import win32process

from . import __version__, dock
from .config import DATA_DIR, ROOT

TOOLS_DIR = ROOT / "phone"
SCRCPY_DIR = TOOLS_DIR / "scrcpy"
SCRCPY = SCRCPY_DIR / "scrcpy.exe"
ADB = SCRCPY_DIR / "adb.exe"
STATE_PATH = DATA_DIR / "phone.json"
LOG_PATH = DATA_DIR / "phone.log"
SHOT_DIR = Path.home() / "Pictures" / "手机截图"

PINNED_VERSION = "v5.0.1"  # GitHub 查不到最新版时用这个
RELEASE_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
RELEASE_URL = "https://github.com/Genymobile/scrcpy/releases/download/{tag}/{name}"
UXPLAY_URL = "https://github.com/leapbtw/uxplay-windows/releases"

NO_WINDOW = subprocess.CREATE_NO_WINDOW
user32 = ctypes.windll.user32

KEYCODES = {"back": 4, "home": 3, "recents": 187, "power": 26, "vol_up": 24, "vol_down": 25, "mute": 164,
            "enter": 66, "delete": 67, "menu": 82, "wakeup": 224, "sleep": 223, "play_pause": 85,
            "tab": 61, "escape": 111}


def _log(*parts) -> None:
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%m-%d %H:%M:%S} " + " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


# ================================================================ 组件下载
def available() -> bool:
    return SCRCPY.exists() and ADB.exists()


def _get(url: str, timeout: float = 20) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, headers={"User-Agent": f"MyAIBot/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout)


def _release() -> tuple[str, str, str]:
    """(版本, zip 下载地址, SHA256SUMS 地址)；查不到最新版就用固定版本。"""
    try:
        with _get(RELEASE_API, 10) as r:
            data = json.load(r)
        assets = {a["name"]: a["browser_download_url"] for a in data["assets"]}
        zip_name = next(n for n in assets if re.fullmatch(r"scrcpy-win64-v[\d.]+\.zip", n))
        return data["tag_name"], assets[zip_name], assets["SHA256SUMS.txt"]
    except Exception as e:
        _log("查询 scrcpy 最新版本失败，用固定版本：", e)
        tag = PINNED_VERSION
        return (tag, RELEASE_URL.format(tag=tag, name=f"scrcpy-win64-{tag}.zip"),
                RELEASE_URL.format(tag=tag, name="SHA256SUMS.txt"))


_dl_lock = threading.Lock()


def ensure_tools(progress: Callable[[int, int], None] | None = None) -> None:
    """下载并解压 scrcpy（含 adb）到 phone/scrcpy；按官方 SHA256SUMS.txt 校验，不对就不用。"""
    with _dl_lock:
        if available():
            return
        tag, zip_url, sums_url = _release()
        name = zip_url.rsplit("/", 1)[-1]
        with _get(sums_url) as r:
            sums = r.read().decode()
        expected = next((ln.split()[0].lower() for ln in sums.splitlines() if ln.strip().endswith(name)), None)
        if not expected:
            raise RuntimeError(f"SHA256SUMS.txt 里没有 {name}")
        buf, h = io.BytesIO(), hashlib.sha256()
        with _get(zip_url, 30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(256 * 1024):
                buf.write(chunk)
                h.update(chunk)
                if progress:
                    progress(buf.tell(), total)
        if h.hexdigest() != expected:
            raise RuntimeError("下载的文件校验不通过（可能被篡改或没下完整），请重试")
        tmp = TOOLS_DIR / "scrcpy.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(buf) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts[1:]  # 去掉压缩包里的 scrcpy-win64-vX/ 这一层
                if not parts or info.is_dir():
                    continue
                dest = tmp.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(info))
        shutil.rmtree(SCRCPY_DIR, ignore_errors=True)
        tmp.rename(SCRCPY_DIR)
        (SCRCPY_DIR / "VERSION").write_text(tag, encoding="utf-8")
        _log("scrcpy 已安装：", tag)


def tools_version() -> str:
    try:
        return (SCRCPY_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ================================================================ adb
_server_lock = threading.Lock()
_server_started = False


def _env() -> dict:
    env = dict(os.environ)
    env["ADB_MDNS_OPENSCREEN"] = "1"  # 无线调试扫码配对要用 mDNS 发现手机
    return env


def _ensure_server() -> None:
    """单独启动 adb 服务：服务进程会继承管道，直接在 capture_output 的调用里拉起会卡住。"""
    global _server_started
    with _server_lock:
        if _server_started:
            return
        subprocess.run([str(ADB), "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, env=_env(), timeout=20)
        _server_started = True


def adb(*args: str, serial: str | None = None, timeout: float = 15, binary: bool = False):
    """执行 adb，返回 (退出码, 输出)。binary=True 时输出是 bytes（截图用）。"""
    if not available():
        raise RuntimeError("投屏组件还没下载")
    _ensure_server()
    cmd = [str(ADB)] + (["-s", serial] if serial else []) + list(args)
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW, env=_env())
    except subprocess.TimeoutExpired:
        return 1, b"" if binary else "超时"
    if binary:
        return p.returncode, p.stdout
    out = (p.stdout + p.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()
    return p.returncode, out


def shell(serial: str, command: str, timeout: float = 15) -> tuple[int, str]:
    return adb("shell", command, serial=serial, timeout=timeout)


def kill_server() -> None:
    global _server_started
    if available() and _server_started:
        try:
            subprocess.run([str(ADB), "kill-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=5)
        except Exception:
            pass
        _server_started = False


# ================================================================ 设备
_info_cache: dict[str, dict] = {}


def _props(serial: str) -> dict:
    keys = ["ro.config.marketing_name", "ro.product.marketname", "ro.product.vendor.marketname",
            "ro.product.brand", "ro.product.model", "ro.build.version.release", "ro.build.version.sdk",
            "hw_sc.build.platform.version"]
    code, out = shell(serial, "getprop", timeout=8)  # 全部属性，每行 [key]: [value]
    allp = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]\s*$", out, re.M)) if code == 0 else {}
    p = {k: allp.get(k, "").strip() for k in keys}
    brand = p["ro.product.brand"].capitalize() if p["ro.product.brand"] else ""
    if brand.lower() in ("huawei", "honor"):
        brand = brand.upper()
    name = (p["ro.config.marketing_name"] or p["ro.product.marketname"] or p["ro.product.vendor.marketname"]
            or " ".join(x for x in (brand, p["ro.product.model"]) if x) or serial)
    os_name = f"Android {p['ro.build.version.release']}" if p["ro.build.version.release"] else "Android"
    if p["hw_sc.build.platform.version"]:
        os_name = f"HarmonyOS {p['hw_sc.build.platform.version']}"
    size = (0, 0)
    code, out = shell(serial, "wm size", timeout=5)
    m = re.findall(r"(\d+)x(\d+)", out)
    if m:
        size = tuple(int(v) for v in m[-1])  # 有 Override size 时取最后一个
    return {"name": name, "os": os_name, "sdk": int(p["ro.build.version.sdk"] or 0), "size": size}


def _battery(serial: str) -> int | None:
    code, out = shell(serial, "dumpsys battery | grep level", timeout=5)
    m = re.search(r"level:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def is_wireless(serial: str) -> bool:
    return bool(re.match(r"^[\d.]+:\d+$", serial)) or "_adb-tls-connect" in serial


def devices() -> list[dict]:
    """当前连着的设备（含正在授权、离线的）。"""
    code, out = adb("devices", "-l", timeout=8)
    rows = []
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        serial, state = parts[0], parts[1]
        extra = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        row = {"serial": serial, "state": state, "wireless": is_wireless(serial),
               "name": extra.get("model", "").replace("_", " ") or serial, "os": "", "battery": None}
        if state == "device":
            info = _info_cache.get(serial)
            if not info or time.time() - info["ts"] > 60:
                info = {**_props(serial), "battery": _battery(serial), "ts": time.time()}
                _info_cache[serial] = info
            row.update(name=info["name"], os=info["os"], battery=info["battery"], sdk=info["sdk"], size=info["size"])
            if row["wireless"]:
                host = serial.split(":")[0] if ":" in serial else ""
                if host:
                    row["addr"] = serial
        rows.append(row)
    # 同一台手机 USB 和无线都连着时，两个都列出来（用户可以拔线继续用无线）
    rows.sort(key=lambda r: (r["state"] != "device", r["wireless"]))
    return rows


def device_info(serial: str) -> dict:
    info = _info_cache.get(serial)
    if not info:
        info = {**_props(serial), "battery": _battery(serial), "ts": time.time()}
        _info_cache[serial] = info
    return info


def default_serial() -> str | None:
    """正在投屏的那台；否则唯一连着的那台。"""
    for s, m in list(MIRRORS.items()):
        if m.alive():
            return s
    ready = [d["serial"] for d in devices() if d["state"] == "device"]
    return ready[0] if len(ready) == 1 else None


# ---------------------------------------------------------------- 记住的无线地址
def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def remember(addr: str, name: str) -> None:
    """记住连过的无线手机（同一个 IP 只留一条：无线调试每次开关端口都会变）。"""
    st = _load_state()
    known = st.setdefault("known", {})
    host = addr.split(":")[0]
    for a in [a for a in known if a.split(":")[0] == host]:
        known.pop(a)
    known[addr] = {"name": name, "ts": int(time.time())}
    _save_state(st)


def _remember_serial(serial: str) -> None:
    """adb 用 mDNS 名字（adb-xxx._adb-tls-connect._tcp）自动连上的手机：查出它的 IP 地址再记住。"""
    try:
        if re.match(r"^[\d.]+:\d+$", serial):
            remember(serial, device_info(serial)["name"])
            return
        for name, kind, addr in _mdns_services():
            if kind.startswith("_adb-tls-connect") and serial.startswith(name):
                remember(addr, device_info(serial)["name"])
                return
    except Exception as e:
        _log("记住手机地址失败", serial, e)


def forget(addr: str) -> None:
    st = _load_state()
    st.get("known", {}).pop(addr, None)
    _save_state(st)


def known() -> list[dict]:
    rows = [{"addr": a, **v} for a, v in _load_state().get("known", {}).items()]
    return sorted(rows, key=lambda r: -r.get("ts", 0))


# ---------------------------------------------------------------- 连接
def connect(addr: str, quiet: bool = False) -> dict:
    addr = addr.strip().replace("：", ":")
    if not re.match(r"^[\w.\-]+(:\d+)?$", addr):
        return {"ok": False, "error": "地址格式不对，应该像 192.168.1.23:5555"}
    if ":" not in addr:
        addr += ":5555"
    code, out = adb("connect", addr, timeout=12)
    ok = "connected to" in out and "cannot" not in out and "failed" not in out
    if ok:
        time.sleep(0.5)
        try:
            remember(addr, device_info(addr)["name"])
        except Exception:
            pass
        return {"ok": True, "serial": addr}
    if not quiet:
        _log("adb connect", addr, out)
    adb("disconnect", addr, timeout=5)  # 连不上的地址别留在列表里显示 offline
    hint = "确认手机和电脑在同一个 Wi-Fi，并且手机上的无线调试 / 网络 ADB 是开着的"
    return {"ok": False, "error": f"连不上 {addr}。{hint}"}


def disconnect(serial: str) -> None:
    adb("disconnect", serial, timeout=6)
    _info_cache.pop(serial, None)


def to_wireless(serial: str) -> dict:
    """USB 连着时切换成无线：打开手机的 5555 端口再按 IP 连接，之后可以拔掉数据线。"""
    code, out = shell(serial, "ip -4 addr", timeout=6)
    # 只认 Wi-Fi 网卡（wlan0 / wlan1 …）；rmnet 是移动数据，电脑连不过去
    m = re.search(r"^\d+: wlan\d*:.*?\n(?:\s+.*\n)*?\s+inet (\d+\.\d+\.\d+\.\d+)", out + "\n", re.M)
    if not m:
        return {"ok": False, "error": "手机好像没连 Wi-Fi，先连上和电脑同一个 Wi-Fi"}
    ip = m.group(1)
    code, out = adb("tcpip", "5555", serial=serial, timeout=10)
    if code != 0:
        return {"ok": False, "error": f"打开无线调试端口失败：{out}"}
    # adbd 带着网络端口重启：USB 那条会断几秒，有的手机（华为）还会重新弹一次「允许 USB 调试」
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1.5)
        state = next((d["state"] for d in devices() if d["serial"] == serial), "")
        if state == "unauthorized":
            continue  # 等主人在手机上点「允许」
        r = connect(f"{ip}:5555", quiet=True)
        if r["ok"]:
            return r
    _log("切换到无线失败", serial, ip)
    return {"ok": False, "error": f"连不上 {ip}:5555。手机上如果又弹出「允许 USB 调试吗？」请点允许，再点一次「切换到无线」；"
                                  "还不行的话确认手机和电脑连的是同一个路由器（有的路由器开了「访客网络 / AP 隔离」）"}


def pair(addr: str, code: str) -> dict:
    addr, code = addr.strip().replace("：", ":"), code.strip()
    if not re.match(r"^[\w.\-]+:\d+$", addr) or not re.match(r"^\d{6}$", code):
        return {"ok": False, "error": "填手机「使用配对码配对」里显示的 IP 地址和端口（如 192.168.1.23:37215）以及 6 位配对码"}
    rc, out = adb("pair", addr, code, timeout=20)
    if "Successfully paired" not in out:
        _log("adb pair", addr, out)
        return {"ok": False, "error": "配对失败：配对码可能已过期，在手机上重新点一次「使用配对码配对」再试"}
    host = addr.split(":")[0]
    return {"ok": True, "serial": _connect_after_pair(host)}


def _mdns_services() -> list[tuple[str, str, str]]:
    code, out = adb("mdns", "services", timeout=6)
    rows = []
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("_adb"):
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _connect_after_pair(host: str) -> str | None:
    """配对完成后，手机通过 mDNS 公布连接端口（adb 一般会自动连上）；最多等 15 秒。"""
    deadline = time.time() + 15
    while time.time() < deadline:
        for d in devices():
            if d["wireless"] and d["state"] == "device" and (host in d["serial"] or "_adb-tls-connect" in d["serial"]):
                _remember_serial(d["serial"])
                return d["serial"]
        for name, kind, addr in _mdns_services():
            if kind.startswith("_adb-tls-connect") and addr.startswith(host + ":"):
                r = connect(addr)
                if r["ok"]:
                    return r["serial"]
        time.sleep(1)
    return None


def nearby(devs: list[dict] | None = None) -> list[dict]:
    """同一个局域网里开着无线调试、但还没连上的手机（mDNS 广播的 _adb-tls-connect 服务）。"""
    devs = devs if devs is not None else devices()
    online = [d["serial"] for d in devs if d["state"] == "device"]
    hosts = {s.split(":")[0] for s in online if ":" in s}
    out = []
    for name, kind, addr in _mdns_services():
        host = addr.split(":")[0]
        if kind.startswith("_adb-tls-connect") and host not in hosts and not any(s.startswith(name) for s in online):
            out.append({"name": name, "addr": addr, "host": host})
    return out


def auto_connect_once() -> list[str]:
    """把记住的手机重新连上：无线调试的端口每次都会变，按 IP 在 mDNS 广播里找新端口；老式 5555 端口直接连。"""
    if not available():
        return []
    devs = devices()
    online = {d["serial"] for d in devs if d["state"] == "device"}
    hosts_online = {s.split(":")[0] for s in online if ":" in s}
    known_hosts = {k["addr"].split(":")[0]: k["addr"] for k in known()}
    joined = []
    for item in nearby(devs):
        if item["host"] in known_hosts and item["host"] not in hosts_online:
            if connect(item["addr"], quiet=True)["ok"]:
                joined.append(item["addr"])
                hosts_online.add(item["host"])
    for host, addr in known_hosts.items():
        if host not in hosts_online and addr.endswith(":5555"):
            if connect(addr, quiet=True)["ok"]:
                joined.append(addr)
    for s in online:  # adb 自己用 mDNS 名字连上的，也记一下 IP
        if "_adb-tls-connect" in s and not any(k.split(":")[0] in s for k in known_hosts.values()):
            _remember_serial(s)
    if joined:
        _log("自动连上：", joined)
    return joined


def auto_connect_loop(on_change: Callable[[], None], interval: float = 12) -> None:
    """后台常驻：配对过的手机一进同一个 Wi-Fi、打开无线调试就自动连上（Bot 的手机工具也能直接用）。"""
    while True:
        try:
            if auto_connect_once():
                on_change()
        except Exception as e:
            _log("自动连接出错", e)
        time.sleep(interval)


class QrPairing:
    """无线调试「使用二维码配对设备」：电脑显示二维码 → 手机扫码后用 mDNS 公布配对服务 → adb pair。"""

    def __init__(self):
        rnd = random.SystemRandom()
        self.name = "MyAIBot-" + "".join(rnd.choices(string.ascii_lowercase + string.digits, k=6))
        self.password = "".join(rnd.choices(string.ascii_letters + string.digits, k=10))
        self.state = "waiting"  # waiting / pairing / connecting / done / error / cancelled
        self.message = "用手机扫这个二维码"
        self.serial: str | None = None
        self.started = time.time()
        threading.Thread(target=self._run, daemon=True, name="phone-qr").start()

    @property
    def payload(self) -> str:
        return f"WIFI:T:ADB;S:{self.name};P:{self.password};;"

    def qr_png(self) -> str:
        import base64
        import qrcode
        q = qrcode.QRCode(border=1, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(self.payload)
        img = q.make_image(fill_color="#1d1d1f", back_color="white").get_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def cancel(self) -> None:
        if self.state in ("waiting", "pairing", "connecting"):
            self.state = "cancelled"

    def _run(self) -> None:
        try:
            while self.state == "waiting":
                if time.time() - self.started > 600:  # 第一次要先去开开发者选项，给足 10 分钟
                    self.state, self.message = "error", "二维码过期了，点「刷新」重新生成"
                    return
                for name, kind, addr in _mdns_services():
                    if name == self.name and kind.startswith("_adb-tls-pairing"):
                        self.state, self.message = "pairing", "扫到了，正在配对…"
                        rc, out = adb("pair", addr, self.password, timeout=20)
                        if "Successfully paired" not in out:
                            _log("二维码配对失败", addr, out)
                            self.state, self.message = "error", "配对失败，请在手机上重新扫码"
                            return
                        self.state, self.message = "connecting", "配对成功，正在连接…"
                        self.serial = _connect_after_pair(addr.split(":")[0])
                        if self.serial:
                            self.state, self.message = "done", "已连接"
                        else:
                            self.state, self.message = ("error", "配对成功了，但没连上。在手机的「无线调试」页面看一下 "
                                                                  "IP 地址和端口，填到下面「直接连接」里")
                        return
                time.sleep(1)
        except Exception as e:
            _log("二维码配对出错", e)
            self.state, self.message = "error", f"出错了：{e}"


# ================================================================ 投屏窗口
MIRRORS: dict[str, "Mirror"] = {}


def _windows_of(pid: int) -> list[int]:
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            try:
                if win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.GetWindowText(hwnd):
                    found.append(hwnd)
            except Exception:
                pass
        return True
    win32gui.EnumWindows(cb, None)
    return found


def visible_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口看得见的边框（不含 Win10/11 窗口四周透明的拖拽边）。"""
    rect = ctypes.wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return win32gui.GetWindowRect(hwnd)


ERRORS = [
    (r"unauthorized", "手机还没允许这台电脑调试：解锁手机，在弹出的「允许 USB 调试吗？」里点允许"),
    (r"Could not find any ADB device|not found", "没找到手机，检查数据线或 Wi-Fi 连接"),
    (r"offline", "手机连接断开了（offline），拔插一下数据线或重新连接无线"),
    (r"Could not open video stream|Could not create (the )?encoder|encoder", "手机的视频编码器启动失败，可以在设置里把画质调低一点再试"),
    (r"Server connection failed|Connection refused|Could not connect", "连不上手机里的投屏服务，重新插一下数据线再试"),
    (r"INSTALL_FAILED|push", "往手机里放投屏服务失败，检查手机存储空间"),
]


class Mirror:
    """一台手机的投屏窗口（scrcpy 进程）+ 旁边的工具条。"""

    def __init__(self, serial: str, opts: dict, place: Callable[[float], tuple[int, int, int, int]],
                 on_event: Callable[[dict], None], on_bar: Callable[[str, str], None]):
        self.serial = serial
        self.opts = opts
        self.place = place
        self.on_event = on_event
        self.on_bar = on_bar
        self.proc: subprocess.Popen | None = None
        self.hwnd = 0
        self.bar = None
        self.frame = None
        self._pending_chars: list[int] = []
        self._flush_timer: threading.Timer | None = None
        self.lines: list[str] = []
        self.on_top = bool(opts.get("on_top"))
        self.name = device_info(serial)["name"]
        self.closing = False

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self) -> None:
        info = device_info(self.serial)
        w, h = info.get("size") or (0, 0)
        ratio = (min(w, h) / max(w, h)) if w and h else 0.46
        x, y, ww, hh = self.place(ratio)  # 竖屏窗口的位置和大小（物理像素）
        scale = user32.GetDpiForSystem() / 96
        o = self.opts
        args = [str(SCRCPY), "-s", self.serial, "--window-title", f"{self.name} · 投屏",
                "--window-x", str(round(x / scale)), "--window-y", str(round(y / scale)),
                "--window-height", str(round(hh / scale)), "--window-borderless", "--stay-awake",
                "--render-driver", "opengl",  # 圆角（SetWindowRgn）和 Direct3D 一起用会让画面卡住不刷新，OpenGL 没这个问题
                "--screen-off-timeout", "600",  # 无线时手机不在充电，--stay-awake 不管用：投屏期间 10 分钟才自动锁屏，结束后恢复
                "--no-terminal-title"]
        if not o.get("audio", True):
            args.append("--no-audio")
        if o.get("screen_off"):
            args.append("--turn-screen-off")
        if self.on_top:
            args.append("--always-on-top")
        if int(o.get("max_size") or 0):
            args += ["--max-size", str(int(o["max_size"]))]
        if o.get("keyboard") == "uhid":
            args.append("--keyboard=uhid")
        _log("启动投屏：", " ".join(args[1:]))
        self.proc = subprocess.Popen(args, cwd=str(SCRCPY_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW, env=_env())
        threading.Thread(target=self._read, daemon=True, name="scrcpy-out").start()
        threading.Thread(target=self._watch, args=((x, y, ww, hh),), daemon=True, name="scrcpy-watch").start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            m = re.search(r"Could not inject char u\+([0-9a-fA-F]{4})", line)
            if m:  # 电脑输入法打出来的中文：scrcpy 注入不了，攒起来走剪贴板粘贴
                self._pending_chars.append(int(m.group(1), 16))
                if self._flush_timer:
                    self._flush_timer.cancel()
                self._flush_timer = threading.Timer(0.12, self._flush_chars)
                self._flush_timer.start()
                continue
            self.lines = (self.lines + [line])[-60:]
            _log("[scrcpy]", line)

    def _flush_chars(self) -> None:
        units, self._pending_chars = self._pending_chars, []
        if not units:
            return
        # 按 UTF-16 拼回去（表情符号是两个代理项）
        text = b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le", "replace")
        try:
            paste_into(self, text, restore_focus=False)
        except Exception as e:
            _log("粘贴中文失败", e)

    def _error_text(self) -> str:
        text = "\n".join(self.lines)
        for pattern, msg in ERRORS:
            if re.search(pattern, text, re.I):
                return msg
        last = next((ln for ln in reversed(self.lines) if "ERROR" in ln), self.lines[-1] if self.lines else "")
        return f"投屏没能启动：{last.replace('ERROR:', '').strip() or '未知原因'}"

    def _watch(self, rect: tuple[int, int, int, int]) -> None:
        deadline = time.time() + 25
        while time.time() < deadline and self.alive() and not self.hwnd:
            wins = _windows_of(self.proc.pid)
            if wins:
                self.hwnd = wins[0]
                break
            time.sleep(0.1)
        if not self.hwnd:
            if self.alive():
                self.proc.kill()
            if not self.closing:
                self.on_event({"type": "phone", "event": "error", "serial": self.serial, "message": self._error_text()})
            MIRRORS.pop(self.serial, None)
            self.on_event({"type": "phone", "event": "changed"})
            return
        self._fit(rect)
        from .phonebar import PhoneBar, PhoneFrame
        self.frame = PhoneFrame(self.hwnd)
        self.bar = PhoneBar(self.hwnd, lambda action: self.on_bar(self.serial, action), pinned=self.on_top,
                            margin=self.frame.margin)
        self.on_event({"type": "phone", "event": "started", "serial": self.serial, "name": self.name})
        self.proc.wait()
        if self.bar:
            self.bar.close()
        if self.frame:
            self.frame.close()
        code = self.proc.returncode
        MIRRORS.pop(self.serial, None)
        if code == 2 and not self.closing:  # 设备中途断开
            self.on_event({"type": "phone", "event": "error", "serial": self.serial,
                           "message": f"{self.name} 断开了，投屏已结束"})
        self.on_event({"type": "phone", "event": "changed"})

    def _fit(self, rect: tuple[int, int, int, int]) -> None:
        """scrcpy 按逻辑像素摆窗口，高 DPI 下会有偏差：出现后按物理像素重新摆。
        无边框窗口整个就是画面，宽高严格按画面比例（不留黑边；横屏时按横屏比例）。"""
        x, y, w, h = rect
        try:
            l, t, r, b = visible_rect(self.hwnd)
            ratio = (r - l) / (b - t) if b > t else w / h
            if abs(ratio - w / h) < 0.03:
                ratio = w / h  # scrcpy 自己按逻辑像素取整会差一点点，用手机的真实比例
            nw, nh = (round(h * ratio), h) if ratio < 1 else (w, round(w / ratio))
            win32gui.SetWindowPos(self.hwnd, 0, x, y, nw, nh, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        except Exception as e:
            _log("摆放投屏窗口失败", e)

    def set_on_top(self, on: bool) -> None:
        self.on_top = on
        if self.hwnd:
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        if self.bar:
            self.bar.set_pinned(on)

    def focus(self) -> None:
        if not self.hwnd:
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
        import win32api
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)  # 按一下 Alt，系统才允许切前台
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass

    def stop(self) -> None:
        """先礼貌地关窗口（scrcpy 会恢复手机亮屏等设置），3 秒没退再强制结束。"""
        self.closing = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ================================================================ 操作
def key(serial: str, name: str) -> dict:
    if name == "notifications":
        shell(serial, "cmd statusbar expand-notifications", timeout=5)
    elif name == "settings_panel":
        shell(serial, "cmd statusbar expand-settings", timeout=5)
    elif name in KEYCODES:
        shell(serial, f"input keyevent {KEYCODES[name]}", timeout=5)
    else:
        return {"ok": False, "error": f"不认识的按键 {name}"}
    return {"ok": True}


def screencap(serial: str) -> bytes:
    code, data = adb("exec-out", "screencap -p", serial=serial, timeout=15, binary=True)
    if code != 0 or not data.startswith(b"\x89PNG"):
        raise RuntimeError("手机截图失败（有的 App 禁止截图，比如银行、视频播放页）")
    return data


def save_screenshot(serial: str) -> Path:
    """截手机屏幕：存到「图片\\手机截图」，同时复制到剪贴板，可以直接粘贴到微信、文档。"""
    data = screencap(serial)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"手机截图_{datetime.now():%Y%m%d_%H%M%S}.png"
    path.write_bytes(data)
    try:
        import win32clipboard
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, buf.getvalue()[14:])
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        _log("截图复制到剪贴板失败", e)
    return path


_paste_lock = threading.Lock()


def _clipboard_snapshot() -> list[tuple[int, bytes]]:
    """把剪贴板里能读出来的格式都存下来（文字、图片、文件…），粘贴完再放回去。"""
    import win32clipboard
    saved = []
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return saved
    try:
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in (2, 3, 14):  # CF_BITMAP / CF_METAFILEPICT / CF_ENHMETAFILE 是句柄，系统会从 DIB 自动合成
                continue
            try:
                saved.append((fmt, win32clipboard.GetClipboardData(fmt)))
            except Exception:
                pass
    finally:
        win32clipboard.CloseClipboard()
    return saved


def _clipboard_restore(saved: list[tuple[int, bytes]]) -> None:
    import win32clipboard
    if not saved:
        return
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.05)
    else:
        return
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in saved:
            try:
                win32clipboard.SetClipboardData(fmt, data)
            except Exception:
                pass
    finally:
        win32clipboard.CloseClipboard()


def paste_into(m: "Mirror", text: str, restore_focus: bool = True) -> None:
    """把文字粘贴进手机当前的输入框：放进电脑剪贴板 → 在投屏窗口按 Ctrl+V（scrcpy 同步到手机并粘贴）→ 还原剪贴板。"""
    import win32api
    import win32clipboard
    with _paste_lock:
        saved = _clipboard_snapshot()
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        prev = user32.GetForegroundWindow()
        if prev != m.hwnd:
            m.focus()
            time.sleep(0.15)
        # 主人可能还按着 Shift（输入法里打标点）：先松开，免得变成 Ctrl+Shift+V（scrcpy 里是逐字注入）
        for vk in (win32con.VK_SHIFT, win32con.VK_MENU, 0x5B):
            if user32.GetAsyncKeyState(vk) & 0x8000:
                win32api.keybd_event(vk, 0, 2, 0)
        for vk, up in ((win32con.VK_CONTROL, 0), (0x56, 0), (0x56, 2), (win32con.VK_CONTROL, 2)):
            win32api.keybd_event(vk, 0, up, 0)
            time.sleep(0.01)
        time.sleep(0.3)  # 等 scrcpy 读完剪贴板
        _clipboard_restore(saved)
        if restore_focus and prev and prev != m.hwnd:
            try:
                win32gui.SetForegroundWindow(prev)
            except Exception:
                pass


def type_text(serial: str, text: str) -> dict:
    """往手机当前输入框里打字。英文数字直接注入；中文等走剪贴板：电脑剪贴板 → 投屏窗口 Ctrl+V（scrcpy 同步到手机再粘贴）。"""
    if not text:
        return {"ok": True}
    if text.isascii() and "\n" not in text and len(text) <= 400:
        arg = text.replace("%", r"\%").replace(" ", "%s")
        code, out = shell(serial, "input text " + shlex.quote(arg), timeout=15)
        return {"ok": code == 0, "error": out if code else ""}
    m = MIRRORS.get(serial)
    if not m or not m.alive() or not m.hwnd:
        return {"ok": False, "error": "输入中文需要先打开投屏窗口（中文是通过剪贴板粘贴过去的）", "need_mirror": True}
    paste_into(m, text, restore_focus=True)
    return {"ok": True}


_apps_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def list_apps(serial: str) -> list[tuple[str, str]]:
    """[(App 名称, 包名)]。用 scrcpy --list-apps 取中文名（第一次几秒，之后缓存 10 分钟）。"""
    hit = _apps_cache.get(serial)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    for attempt in range(2):  # 刚好在开投屏时会和它抢着往手机里放 scrcpy-server，失败了等一下再试一次
        p = subprocess.run([str(SCRCPY), "-s", serial, "--list-apps", "--no-terminal-title"], cwd=str(SCRCPY_DIR),
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=40, creationflags=NO_WINDOW, env=_env())
        apps, pending = [], None
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            m = re.match(r"^\s*[*\-]\s+(.+?)\s{2,}([\w.]+)\s*$", line)
            if m:
                apps.append((m.group(1).strip(), m.group(2)))
                pending = None
                continue
            if pending and (m := re.match(r"^\s{10,}([\w.]+)\s*$", line)):  # 名字太长时包名在下一行
                apps.append((pending, m.group(1)))
                pending = None
                continue
            m = re.match(r"^\s*[*\-]\s+(.+?)\s*$", line)
            pending = m.group(1) if m else None
        if apps:
            _apps_cache[serial] = (time.time(), apps)
            return apps
        _log("scrcpy --list-apps 没拿到结果", p.returncode, p.stdout.decode("utf-8", "replace")[-300:])
        time.sleep(2)
    code, out = shell(serial, "pm list packages -3", timeout=15)  # 实在不行只给包名（不缓存，下次再试）
    return [(x, x) for x in re.findall(r"package:([\w.]+)", out)]


def open_app(serial: str, name: str) -> dict:
    name = name.strip()
    pkg = name if re.fullmatch(r"[a-zA-Z][\w]*(\.[\w]+)+", name) else None
    if not pkg:
        apps = list_apps(serial)
        low = name.lower()
        exact = [p for n, p in apps if n.lower() == low]
        part = [p for n, p in apps if low in n.lower()]
        pkg = (exact or part or [None])[0]
        if not pkg:
            return {"ok": False, "error": f"手机上没找到「{name}」，可以先用 list_apps 看看有哪些"}
    code, out = shell(serial, f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", timeout=15)
    if "No activities found" in out or code != 0:
        return {"ok": False, "error": f"打不开 {pkg}：{out[-200:]}"}
    return {"ok": True, "package": pkg}


# ================================================================ iPhone（只能看）
def iphone_receiver() -> str | None:
    """找本机装的 uxplay-windows（AirPlay 接收端）。"""
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                key_ = winreg.OpenKey(root, base)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key_)[0]):
                try:
                    sub = winreg.OpenKey(key_, winreg.EnumKey(key_, i))
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                except OSError:
                    continue
                if "uxplay" not in str(name).lower():
                    continue
                for value in ("InstallLocation", "DisplayIcon"):
                    try:
                        p = str(winreg.QueryValueEx(sub, value)[0]).split(",")[0].strip('"')
                    except OSError:
                        continue
                    path = Path(p)
                    if path.is_file() and path.suffix.lower() == ".exe":
                        return str(path)
                    if path.is_dir():
                        exe = next((e for e in path.glob("*.exe") if "unins" not in e.name.lower()), None)
                        if exe:
                            return str(exe)
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for exe in Path(base).glob("*uxplay*/*.exe") if base else []:
            if "unins" not in exe.name.lower():
                return str(exe)
    return None


def shutdown() -> None:
    for m in list(MIRRORS.values()):
        try:
            m.stop()
        except Exception:
            pass
    kill_server()


def work_area() -> tuple[int, int, int, int]:
    return dock.work_area()
