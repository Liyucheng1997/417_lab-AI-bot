"""用量面板的数据：Claude 订阅额度（本机 Claude Code）和 API 用量（Claude Console）。

订阅：在后台对 Claude Code 执行 /usage（本地命令，不调用模型、不耗额度、不留会话记录），
      解析「Current session: 20% used · resets …」这样的行。
API： Usage and Cost Analytics API（https://platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api），
      需要 Console 里创建的 Admin API key（sk-ant-admin01-…），保存在 Windows 凭据管理器里，不写进配置文件。
      公开 API 不提供预付费余额，面板里给一个打开 Console 账单页的链接。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from . import __version__
from .config import DATA_DIR

API_BASE = "https://api.anthropic.com/v1/organizations/analytics"
BILLING_URL = "https://platform.claude.com/settings/billing"
USAGE_URL = "https://platform.claude.com/usage"
ADMIN_KEYS_DOC = "https://platform.claude.com/docs/en/manage-claude/admin-api-keys"
CRED_TARGET = "MyAIBot/AnthropicAdminKey"

WINDOW_LABELS = {"current session": "5 小时会话", "current week (all models)": "本周 · 全部模型"}


# ------------------------------------------------------------------ 订阅（Claude Code /usage）
async def subscription() -> dict:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    DATA_DIR.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(cwd=str(DATA_DIR), max_turns=1, setting_sources=[],
                              extra_args={"no-session-persistence": None})
    text = ""
    async for msg in query(prompt="/usage", options=opts):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return parse_usage(text)


def parse_usage(text: str) -> dict:
    windows = []
    for m in re.finditer(r"^(Current [^:\n]+):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets ([^\n]+))?", text, re.M):
        name, pct, resets = m.group(1).strip(), float(m.group(2)), (m.group(3) or "").strip()
        label = WINDOW_LABELS.get(name.lower())
        if not label:  # Current week (Fable) 之类
            inner = re.search(r"\(([^)]+)\)", name)
            label = f"本周 · {inner.group(1)}" if inner else name
        windows.append({"label": label, "pct": pct, "resets": re.sub(r"\s*\([^)]*\)\s*$", "", resets)})
    if not windows:
        return {"error": (text.strip().splitlines() or ["没有读到订阅用量（可能不是用订阅登录的）"])[0]}
    return {"windows": windows}


# ------------------------------------------------------------------ Admin API key（Windows 凭据管理器）
def get_admin_key() -> str:
    if os.environ.get("ANTHROPIC_ADMIN_KEY"):
        return os.environ["ANTHROPIC_ADMIN_KEY"]
    import win32cred
    try:
        cred = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
        return cred["CredentialBlob"].decode("utf-16-le")
    except Exception:
        return ""


def set_admin_key(key: str) -> None:
    import win32cred
    win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": CRED_TARGET,
                         "UserName": "admin-api-key", "CredentialBlob": key,
                         "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)


def clear_admin_key() -> None:
    import win32cred
    try:
        win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        pass


# ------------------------------------------------------------------ API 用量
def _get(endpoint: str, key: str, params: list[tuple[str, str]]) -> dict:
    req = urllib.request.Request(f"{API_BASE}/{endpoint}?{urlencode(params)}", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "anthropic-beta": "analytics-usage-cost-2026-08-24", "User-Agent": f"MyAIBot/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cost(key: str, start: datetime, end: datetime) -> float:
    """这段时间的花费（美元，抵扣额度之前的金额，正好是额度被用掉的部分）。"""
    data = _get("cost", key, [("starting_at", _iso(start)), ("ending_at", _iso(end))]).get("data", [])
    return sum(float(row["cost"]["amount"]) for row in data) / 100


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v).strip())  # YAML 可能已解析成日期


def _credit(key: str, credit: dict, now: datetime) -> dict | None:
    """赠送/预付额度：剩余 = 金额 − 有效期开始以来的花费。"""
    try:
        amount, start, end = float(credit["amount"]), _date(credit["start"]), _date(credit["end"])
    except (KeyError, TypeError, ValueError):
        return None
    begin = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    stop = min(now, datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1))
    used = _cost(key, begin, stop) if stop > begin else 0.0
    today = now.date()
    return {"amount": amount, "used": round(used, 2), "remaining": round(max(amount - used, 0), 2),
            "start": f"{start.month}月{start.day}日", "end": f"{end.month}月{end.day}日",
            "days_left": max((end - today).days + 1, 0), "expired": today > end}


def api_usage(key: str | None = None, credit: dict | None = None) -> dict:
    """额度剩余，以及近 7 天（含今天）的花费、token 数（都按天）和缓存命中率。"""
    key = key or get_admin_key()
    if not key:
        return {"need_key": True}
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    week_start = (now - timedelta(days=6)).replace(hour=0, minute=0)
    # 按天分桶时结束时间会被向下取整到 0 点，传「明天 0 点」才包含今天
    week_end = week_start + timedelta(days=7)
    by_day = [("starting_at", _iso(week_start)), ("ending_at", _iso(week_end)),
              ("time_bucket_width", "1d"), ("order_by", "time"), ("order", "asc"), ("limit", "31")]
    try:
        cost = _get("cost", key, by_day)
        credit_info = _credit(key, credit, now) if credit else None
        usage = _get("usage", key, by_day)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Admin API key 无效或没有权限（需要组织账号的 sk-ant-admin 密钥）", "auth": True}
        return {"error": f"读取 API 用量失败：HTTP {e.code}"}
    except Exception as e:
        return {"error": f"读取 API 用量失败：{e}"}

    days: dict[str, int] = {}
    cached = fresh = 0
    for row in usage.get("data", []):  # 一天可能有多行，按日期合并
        u = row["usage"]
        day = row["starting_at"][:10]
        days[day] = days.get(day, 0) + u.get("total_tokens", 0)
        created = sum((u.get("cache_creation") or {}).values())
        cached += u.get("cache_read_input_tokens", 0)
        fresh += u.get("uncached_input_tokens", 0) + created
    spent: dict[str, float] = {}
    for row in cost.get("data", []):
        day = row["starting_at"][:10]
        spent[day] = spent.get(day, 0.0) + float(row["cost"]["amount"]) / 100
    dates = [week_start + timedelta(days=i) for i in range(7)]
    keys = [d.strftime("%Y-%m-%d") for d in dates]
    return {"spend_7d": round(sum(spent.values()), 2), "tokens_7d": sum(days.values()),
            "daily": [{"label": "今天" if i == 6 else f"{d.month}/{d.day}",
                       "cost": round(spent.get(k, 0.0), 2), "tokens": days.get(k, 0)}
                      for i, (d, k) in enumerate(zip(dates, keys))],
            "cache_hit": round(cached * 100 / (cached + fresh)) if cached + fresh else None,
            "credit": credit_info}
