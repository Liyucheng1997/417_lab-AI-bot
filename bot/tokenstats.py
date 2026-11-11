"""本机 Claude Code 的 token 用量，按 API 官方价格折算成美元（「如果走 API 要花多少钱」）。

数据来源：~/.claude/projects/**/*.jsonl（Claude Code、桌面版 Code、本 Bot 的会话记录，含子代理）。
每条助手消息带 message.model 和 message.usage；同一条消息按内容块会重复写几行，按 message.id 去重。
记录文件很大（几个 GB），所以按文件记住读到哪个字节，之后只读新追加的部分；结果缓存在 data/token_stats.pkl。
"""
from __future__ import annotations

import json
import os
import pickle
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
CACHE_PATH = DATA_DIR / "token_stats.pkl"
CACHE_VERSION = 1
KEEP_DAYS = 31

# 每百万 token 的美元价格：输入、输出、5 分钟缓存写入、1 小时缓存写入、缓存读取（Anthropic API 官方价，2026-11）
# 按前缀匹配，长的写在前面。fast 模式价格 ×2。
PRICES: list[tuple[str, str, tuple[float, float, float, float, float]]] = [
    ("claude-fable-5-2", "Fable 5.2", (8, 40, 10, 16, 0.20)),
    ("claude-mythos-5-2", "Mythos 5.2", (8, 40, 10, 16, 0.20)),
    ("claude-fable-5-1", "Fable 5.1", (10, 50, 12.5, 20, 0.25)),
    ("claude-mythos-5-1", "Mythos 5.1", (10, 50, 12.5, 20, 0.25)),
    ("claude-fable-5", "Fable 5", (10, 50, 12.5, 20, 1.0)),
    ("claude-mythos-5", "Mythos 5", (10, 50, 12.5, 20, 1.0)),
    ("claude-opus-5-5", "Opus 5.5", (4, 20, 5, 8, 0.20)),
    ("claude-opus-5", "Opus 5", (5, 25, 6.25, 10, 0.50)),
    ("claude-opus-4-8", "Opus 4.8", (5, 25, 6.25, 10, 0.50)),
    ("claude-opus-4-7", "Opus 4.7", (5, 25, 6.25, 10, 0.50)),
    ("claude-opus-4-6", "Opus 4.6", (5, 25, 6.25, 10, 0.50)),
    ("claude-opus-4-5", "Opus 4.5", (5, 25, 6.25, 10, 0.50)),
    ("claude-opus-4", "Opus 4", (15, 75, 18.75, 30, 1.50)),
    ("claude-sonnet-5-5", "Sonnet 5.5", (2, 10, 2.5, 4, 0.10)),
    ("claude-sonnet-5", "Sonnet 5", (2, 10, 2.5, 4, 0.20)),
    ("claude-sonnet-4", "Sonnet 4", (3, 15, 3.75, 6, 0.30)),
    ("claude-haiku-5-5", "Haiku 5.5", (0.10, 0.50, 0.125, 0.20, 0.01)),
    ("claude-haiku-4-5", "Haiku 4.5", (1, 5, 1.25, 2, 0.10)),
]
HAIKU_55_LONG = (0.50, 2.50, 0.625, 1.0, 0.05)  # Haiku 5.5 单次输入超过 10 万 token 时的价格

# 记录：(时间戳, 模型, 是否 fast, 输入, 5 分钟缓存写入, 1 小时缓存写入, 缓存读取, 输出)
Rec = tuple[float, str, bool, int, int, int, int, int]

_lock = threading.Lock()
_files: dict[str, dict] = {}   # 路径 → {"size", "mtime", "off", "recs": {message_id: Rec}}
_loaded = False
_scanned_at = 0.0


def model_info(model: str) -> tuple[str, tuple | None]:
    for prefix, name, price in PRICES:
        if model.startswith(prefix):
            return name, price
    return model.removeprefix("claude-"), None


def cost(rec: Rec) -> float | None:
    _, model, fast, inp, c5, c1h, cr, out = rec
    _, price = model_info(model)
    if price is None:
        return None
    if model.startswith("claude-haiku-5-5") and inp + c5 + c1h + cr > 100_000:
        price = HAIKU_55_LONG
    p_in, p_out, p_5m, p_1h, p_read = price
    usd = (inp * p_in + out * p_out + c5 * p_5m + c1h * p_1h + cr * p_read) / 1e6
    return usd * 2 if fast else usd


# ------------------------------------------------------------------ 扫描
def _parse(line: bytes) -> tuple[str, Rec] | None:
    if b'"usage"' not in line or b'"assistant"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    m = d.get("message")
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
        return None
    model, mid = m.get("model") or "", m.get("id")
    if not mid or not model.startswith("claude-"):  # <synthetic> 等不是真正的模型调用
        return None
    u = m["usage"]
    try:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None
    created = u.get("cache_creation_input_tokens") or 0
    split = u.get("cache_creation") or {}
    c1h = split.get("ephemeral_1h_input_tokens") or 0
    c5 = split.get("ephemeral_5m_input_tokens") or 0
    if c1h + c5 != created:  # 老记录没有分开写：按 5 分钟算
        c5, c1h = created, 0
    return mid, (ts, model, u.get("speed") == "fast", u.get("input_tokens") or 0, c5, c1h,
                 u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0)


def _read_file(path: Path, st: os.stat_result, entry: dict) -> None:
    if st.st_size < entry["off"]:  # 文件被重写过：从头读
        entry.update(off=0, recs={})
    with open(path, "rb") as f:
        f.seek(entry["off"])
        data = f.read(st.st_size - entry["off"])
    end = data.rfind(b"\n") + 1  # 最后一行可能还没写完，留到下次
    for line in data[:end].splitlines():
        hit = _parse(line)
        if hit:
            entry["recs"][hit[0]] = hit[1]  # 同一条消息后写的行覆盖先写的（输出 token 以最后为准）
    entry.update(off=entry["off"] + end, size=st.st_size, mtime=st.st_mtime)


def _load_cache() -> None:
    global _loaded
    _loaded = True
    try:
        with open(CACHE_PATH, "rb") as f:
            data = pickle.load(f)
        if data.get("v") == CACHE_VERSION:
            _files.update(data["files"])
    except (OSError, pickle.UnpicklingError, EOFError, KeyError, TypeError):
        pass


def _save_cache() -> None:
    try:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"v": CACHE_VERSION, "files": _files}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        print("保存 token 统计缓存失败：", e)


def scan(max_age: float = 0) -> None:
    """读新增的会话记录。max_age 秒内扫过就不再扫。"""
    global _scanned_at
    with _lock:
        if max_age and time.time() - _scanned_at < max_age:
            return
        if not _loaded:
            _load_cache()
        cutoff = time.time() - KEEP_DAYS * 86400
        seen, changed = set(), False
        for path in PROJECTS.rglob("*.jsonl") if PROJECTS.exists() else ():
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(path)
            seen.add(key)
            entry = _files.get(key)
            if entry and entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
                continue
            entry = entry or {"size": 0, "mtime": 0, "off": 0, "recs": {}}
            try:
                _read_file(path, st, entry)
            except OSError:
                continue
            _files[key] = entry
            changed = True
        for key in [k for k in _files if k not in seen]:  # 删掉的、过期的文件
            del _files[key]
            changed = True
        if changed:
            _save_cache()
        _scanned_at = time.time()


def records(since: float) -> list[Rec]:
    with _lock:
        merged: dict[str, Rec] = {}
        for entry in _files.values():
            for mid, rec in entry["recs"].items():
                if rec[0] >= since:
                    merged[mid] = rec  # 会话被分叉、续写时同一条消息会出现在两个文件里
        return list(merged.values())


# ------------------------------------------------------------------ 汇总
def _tally(recs: list[Rec]) -> dict:
    by_model: dict[str, dict] = {}
    for rec in recs:
        name, price = model_info(rec[1])
        m = by_model.setdefault(name, {"name": name, "msgs": 0, "input": 0, "output": 0, "cache_write": 0,
                                       "cache_read": 0, "tokens": 0, "cost": 0.0, "priced": price is not None})
        _, _, _, inp, c5, c1h, cr, out = rec
        m["msgs"] += 1
        m["input"] += inp
        m["output"] += out
        m["cache_write"] += c5 + c1h
        m["cache_read"] += cr
        m["tokens"] += inp + c5 + c1h + cr + out
        m["cost"] += cost(rec) or 0.0
    models = sorted(by_model.values(), key=lambda m: (-m["cost"], -m["tokens"]))
    for m in models:
        m["cost"] = round(m["cost"], 2)
    total = {k: sum(m[k] for m in models) for k in ("msgs", "input", "output", "cache_write", "cache_read", "tokens")}
    total["cost"] = round(sum(m["cost"] for m in models), 2)
    return {"models": models, "total": total}


def summary() -> dict:
    """今天 / 近 7 天 / 近 30 天（都含今天）三种视图，按模型分开，7 天和 30 天带每天的柱子。"""
    scan(max_age=20)
    today = date.today()
    start = today - timedelta(days=29)
    recs = records(datetime(start.year, start.month, start.day).timestamp())
    by_day: dict[date, list[Rec]] = {}
    for rec in recs:
        by_day.setdefault(date.fromtimestamp(rec[0]), []).append(rec)

    def view(days: int) -> dict:
        first = today - timedelta(days=days - 1)
        dates = [first + timedelta(days=i) for i in range(days)]
        v = _tally([r for d in dates for r in by_day.get(d, [])])
        if days > 1:
            v["daily"] = []
            for d in dates:
                t = _tally(by_day.get(d, []))
                v["daily"].append({"label": "今天" if d == today else f"{d.month}/{d.day}",
                                   "cost": t["total"]["cost"], "tokens": t["total"]["tokens"],
                                   "models": [[m["name"], m["cost"], m["tokens"]] for m in t["models"]]})
        return v

    return {"today": view(1), "7d": view(7), "30d": view(30)}


def describe(views: dict) -> str:
    """给 Bot 看的文字版。"""
    def money(x):
        return f"${x:,.2f}"

    def tok(n):
        return f"{n / 1e8:.2f} 亿" if n >= 1e8 else f"{n / 1e4:.1f} 万" if n >= 1e4 else str(n)

    lines = []
    for key, label in (("today", "今天"), ("7d", "近 7 天"), ("30d", "近 30 天")):
        v = views[key]
        t = v["total"]
        lines.append(f"【{label}】折合 API 价格 {money(t['cost'])}，共 {tok(t['tokens'])} token，{t['msgs']} 次模型调用")
        for m in v["models"]:
            price = money(m["cost"]) if m["priced"] else "未知价格"
            lines.append(f"  - {m['name']}：{price}；输入 {tok(m['input'])}、输出 {tok(m['output'])}、"
                         f"缓存写入 {tok(m['cache_write'])}、缓存读取 {tok(m['cache_read'])}")
    return "\n".join(lines)


def warm_up(delay: float = 0) -> None:
    """启动后在后台先扫一遍（第一次要十几秒），打开用量卡片时就不用等了。"""
    def run():
        time.sleep(delay)
        try:
            scan()
        except Exception as e:
            print("统计 token 用量出错：", e)
    threading.Thread(target=run, daemon=True, name="token-stats").start()
