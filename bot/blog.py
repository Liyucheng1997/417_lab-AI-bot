"""随手记：在面板里写笔记 / 感想，直接发到 liyucheng.me 的博客。

和网站后台 blog/write.html 用同一套接口、同一份数据（服务器上的 posts.json，GitHub 由服务器备份）：
  GET  /api/blog/posts                 全部文章
  PUT  /api/blog/posts {posts,message} 头 X-Blog-Pin
  POST /api/blog/login                 头 X-Blog-Pin，校验数字密码
服务器只有「整份读 / 整份写」，所以全量列表只留在这里（内存缓存），网页每次只拿最近几条。
数字密码存在 Windows 凭据管理器；草稿存在 data/note_draft.json，写到一半收起面板也不会丢。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from .config import DATA_DIR

SITE = "https://liyucheng.me"
CRED_TARGET = "MyAIBot/BlogPin"
DRAFT_PATH = DATA_DIR / "note_draft.json"
CATS_PATH = DATA_DIR / "blog_cats.json"  # 上次从网站拿到的分类，断网时用
# 分类以网站上的 blog/cats.json 为准（网站加了标签，Bot 下次启动自动跟上）；拿不到时先用上次存的，再不行用这份
CATS = {"think": "个人思考", "read": "读书笔记", "research": "科研心得", "learn": "学习方法", "lang": "语言学习",
        "film": "观影感受"}
CACHE_S = 120  # 两分钟内再打开直接用缓存（别处写了的话点刷新）


def _parse_cats(d) -> dict:
    """{"think": {"zh": "个人思考", "en": ...}} 或 {"think": "个人思考"} → {"think": "个人思考"}"""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            name = v.get("zh") if isinstance(v, dict) else v
            if isinstance(k, str) and k and isinstance(name, str) and name.strip():
                out[k] = name.strip()
    return out


def _saved_cats() -> dict:
    try:
        return _parse_cats(json.loads(CATS_PATH.read_text(encoding="utf-8")))
    except Exception:
        return {}


# ------------------------------------------------------------------ 数字密码（Windows 凭据管理器）
def get_pin() -> str:
    import win32cred
    try:
        return win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)["CredentialBlob"].decode("utf-16-le")
    except Exception:
        return ""


def _set_pin(pin: str) -> None:
    import win32cred
    win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": CRED_TARGET,
                         "UserName": "blog-pin", "CredentialBlob": pin,
                         "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)


def _clear_pin() -> None:
    import win32cred
    try:
        win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        pass


class BlogError(Exception):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth  # 密码不对 / 被锁：网页要回到输密码


def _request(method: str, path: str, pin: str = "", body: dict | None = None, timeout: float = 15) -> dict:
    headers = {"User-Agent": "MyAIBot", "Accept": "application/json", "Cache-Control": "no-store"}
    data = None
    if pin:
        headers["X-Blog-Pin"] = pin
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(SITE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or ""
        except Exception:
            msg = ""
        if e.code in (403, 429):
            raise BlogError(msg or "密码不对", auth=True)
        raise BlogError(msg or f"服务器返回 {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BlogError(f"连不上 liyucheng.me（{getattr(e, 'reason', e)}）")


def _key(p: dict) -> tuple:
    return (str(p.get("date", "")), str(p.get("time", "")), str(p.get("id", "")))


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


class Blog:
    def __init__(self):
        self._posts: list | None = None  # 服务器上的原始顺序（新的在前）
        self._at = 0.0
        self._lock = threading.Lock()
        self.cats = _saved_cats() or dict(CATS)
        self._cats_ready = threading.Event()
        threading.Thread(target=self._load_cats, daemon=True, name="blog-cats").start()

    # ---------------- 分类（启动时后台从网站拿一次）
    def _load_cats(self) -> None:
        try:
            cats = _parse_cats(_request("GET", f"/blog/cats.json?t={int(time.time())}", timeout=8))
            if cats:
                self.cats = cats
                DATA_DIR.mkdir(exist_ok=True)
                tmp = CATS_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(cats, ensure_ascii=False), encoding="utf-8")
                tmp.replace(CATS_PATH)
        except Exception:
            pass
        finally:
            self._cats_ready.set()

    # ---------------- 登录
    def status(self) -> dict:
        self._cats_ready.wait(3)  # 刚启动就打开随手记时稍等网站的分类
        return {"logged_in": bool(get_pin()), "cats": self.cats, "site": SITE}

    def login(self, pin: str) -> dict:
        pin = str(pin or "").strip()
        if not (pin.isdigit() and 6 <= len(pin) <= 10):
            return {"ok": False, "error": "数字密码是 6–10 位数字"}
        try:
            _request("POST", "/api/blog/login", pin=pin)
        except BlogError as e:
            return {"ok": False, "error": str(e)}
        _set_pin(pin)
        return {"ok": True}

    def logout(self) -> None:
        _clear_pin()

    # ---------------- 读
    def _fetch(self) -> list:
        d = _request("GET", f"/api/blog/posts?t={int(time.time() * 1000)}")
        posts = d.get("posts") if isinstance(d, dict) else None
        if not isinstance(posts, list):
            raise BlogError("服务器返回的数据不对")
        self._posts, self._at = posts, time.monotonic()
        return posts

    def recent(self, offset: int = 0, limit: int = 5, refresh: bool = False) -> dict:
        """最近的几条（按日期时间倒序）。只把这一页发给网页。"""
        try:
            with self._lock:
                if refresh or self._posts is None or time.monotonic() - self._at > CACHE_S:
                    self._fetch()
                rows = sorted(self._posts, key=_key, reverse=True)
        except BlogError as e:
            return {"ok": False, "error": str(e)}
        offset, limit = max(0, int(offset)), max(1, min(50, int(limit)))
        page = rows[offset:offset + limit]
        return {"ok": True, "posts": page, "total": len(rows), "more": offset + len(page) < len(rows)}

    # ---------------- 写
    def save(self, post: dict) -> dict:
        """发布新笔记，或保存修改（post 里带 id）。先取最新一份再改，免得盖掉网站上刚写的。"""
        pin = get_pin()
        if not pin:
            return {"ok": False, "error": "先输入数字密码", "auth": True}
        text = str(post.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "内容不能为空"}
        now = datetime.now()
        pid = str(post.get("id") or "")
        item = {
            "id": pid or _base36(int(time.time() * 1000)),
            "date": str(post.get("date") or now.strftime("%Y-%m-%d")),
            "time": str(post.get("time") if post.get("time") is not None else now.strftime("%H:%M")),
            "cat": post.get("cat") if post.get("cat") in self.cats else "think",
            "title": str(post.get("title") or "").strip(),
            "text": text,
        }
        label = item["title"] or text[:20]
        try:
            with self._lock:
                posts = list(self._fetch())
                i = next((n for n, p in enumerate(posts) if pid and p.get("id") == pid), -1)
                if i >= 0:
                    posts[i] = item
                else:
                    posts.insert(0, item)
                _request("PUT", "/api/blog/posts", pin=pin,
                         body={"posts": posts, "message": f"blog: {'更新' if i >= 0 else '发布'}「{label}」（Bot）"})
                self._posts, self._at = posts, time.monotonic()
        except BlogError as e:
            return {"ok": False, "error": str(e), "auth": e.auth}
        return {"ok": True, "post": item, "updated": i >= 0}

    def delete(self, pid: str) -> dict:
        pin = get_pin()
        if not pin:
            return {"ok": False, "error": "先输入数字密码", "auth": True}
        try:
            with self._lock:
                posts = self._fetch()
                gone = next((p for p in posts if p.get("id") == pid), None)
                if not gone:
                    return {"ok": True}
                kept = [p for p in posts if p.get("id") != pid]
                label = gone.get("title") or str(gone.get("text", ""))[:20]
                _request("PUT", "/api/blog/posts", pin=pin, body={"posts": kept, "message": f"blog: 删除「{label}」（Bot）"})
                self._posts, self._at = kept, time.monotonic()
        except BlogError as e:
            return {"ok": False, "error": str(e), "auth": e.auth}
        return {"ok": True}

    # ---------------- 草稿
    @staticmethod
    def draft() -> dict:
        try:
            return json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def set_draft(d: dict | None) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        if not d or not (str(d.get("text") or "").strip() or str(d.get("title") or "").strip()):
            DRAFT_PATH.unlink(missing_ok=True)
            return
        tmp = DRAFT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DRAFT_PATH)
