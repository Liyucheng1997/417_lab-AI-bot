"""版本更新：查 GitHub Releases 有没有新版本，下载更新安装包，静默安装后自动重启。

发布约定（release.ps1 生成）：每个版本的 Release 带两个文件
  MyAIBot-Setup-v<版本>.exe   完整安装包（含 OCR 引擎，首次安装用）
  MyAIBot-Update-v<版本>.exe  更新包（不含 OCR 引擎，几十 MB，客户端内更新用）
两者 AppId 相同，都装到原来的安装目录，配置、记忆、对话历史都不动。

仓库是私有的，访问 GitHub API 需要令牌：依次用环境变量 GITHUB_TOKEN / GH_TOKEN、本机 gh 登录（gh auth token）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import __version__
from .config import FROZEN

REPO = "Liyucheng1997/417_lab-AI-bot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
# 调试用：指向一个本地 JSON 文件（格式同 GitHub 的 releases/latest），不联网也能走完整流程
FEED = os.environ.get("MYAIBOT_UPDATE_FEED", "")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "MyAIBot-update"
_token: str | None = None


def parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3]) or (0,)


def _github_token() -> str:
    global _token
    if _token is None:
        _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not _token and shutil.which("gh"):
            try:
                _token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                        timeout=10).stdout.strip()
            except Exception:
                pass
    return _token


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """下载附件时 GitHub 会 302 到带签名的存储地址，那边不能再带令牌。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _open(url: str, accept: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"MyAIBot/{__version__}"})
    if urlparse(url).hostname == "api.github.com" and _github_token():
        req.add_header("Authorization", f"Bearer {_github_token()}")
    return _opener.open(req, timeout=timeout)


def check() -> dict:
    """返回 {"available": bool, "current", "version", "notes", "size", "url", "name", "page"}；出错时带 "error"。"""
    info = {"available": False, "current": __version__}
    try:
        if FEED:
            release = json.loads(Path(FEED).read_text(encoding="utf-8"))
        else:
            with _open(API, "application/vnd.github+json", 15) as r:
                release = json.load(r)
    except urllib.error.HTTPError as e:
        info["error"] = "没有权限访问更新仓库（请先运行 gh auth login）" if e.code in (401, 403, 404) \
            else f"检查更新失败：HTTP {e.code}"
        return info
    except Exception as e:
        info["error"] = f"检查更新失败：{e}"
        return info

    version = release.get("tag_name", "").lstrip("v")
    assets = {a["name"]: a for a in release.get("assets", [])}
    # 优先用小的更新包，没有就用完整安装包
    asset = next((a for n, a in assets.items() if n.startswith("MyAIBot-Update-") and n.endswith(".exe")), None) \
        or next((a for n, a in assets.items() if n.startswith("MyAIBot-Setup-") and n.endswith(".exe")), None)
    info.update(version=version, notes=release.get("body") or "", page=release.get("html_url", ""))
    if asset:
        info.update(name=asset["name"], size=asset.get("size", 0),
                    url=asset.get("url") or asset.get("browser_download_url"))
    info["available"] = bool(asset) and parse_version(version) > parse_version(__version__)
    return info


def download(info: dict, progress: Callable[[int, int], None]) -> Path:
    """下载更新安装包到临时目录，边下边回调 progress(已下载, 总大小)。"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DOWNLOAD_DIR / info["name"]
    part = target.with_suffix(".part")
    url = info["url"]
    if Path(url).exists():  # 本地 FEED 调试
        shutil.copyfile(url, target)
        return target
    with _open(url, "application/octet-stream", 60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            progress(done, total)
    if total and done != total:
        raise IOError(f"下载不完整（{done}/{total} 字节）")
    part.replace(target)
    return target


def install(installer: Path) -> None:
    """后台静默运行更新安装包；它会等本程序退出后再覆盖文件，装完自动重新启动 Bot。"""
    if not FROZEN:
        raise RuntimeError("源码运行的版本请用 git pull 更新")
    subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/UPDATE"],
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)
