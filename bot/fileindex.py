"""本机文件搜索（替代 Everything）：只索引主人自己会打开的文件——文档、图片、视频、音乐、压缩包、快捷方式。

- 不索引系统和程序文件：跳过 Windows、Program Files、AppData、node_modules、.git 等目录，
  以及含 .dll 的程序目录；开始菜单里的快捷方式单独收录（可以当启动器用）。
- 全部放在内存里（几十万条，搜索一次几十毫秒），缓存到 data/file_index.pkl，下次启动秒开。
- ReadDirectoryChangesW 监听各盘变化，新建 / 删除 / 改名几秒内反映到结果里；启动时和每天再全盘核对一次。
- 排序参考使用习惯：在这里打开过的文件（按次数、最近程度、当时搜的词）、Windows「最近使用」里的文件、
  常用文件夹里的文件排在前面。
"""
from __future__ import annotations

import ctypes
import fnmatch
import heapq
import json
import math
import os
import pickle
import queue
import re
import string
import threading
import time
from array import array
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

CACHE_PATH = DATA_DIR / "file_index.pkl"
instance: "FileSearch | None" = None  # 程序里唯一的一份（app.py 创建）
HISTORY_PATH = DATA_DIR / "search_history.json"
CACHE_VERSION = 4

CAT_NAMES = {"doc": "文档", "image": "图片", "video": "视频", "audio": "音乐",
             "archive": "压缩包", "app": "应用", "folder": "文件夹"}
EXTS = {
    "doc": "pdf doc docx docm dot dotx rtf odt ott wps wpt txt md markdown xls xlsx xlsm xlsb csv ods et ett "
           "ppt pptx pptm pps ppsx odp dps dpt epub mobi azw azw3 djvu caj kdh nh teb ofd xps oxps chm "
           "tex bib one vsd vsdx mpp xmind mm opml enex dwg dxf skp blend stl step stp 3mf c4d",
    "image": "jpg jpeg jpe jfif png gif bmp webp heic heif avif tif tiff svg raw cr2 cr3 nef arw dng orf rw2 "
             "raf psd psb ai eps cdr xcf sketch fig xd indd afphoto afdesign kra clip",
    "video": "mp4 m4v mkv avi mov wmv flv f4v webm rmvb rm 3gp mpg mpeg m2ts mts vob ogv",
    "audio": "mp3 flac wav m4a aac ogg oga opus wma ape aiff aif mid midi amr dsf dff",
    "archive": "zip rar 7z tar gz tgz bz2 xz zst iso cab",
    "app": "lnk url exe msi appref-ms",
}
EXT_CAT = {e: c for c, s in EXTS.items() for e in s.split()}
CAT_CODE = {c: i for i, c in enumerate(["doc", "image", "video", "audio", "archive", "app"])}
CODE_CAT = {i: c for c, i in CAT_CODE.items()}

# 盘符根目录下直接跳过的系统目录
ROOT_SKIP = {"windows", "programdata", "$recycle.bin", "system volume information", "recovery", "perflogs",
             "$winreagent", "msocache", "$windows.~bt", "$windows.~ws", "$sysreset", "config.msi",
             "onedrivetemp", "intel", "amd", "nvidia", "drivers", "swsetup", "xboxgames", "wpsystem", "boot"}
# 任何层级都跳过的目录（另外：以 . 或 $ 开头的、隐藏+系统属性的、软链接 / 联接点、含 .dll 的程序目录）
ANY_SKIP = {"program files", "program files (x86)", "appdata", "application data", "node_modules",
            "bower_components", "__pycache__", "site-packages", "dist-packages", "steamapps", "windowsapps",
            "anaconda3", "miniconda3", "default", "default user", "all users", "temp", "tmp", "logs",
            "collection.media", "thumbnails", "texlive", "miktex"}
# 代码项目（有 package.json 之类）里的编译 / 打包输出目录：都是重复的副本
PROJECT_MARKERS = {"package.json", "pyproject.toml", "setup.py", "cargo.toml", "go.mod", "pom.xml",
                   "build.gradle", "cmakelists.txt", "makefile"}
BUILD_DIRS = {"dist", "build", "out", "target", "release", "debug", "bin", "obj", "coverage",
              "public", "static", "assets", "res", "resources", "vendor", "third_party"}
HIDDEN, SYSTEM = 0x2, 0x4

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _start_menus() -> list[str]:
    out = []
    for env in ("APPDATA", "PROGRAMDATA"):
        p = Path(os.environ.get(env, "")) / "Microsoft/Windows/Start Menu/Programs"
        if p.is_dir():
            out.append(str(p))
    return out


def _drives() -> list[str]:
    return [f"{d}:\\" for d in string.ascii_uppercase if kernel32.GetDriveTypeW(f"{d}:\\") == 3]  # DRIVE_FIXED


def _skip_dir(low: str, at_root: bool) -> bool:
    return (low[:1] in ".$" or low in ANY_SKIP or "cache" in low or "缓存" in low
            or (at_root and low in ROOT_SKIP))


def _has_program(path: str, depth: int = 1) -> bool:
    """目录里（或下一层）有 .exe / .dll。"""
    try:
        with os.scandir(path) as it:
            for e in it:
                low = e.name.lower()
                if low.endswith((".exe", ".dll")):
                    return True
                if depth and e.is_dir(follow_symlinks=False) and _has_program(e.path, depth - 1):
                    return True
    except OSError:
        pass
    return False


def _is_software(entries, check_bin: bool = True) -> bool:
    """装好的软件目录：有 .dll、有卸载程序，或者 bin 目录里是可执行文件（TeX Live、MATLAB、游戏引擎……）。"""
    for e in entries:
        low = e.name.lower()
        if low.endswith(".dll") or (low.startswith("unins") and low.endswith((".exe", ".dat"))):
            return True
    for e in entries if check_bin else ():
        if e.name.lower() in ("bin", "binaries") and e.is_dir(follow_symlinks=False) and _has_program(e.path):
            return True
    return False


def _is_project(entries) -> bool:
    return any(e.name.lower() in PROJECT_MARKERS or e.name.lower().endswith((".sln", ".csproj")) for e in entries)


def _decay(age_s: float, half_days: float) -> float:
    return 0.5 ** (max(age_s, 0) / (half_days * 86400))


def _join(d: str, name: str) -> str:
    return d + name if d.endswith("\\") else d + "\\" + name


class Index:
    """一份索引：目录表 + 文件表（平行列表，删除只打标记，全盘重建时才真正清掉）。

    省内存：目录只存小写完整路径 + 原样的目录名（显示用的完整路径顺着父目录拼出来），
    文件名本来就是小写的就和小写版共用一个字符串；父子关系不常驻，用到时扫一遍数组。
    """

    def __init__(self):
        self.d_low: list[str] = []       # 小写完整路径
        self.d_orig: list[str] = []      # 原样的目录名（根目录存完整的 C:\ 这种）
        self.d_name: list[str] = []      # 小写目录名（搜文件夹用）
        self.d_parent = array("i")
        self.d_alive = bytearray()
        self.d_content = bytearray()     # 子树里有被索引的文件：这样的文件夹才出现在结果里
        self.by_path: dict[str, int] = {}
        self.f_name: list[str] = []
        self.f_low: list[str] = []
        self.f_dir = array("i")
        self.f_size = array("q")
        self.f_mtime = array("d")
        self.f_cat = bytearray()
        self.f_alive = bytearray()
        self.roots: list[int] = []
        self.root_cats: dict[int, set] = {}  # 开始菜单这类根目录只收快捷方式
        self.built = 0.0
        self._paths: dict[int, str] = {}

    CORE = ("d_low", "d_orig", "d_parent", "d_alive", "d_content", "f_name", "f_dir", "f_size", "f_mtime",
            "f_cat", "f_alive", "roots", "root_cats", "built")

    def dump(self) -> dict:
        return {k: getattr(self, k) for k in self.CORE}

    @classmethod
    def from_dump(cls, data: dict) -> "Index":
        """缓存里只存必要的列，其余（小写名、路径表）载入时重新算。"""
        ix = cls()
        for k in cls.CORE:
            setattr(ix, k, data[k])
        ix.d_name = [o.lower() for o in ix.d_orig]
        ix.f_low = [n if n.lower() == n else n.lower() for n in ix.f_name]
        ix.by_path = {low: i for i, low in enumerate(ix.d_low) if ix.d_alive[i]}
        return ix

    @property
    def files(self) -> int:
        return len(self.f_alive) - self.f_alive.count(0)

    def path_of(self, d: int) -> str:
        """目录的完整路径（原样大小写）。"""
        p = self._paths.get(d)
        if p is None:
            par = self.d_parent[d]
            p = self.d_orig[d] if par < 0 else _join(self.path_of(par), self.d_orig[d])
            if len(self._paths) > 50000:
                self._paths.clear()
            self._paths[d] = p
        return p

    def add_dir(self, path: str, parent: int) -> int:
        i = len(self.d_low)
        low = path.lower()
        name = path if parent < 0 else os.path.basename(path)
        self.d_low.append(low)
        self.d_orig.append(name)
        self.d_name.append(name.lower())
        self.d_parent.append(parent)
        self.d_alive.append(1)
        self.d_content.append(0)
        self.by_path[low] = i
        return i

    def add_file(self, d: int, name: str, size: int, mtime: float, cat: str) -> int:
        i = len(self.f_name)
        low = name.lower()
        self.f_name.append(name)
        self.f_low.append(name if low == name else low)
        self.f_dir.append(d)
        self.f_size.append(size)
        self.f_mtime.append(mtime)
        self.f_cat.append(CAT_CODE[cat])
        self.f_alive.append(1)
        while d >= 0 and not self.d_content[d]:
            self.d_content[d] = 1
            d = self.d_parent[d]
        return i

    def kill_dirs(self, ds) -> None:
        """删掉几个目录和它们下面的一切。"""
        kids: dict[int, list[int]] = {}
        alive = self.d_alive
        for i, par in enumerate(self.d_parent):
            if par >= 0 and alive[i]:
                kids.setdefault(par, []).append(i)
        dead: set[int] = set()
        stack = list(ds)
        while stack:
            x = stack.pop()
            if x in dead:
                continue
            dead.add(x)
            alive[x] = 0
            self.by_path.pop(self.d_low[x], None)
            stack.extend(kids.get(x, ()))
        f_alive = self.f_alive
        for i, d in enumerate(self.f_dir):
            if d in dead:
                f_alive[i] = 0

    def root_of(self, d: int) -> int:
        while self.d_parent[d] >= 0:
            d = self.d_parent[d]
        return d

    # ---------------------------------------------------------------- 扫描
    def scan(self, path: str, parent: int, cats: set | None = None, stop=None, progress=None) -> int:
        """把 path 整棵子树扫进来，返回 path 的目录编号（-1 = 这是要跳过的目录）。"""
        top = -1
        stack = [(path, parent)]
        while stack:
            if stop is not None and stop.is_set():
                break
            p, par = stack.pop()
            try:
                with os.scandir(p) as it:
                    entries = list(it)
            except OSError:
                continue
            # 装好的软件、编译输出：整个跳过（盘符、开始菜单这些根目录除外）
            project = _is_project(entries)
            if par >= 0 and _is_software(entries, check_bin=not project):
                continue
            d = self.add_dir(p, par)
            if top < 0:
                top = d
            at_root = len(p) <= 3
            for e in entries:
                name = e.name
                low = name.lower()
                try:
                    if e.is_dir(follow_symlinks=False):
                        if (e.is_junction() or e.is_symlink() or _skip_dir(low, at_root)
                                or (project and low in BUILD_DIRS)):
                            continue
                        attrs = e.stat(follow_symlinks=False).st_file_attributes
                        if attrs & HIDDEN and attrs & SYSTEM:
                            continue
                        stack.append((e.path, d))
                        continue
                    dot = low.rfind(".")
                    cat = EXT_CAT.get(low[dot + 1:]) if dot > 0 else None
                    if not cat or (cats and cat not in cats) or low.startswith("~$"):
                        continue
                    st = e.stat(follow_symlinks=False)
                    if st.st_file_attributes & HIDDEN:
                        continue
                    self.add_file(d, name, st.st_size, st.st_mtime, cat)
                except OSError:
                    continue
            if progress and len(self.d_low) % 2000 == 0:
                progress(self)
        return top

    def rescan_dirs(self, ds: set[int]) -> None:
        """这些目录里有变化：重新列一遍，增删改文件；新出现的子目录整个扫进来，消失的子目录连同内容删掉。"""
        files: dict[int, list[int]] = {d: [] for d in ds}
        subs: dict[int, list[int]] = {d: [] for d in ds}
        for i, d in enumerate(self.f_dir):
            if d in files and self.f_alive[i]:
                files[d].append(i)
        for i, par in enumerate(self.d_parent):
            if par in subs and self.d_alive[i]:
                subs[par].append(i)
        gone: list[int] = []
        for d in ds:
            if self.d_alive[d]:
                gone += self._rescan(d, files[d], subs[d])
        if gone:
            self.kill_dirs(gone)

    def _rescan(self, d: int, file_ids: list[int], sub_ids: list[int]) -> list[int]:
        path = self.path_of(d)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            return [] if os.path.isdir(path) else [d]
        cats = self.root_cats.get(self.root_of(d))
        at_root = len(path) <= 3
        project = _is_project(entries)
        old_files = {self.f_low[f]: f for f in file_ids}
        old_subs = {self.d_name[s]: s for s in sub_ids}
        seen_files, seen_subs = set(), set()
        for e in entries:
            low = e.name.lower()
            try:
                if e.is_dir(follow_symlinks=False):
                    if (e.is_junction() or e.is_symlink() or _skip_dir(low, at_root)
                            or (project and low in BUILD_DIRS)):
                        continue
                    seen_subs.add(low)
                    if low not in old_subs:
                        self.scan(e.path, d, cats)
                    continue
                dot = low.rfind(".")
                cat = EXT_CAT.get(low[dot + 1:]) if dot > 0 else None
                if not cat or (cats and cat not in cats) or low.startswith("~$"):
                    continue
                st = e.stat(follow_symlinks=False)
                if st.st_file_attributes & HIDDEN:
                    continue
                seen_files.add(low)
                f = old_files.get(low)
                if f is None:
                    self.add_file(d, e.name, st.st_size, st.st_mtime, cat)
                else:
                    self.f_size[f], self.f_mtime[f], self.f_name[f] = st.st_size, st.st_mtime, e.name
            except OSError:
                continue
        for low, f in old_files.items():
            if low not in seen_files:
                self.f_alive[f] = 0
        return [s for low, s in old_subs.items() if low not in seen_subs]

    def nearest_dir(self, low_path: str) -> int:
        """路径（小写）所在的、已经在索引里的最近一级目录；中间经过要跳过的目录就返回 -1。"""
        p = low_path
        while True:
            parent = os.path.dirname(p)
            if parent == p:
                return -1
            d = self.by_path.get(parent)
            if d is not None:
                rest = low_path[len(parent):].strip("\\").split("\\")[:-1]
                at_root = len(parent) <= 3
                for i, seg in enumerate(rest):
                    if _skip_dir(seg, at_root and i == 0):
                        return -1
                return d
            p = parent


class History:
    """使用习惯：在搜索里打开过什么（次数、最近一次、当时搜的词）+ Windows「最近使用」。"""

    def __init__(self):
        self.opens: dict[str, dict] = {}
        self.recent: dict[str, tuple[str, float]] = {}  # 小写路径 → (路径, 最近打开时间)
        self.dir_affinity: dict[str, float] = {}
        self._lnk_cache: dict[str, tuple[float, str]] = {}
        self._recent_at = 0.0
        try:
            self.opens = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("opens", {})
        except Exception:
            pass
        self._rebuild_affinity()

    def _rebuild_affinity(self) -> None:
        aff: dict[str, float] = {}
        now = time.time()
        for low, h in self.opens.items():
            d = os.path.dirname(low)
            aff[d] = aff.get(d, 0) + h["n"] * _decay(now - h["last"], 60)
        for low, (_, t) in self.recent.items():
            d = os.path.dirname(low)
            aff[d] = aff.get(d, 0) + 0.5 * _decay(now - t, 30)
        self.dir_affinity = aff

    def record(self, path: str, query: str, weight: float = 1.0) -> None:
        low = path.lower()
        h = self.opens.setdefault(low, {"path": path, "n": 0, "last": 0, "q": {}})
        h["path"], h["n"], h["last"] = path, h["n"] + weight, time.time()
        q = query.strip().lower()
        if q:
            h["q"][q] = h["q"].get(q, 0) + 1
            if len(h["q"]) > 12:
                h["q"] = dict(sorted(h["q"].items(), key=lambda kv: -kv[1])[:12])
        if len(self.opens) > 3000:
            keep = sorted(self.opens.items(), key=lambda kv: -kv[1]["last"])[:2500]
            self.opens = dict(keep)
        self._rebuild_affinity()
        try:
            HISTORY_PATH.write_text(json.dumps({"opens": self.opens}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def refresh_recent(self, force: bool = False) -> None:
        """读 %APPDATA%\\Microsoft\\Windows\\Recent 里的快捷方式（主人用任何软件打开过的文件都在这里）。"""
        if not force and time.time() - self._recent_at < 600:
            return
        self._recent_at = time.time()
        folder = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Recent"
        try:
            import pythoncom
            from win32com.shell import shell
            pythoncom.CoInitialize()
        except Exception:
            return
        out: dict[str, tuple[str, float]] = {}
        cache: dict[str, tuple[float, str]] = {}
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            if not e.name.lower().endswith(".lnk"):
                continue
            try:
                mtime = e.stat().st_mtime
                hit = self._lnk_cache.get(e.path)
                if hit and hit[0] == mtime:
                    target = hit[1]
                else:
                    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                                      shell.IID_IShellLink)
                    link.QueryInterface(pythoncom.IID_IPersistFile).Load(e.path)
                    target = link.GetPath(0)[0]
                cache[e.path] = (mtime, target)
                if target and len(target) > 3:
                    out[target.lower()] = (target, mtime)
            except Exception:
                continue
        self._lnk_cache = cache
        self.recent = out
        self._rebuild_affinity()

    def habit(self, low: str, query: str, now: float) -> float:
        """使用习惯加分。"""
        s = 0.0
        h = self.opens.get(low)
        if h:
            s += 30 * math.log2(1 + h["n"]) * (0.4 + 0.6 * _decay(now - h["last"], 30))
            if query:
                for q, n in h["q"].items():
                    if q == query:
                        s += 60 + 10 * min(n, 4)
                        break
                    if q.startswith(query) or query.startswith(q):
                        s += 30
                        break
        r = self.recent.get(low)
        if r:
            s += 25 * _decay(now - r[1], 14) + 5
        aff = self.dir_affinity.get(os.path.dirname(low))
        if aff:
            s += min(12, 6 * math.log2(1 + aff))
        return s


def _parse(query: str):
    plain, excl, wild, paths, exts = [], [], [], [], None
    for a, b in re.findall(r'"([^"]+)"|(\S+)', query):
        t = (a or b).lower()
        if t.startswith("ext:") and len(t) > 4:
            exts = {x.strip(".") for x in re.split(r"[;,|]", t[4:]) if x.strip(".")}
        elif t.startswith("!") and len(t) > 1:
            excl.append(t[1:])
        elif "\\" in t or "/" in t:
            paths.append(t.replace("/", "\\"))
        elif "*" in t or "?" in t:
            wild.append(re.compile(fnmatch.translate(t)))
        else:
            plain.append(t)
    return plain, excl, wild, paths, exts


def _known_folders() -> list[tuple[str, str]]:
    home = os.path.expanduser("~")
    names = [("Desktop", "桌面"), ("Documents", "文档"), ("Downloads", "下载"), ("Pictures", "图片"),
             ("Videos", "视频"), ("Music", "音乐"), ("OneDrive", "OneDrive")]
    out = [(os.path.join(home, a).lower(), b) for a, b in names]
    out.append((home.lower(), "~"))
    return out


KNOWN = _known_folders()
USER_DIRS = tuple(p for p, _ in KNOWN[:-1])


def friendly_dir(path: str) -> str:
    low = path.lower()
    for prefix, label in KNOWN:
        if low == prefix or low.startswith(prefix + "\\"):
            rest = path[len(prefix):].strip("\\")
            return label + (" › " + rest.replace("\\", " › ") if rest else "")
    return path


def _fmt_time(t: float, now: float) -> str:
    d = datetime.fromtimestamp(t)
    today = datetime.fromtimestamp(now).date()
    if d.date() == today:
        return d.strftime("今天 %H:%M")
    if (today - d.date()).days == 1:
        return d.strftime("昨天 %H:%M")
    return d.strftime("%m-%d %H:%M") if d.year == today.year else d.strftime("%Y-%m-%d")


class FileSearch:
    """后台线程：载入缓存 → 监听变化 → 全盘核对；搜索在调用线程里做（持锁几十毫秒）。"""

    def __init__(self, emit=None, exclude: list[str] | None = None, extra_ext: list[str] | None = None):
        self.emit = emit or (lambda ev: None)
        self.ix = Index()
        self.lock = threading.RLock()
        self.history = History()
        self.state = "loading"  # loading / scanning / ready
        self.scan_count = 0
        self.exclude = [str(Path(p)).lower() for p in (exclude or [])]
        for e in extra_ext or []:
            EXT_CAT.setdefault(str(e).lower().strip("."), "doc")
        self._dirty: set[str] = set()
        self._events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._rescan = threading.Event()
        self._last: tuple | None = None
        self._last_results: list = []
        self._last_total = 0
        self._saved_at = 0.0
        self._changed = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        threading.Thread(target=self._main, daemon=True, name="file-index").start()

    def rebuild(self) -> None:
        self._rescan.set()

    def status(self) -> dict:
        ix = self.ix
        return {"state": self.state, "files": ix.files, "scanned": self.scan_count,
                "built": datetime.fromtimestamp(ix.built).strftime("%m-%d %H:%M") if ix.built else ""}

    def _emit_status(self) -> None:
        self.emit({"type": "index", **self.status()})

    def _main(self) -> None:
        try:
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)  # BELOW_NORMAL
        except Exception:
            pass
        loaded = self._load()
        self.state = "ready" if loaded else "scanning"
        self._emit_status()
        roots = _drives()
        for r in roots:
            threading.Thread(target=self._watch, args=(r,), daemon=True, name=f"watch-{r[0]}").start()
        threading.Thread(target=self._apply_events, daemon=True, name="index-events").start()
        try:
            self.history.refresh_recent(force=True)
        except Exception as e:
            print("读取最近使用失败：", e)
        # 有缓存：等启动忙完再核对（程序没开时的变化）；没有缓存：马上建
        if loaded:
            self._rescan.wait(90)
        while not self._stop.is_set():
            self._rescan.clear()
            try:
                self._full_scan()
            except Exception as e:
                print("建立文件索引出错：", e)
                self.state = "ready"
                self._emit_status()
            self._rescan.wait(24 * 3600)

    def _load(self) -> bool:
        try:
            with open(CACHE_PATH, "rb") as f:
                data = pickle.load(f)
            if data.get("v") != CACHE_VERSION:
                return False
            ix = Index.from_dump(data["ix"])
            with self.lock:
                self.ix = ix
            print(f"文件索引：从缓存载入 {ix.files} 个文件")
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            print("文件索引缓存读取失败，重新建立：", e)
            return False

    def _save(self) -> None:
        tmp = CACHE_PATH.with_suffix(".tmp")
        try:
            with self.lock:
                data = pickle.dumps({"v": CACHE_VERSION, "ix": self.ix.dump()}, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.write_bytes(data)
            os.replace(tmp, CACHE_PATH)
            self._saved_at, self._changed = time.time(), False
        except Exception as e:
            print("文件索引缓存保存失败：", e)

    def _full_scan(self) -> None:
        t0 = time.time()
        self.state, self.scan_count = "scanning", 0
        self._emit_status()
        ix = Index()
        last_emit = [0.0]

        def progress(x: Index):
            self.scan_count = len(x.f_name)
            if time.time() - last_emit[0] > 1:
                last_emit[0] = time.time()
                self._emit_status()

        with self.lock:
            dirty_before = set(self._dirty)
        for r in _drives():
            d = ix.scan(r, -1, stop=self._stop, progress=progress)
            if d >= 0:
                ix.roots.append(d)
        for r in _start_menus():
            d = ix.scan(r, -1, cats={"app"})
            if d >= 0:
                ix.roots.append(d)
                ix.root_cats[d] = {"app"}
        self._apply_excludes(ix)
        ix.built = time.time()
        with self.lock:
            # 扫描期间发生的变化：在新索引上再做一遍
            redo = {ix.by_path[low] for low in self._dirty - dirty_before if low in ix.by_path}
            if redo:
                ix.rescan_dirs(redo)
            self.ix = ix
            self._last = None
        self.state = "ready"
        print(f"文件索引：{ix.files} 个文件、{len(ix.d_low)} 个目录，用时 {time.time() - t0:.1f} 秒")
        self._save()
        self._emit_status()

    def _apply_excludes(self, ix: Index) -> None:
        dead = [ix.by_path[p] for p in self.exclude if p in ix.by_path]
        if dead:
            ix.kill_dirs(dead)

    # ---------------------------------------------------------------- 实时更新
    def _watch(self, root: str) -> None:
        import win32con
        import win32file
        flags = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME | win32con.FILE_NOTIFY_CHANGE_DIR_NAME
                 | win32con.FILE_NOTIFY_CHANGE_SIZE | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE)
        try:
            h = win32file.CreateFile(root, 0x0001, 7, None, win32con.OPEN_EXISTING,
                                     win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
        except Exception as e:
            print(f"监听 {root} 失败：", e)
            return
        while not self._stop.is_set():
            try:
                changes = win32file.ReadDirectoryChangesW(h, 1 << 18, True, flags, None, None)
            except Exception as e:
                print(f"监听 {root} 中断：", e)
                time.sleep(30)
                continue
            if not changes:  # 缓冲区溢出（一下子变化太多）：过一会儿全盘核对
                self._events.put(("overflow", root))
                continue
            for action, rel in changes:
                self._events.put((action, root + rel))

    def _apply_events(self) -> None:
        pending: set[str] = set()
        overflow_at = first = 0.0
        while not self._stop.is_set():
            try:
                action, path = self._events.get(timeout=0.5)
            except queue.Empty:
                action = None
            # 攒一小会儿再处理（一次保存常常触发好几个事件）；一直有事件也最多等 3 秒
            if pending and (action is None or time.time() - first > 3):
                self._flush(pending)
                pending = set()
            if action is None:
                if self._changed and time.time() - self._saved_at > 600 and self.state == "ready":
                    self._save()
                continue
            if action == "overflow":
                if time.time() - overflow_at > 1800:
                    overflow_at = time.time()
                    self._rescan.set()
                continue
            low = path.lower()
            dot = low.rfind(".")
            ext = low[dot + 1:] if dot > low.rfind("\\") else ""
            # 只关心被索引的文件类型、已知的目录，以及可能是新目录的路径
            if not (ext in EXT_CAT or low in self.ix.by_path or (action in (1, 5) and os.path.isdir(path))):
                continue
            if not pending:
                first = time.time()
            pending.add(low)

    def _flush(self, paths: set[str]) -> None:
        with self.lock:
            ix = self.ix
            dirs = set()
            for low in paths:
                d = ix.nearest_dir(low)
                if d >= 0 and ix.d_alive[d]:
                    dirs.add(d)
            for d in dirs:
                self._dirty.add(ix.d_low[d])
            if dirs:
                ix.rescan_dirs(dirs)
                self._changed = True
                self._last = None
            if len(self._dirty) > 50000:
                self._dirty.clear()

    # ---------------------------------------------------------------- 搜索
    def search(self, query: str, cat: str = "all", sort: str = "smart", offset: int = 0, limit: int = 80) -> dict:
        t0 = time.perf_counter()
        query = (query or "").strip()
        if not query:
            return self.recent(cat, limit)
        key = (query, cat, sort)
        with self.lock:
            if self._last != key:
                self._last_results, self._last_total = self._run(query, cat, sort)
                self._last = key
            ranked = self._last_results
            items = []
            for kind, i in ranked[offset:offset + limit]:
                items.append(self._row(kind, i))
        return {"items": items, "total": self._last_total, "more": offset + limit < len(ranked), "offset": offset,
                "ms": int((time.perf_counter() - t0) * 1000), **self.status()}

    def _run(self, query: str, cat: str, sort: str) -> list:
        ix = self.ix
        plain, excl, wild, paths, exts = _parse(query)
        if not (plain or wild or paths or exts):
            return [], 0
        want = None if cat in ("all", "folder") else CAT_CODE.get(cat)
        f_low, f_alive, f_cat, f_dir, d_low = ix.f_low, ix.f_alive, ix.f_cat, ix.f_dir, ix.d_low
        main = max(plain, key=len) if plain else ""
        others = [t for t in plain if t is not main]
        multi = len(plain) > 1

        def name_ok(low: str, d: int) -> bool:
            for t in others:
                if t not in low and t not in d_low[d]:  # 多个词时，其余的词也可以出现在文件夹路径里
                    return False
            for t in excl:
                if t in low:
                    return False
            for r in wild:
                if not r.match(low):
                    return False
            if exts is not None:
                dot = low.rfind(".")
                if dot < 0 or low[dot + 1:] not in exts:
                    return False
            for p in paths:
                if p not in _join(d_low[d], low):
                    return False
            return True

        files: list[int] = []
        if cat != "folder":
            if main:
                pool = (i for i, low in enumerate(f_low) if main in low)
                if multi:  # 主词也可能只出现在路径里，但至少要有一个词出现在文件名中
                    pool = (i for i, low in enumerate(f_low)
                            if any(t in low for t in plain) and (main in low or main in d_low[f_dir[i]]))
            else:
                pool = range(len(f_low))
            for i in pool:
                if f_alive[i] and (want is None or f_cat[i] == want) and name_ok(f_low[i], f_dir[i]):
                    files.append(i)
        dirs: list[int] = []
        if cat in ("all", "folder") and exts is None and not wild:
            d_name, d_alive, d_content, d_parent = ix.d_name, ix.d_alive, ix.d_content, ix.d_parent
            for i, low in enumerate(d_name):
                if (d_parent[i] >= 0 and d_alive[i] and d_content[i] and (not main or main in low)
                        and all(t in low or t in d_low[d_parent[i]] for t in others)
                        and not any(t in low for t in excl) and all(p in d_low[i] + "\\" for p in paths)):
                    dirs.append(i)

        now = time.time()
        q = " ".join(plain)
        if sort == "recent":
            key_f = lambda i: -ix.f_mtime[i]
            key_d = lambda i: 0.0
        elif sort == "name":
            key_f = lambda i: f_low[i]
            key_d = lambda i: ix.d_name[i]
        elif sort == "size":
            key_f = lambda i: -ix.f_size[i]
            key_d = lambda i: 1.0
        else:
            hist = self.history

            def match_score(low: str, d: int) -> float:
                if not main:
                    return 20
                stem = low.rsplit(".", 1)[0] if "." in low else low
                if stem == q or stem == main:
                    return 100
                if low.startswith(main):
                    return 70
                p = low.find(main)
                if p < 0:
                    return 5  # 只在路径里
                prev = low[p - 1]
                return 45 if not prev.isalnum() or ord(prev) > 0x2E80 or main[0] > "⺀" else 25

            def key_f(i):
                low = f_low[i]
                d = f_dir[i]
                full = _join(d_low[d], low)
                s = match_score(low, d) + hist.habit(full, q, now) + 12 * _decay(now - ix.f_mtime[i], 30)
                if d_low[d].startswith(USER_DIRS):
                    s += 6
                return -s

            def key_d(i):
                low = ix.d_name[i]
                s = match_score(low + ".", i) + hist.habit(d_low[i], q, now) + 4
                return -s

            if len(files) > 20000:
                # 结果太多（只打了一两个字）：先用便宜的分数挑出前 3000，再加上用过的文件，最后精排
                used = {os.path.basename(p) for p in hist.opens} | {os.path.basename(p) for p in hist.recent}
                cheap = heapq.nsmallest(3000, files, key=lambda i: -(match_score(f_low[i], 0)
                                                                     + 12 * _decay(now - ix.f_mtime[i], 30)))
                keep = set(cheap)
                keep.update(i for i in files if f_low[i] in used)
                total = len(files)
                files = list(keep)
                ranked = [(key_f(i), 0, i) for i in files] + [(key_d(i), 1, i) for i in dirs]
                ranked.sort(key=lambda t: t[0])
                return [(kind, i) for _, kind, i in ranked], total
        ranked = [(key_f(i), 0, i) for i in files] + [(key_d(i), 1, i) for i in dirs]
        ranked.sort(key=lambda t: t[0])
        return [(kind, i) for _, kind, i in ranked], len(ranked)

    def _row(self, kind: int, i: int) -> dict:
        ix = self.ix
        now = time.time()
        if kind == 1:
            path = ix.path_of(i)
            parent = ix.path_of(ix.d_parent[i])
            return {"path": path, "name": os.path.basename(path), "dir": friendly_dir(parent), "dir_path": parent,
                    "cat": "folder", "ext": "", "size": "", "time": ""}
        d = ix.f_dir[i]
        name = ix.f_name[i]
        dpath = ix.path_of(d)
        return self._file_row(_join(dpath, name), name, dpath, CODE_CAT[ix.f_cat[i]], ix.f_size[i], ix.f_mtime[i], now)

    @staticmethod
    def _file_row(path, name, dir_path, cat, size, mtime, now) -> dict:
        dot = name.rfind(".")
        ext = name[dot + 1:].lower() if dot > 0 else ""
        shown = name[:dot] if ext in ("lnk", "url", "appref-ms") else name
        return {"path": path, "name": shown, "dir": friendly_dir(dir_path), "dir_path": dir_path, "cat": cat,
                "ext": ext, "size": _fmt_size(size) if cat != "app" else "", "time": _fmt_time(mtime, now)}

    def recent(self, cat: str = "all", limit: int = 60) -> dict:
        """没输入时：最近打开过的（这里打开的 + Windows 最近使用），按类型筛选。"""
        threading.Thread(target=self.history.refresh_recent, daemon=True).start()
        h = self.history
        rows: dict[str, tuple[float, str]] = {}
        for low, (path, t) in h.recent.items():
            rows[low] = (t, path)
        for low, o in h.opens.items():
            if low not in rows or rows[low][0] < o["last"]:
                rows[low] = (o["last"], o["path"])
        items = []
        now = time.time()
        for t, path in sorted(rows.values(), reverse=True):
            if len(items) >= limit:
                break
            try:
                st = os.stat(path)
            except OSError:
                continue
            name = os.path.basename(path.rstrip("\\")) or path
            if os.path.isdir(path):
                if cat not in ("all", "folder"):
                    continue
                items.append({"path": path, "name": name, "dir": friendly_dir(os.path.dirname(path)),
                              "dir_path": os.path.dirname(path), "cat": "folder", "ext": "", "size": "",
                              "time": _fmt_time(t, now)})
                continue
            dot = name.rfind(".")
            c = EXT_CAT.get(name[dot + 1:].lower()) if dot > 0 else None
            if not c or (cat != "all" and c != cat):
                continue
            row = self._file_row(path, name, os.path.dirname(path), c, st.st_size, t, now)
            items.append(row)
        return {"items": items, "total": len(items), "offset": 0, "recent": True, "ms": 0, **self.status()}

    def record_open(self, path: str, query: str, weight: float = 1.0) -> None:
        self.history.record(path, query, weight)
        self._last = None

    def find(self, query: str, limit: int = 50) -> list[str]:
        """给 Bot 用：返回完整路径。"""
        r = self.search(query, "all", "smart", 0, limit)
        return [it["path"] for it in r["items"]]


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
    return ""
