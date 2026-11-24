"""用量面板的 Google AI Studio（Gemini API）部分：每天用了多少 token、折合多少钱、赠金还剩多少。

Google 没有公开「赠金余额」或 AI Studio 用量的 API，所以拼起来算：
  登录：用 Google Cloud CLI 的 `gcloud auth application-default login` 授权一次，
        这里读它留下的凭据文件（%APPDATA%/gcloud/application_default_credentials.json）自己换 access token。
  用量：Cloud Monitoring 的 generate_content_usage_output_token_count（按模型、文字 / 图片分开）和
        付费层输入 token 配额用量，几分钟延迟，保留 6 周。结算账号下的所有项目都算。
  金额：token × 官方单价（美元），再按欧洲央行汇率换成账单币种。图片按 token 计价，每张图的 token 数固定，估得很准。
  赠金：config.yaml 里 gemini.credits 记下某一刻（as_of）每笔赠金的剩余和到期日，
        剩余 = 快照 − as_of 之后的花费（先扣最早到期的）。as_of 之后的花费优先用 BigQuery 账单导出里的真实抵扣额，
        账单还没覆盖到的那段时间用上面的估算补上。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import DATA_DIR

BILL_PATH = DATA_DIR / "gemini_bill.json"
PRE_PATH = DATA_DIR / "gemini_pre.json"
ADC_PATH = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
SPEND_URL = "https://aistudio.google.com/spend"
CREDITS_URL = "https://console.cloud.google.com/billing/{account}/credits/all"
EXPORT_URL = "https://console.cloud.google.com/billing/{account}/export"
SDK_DIRS = [Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Google" / "Cloud SDK",
            Path(os.environ.get("ProgramFiles", "")) / "Google" / "Cloud SDK"]

# 官方标准价（美元 / 百万 token）：输入、文字输出（含思考）、图片输出。https://aistudio.google.com/docs/pricing
# 有涨价日期的写成 [(生效日, 价格...), ...]
PRICES: dict[str, list[tuple]] = {
    "gemini-3.1-flash-lite-image": [("2000-01-01", 0.25, 1.50, 30.0)],
    "gemini-3.1-flash-image": [("2000-01-01", 0.50, 3.00, 60.0)],
    "gemini-nano-banana-2.1": [("2000-01-01", 1.50, 7.50, 30.0)],
    "gemini-3-pro-image": [("2000-01-01", 2.00, 12.00, 120.0)],
    "gemini-2.5-flash-image": [("2000-01-01", 0.30, 2.50, 30.0)],
    "gemini-3.8-flash": [("2000-01-01", 0.75, 3.75, None), ("2027-01-01", 1.50, 7.50, None)],
    "gemini-3.6-flash": [("2000-01-01", 0.75, 3.75, None), ("2027-01-01", 1.50, 7.50, None)],
    "gemini-3.5-transcribe": [("2000-01-01", 2.00, 12.00, None)],
    "gemini-3.5-flash-lite": [("2000-01-01", 0.30, 2.50, None)],
    "gemini-4-carbon": [("2000-01-01", 2.00, 10.00, None), ("2026-12-01", 4.00, 20.00, None)],
    "gemini-4-argon": [("2000-01-01", 2.00, 10.00, None), ("2026-12-01", 4.00, 20.00, None)],
    "gemini-4-neon": [("2000-01-01", 3.00, 15.00, None)],
    "gemini-4-hydrogen": [("2000-01-01", 1.50, 7.50, None)],
    "gemini-3.1-pro": [("2000-01-01", 2.00, 12.00, None)],
    "gemini-3.1-flash-lite": [("2000-01-01", 0.25, 1.50, None)],
    "gemini-3-flash": [("2000-01-01", 0.50, 3.00, None)],
    "gemini-2.5-pro": [("2000-01-01", 1.25, 10.00, None)],
    "gemini-2.5-flash-lite": [("2000-01-01", 0.10, 0.40, None)],
    "gemini-2.5-flash": [("2000-01-01", 0.30, 2.50, None)],
}
FALLBACK = (2.00, 12.00, 120.0)  # 没见过的模型按偏贵的价格算，宁可多估
IMAGE_TOKENS = {"gemini-2.5-flash-image": 1290}  # 一张 1K 图的输出 token，其余模型都是 1120

OUT_METRIC = "generativelanguage.googleapis.com/generate_content_usage_output_token_count"
IN_METRICS = [f"generativelanguage.googleapis.com/quota/generate_content_paid_tier{t}_input_token_count/usage"
              for t in ("", "_2", "_3")]
RETENTION = timedelta(days=42)  # Monitoring 指标保留 6 周
STEP = timedelta(minutes=10)  # 每 10 分钟一格，算赠金截止时刻时误差不超过 10 分钟

_token: dict = {}
_cache: dict = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ 登录（gcloud 的 ADC 凭据）
def gcloud_path() -> str:
    found = shutil.which("gcloud")
    if found:
        return found
    for d in SDK_DIRS:
        p = d / "google-cloud-sdk" / "bin" / "gcloud.cmd"
        if p.exists():
            return str(p)
    return ""


def login() -> dict:
    """开一个命令行窗口跑 gcloud 登录（会弹浏览器授权），登录完窗口自己关掉。"""
    exe = gcloud_path()
    if not exe:
        return {"ok": False, "error": "没找到 Google Cloud CLI，请先安装：https://cloud.google.com/sdk/docs/install"}
    _token.clear()
    _cache.clear()
    subprocess.Popen(["cmd", "/c", exe, "auth", "application-default", "login"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {"ok": True}


def _access_token() -> str:
    if _token.get("exp", 0) > time.time() + 60:
        return _token["value"]
    cred = json.loads(ADC_PATH.read_text(encoding="utf-8"))
    if cred.get("type") != "authorized_user":
        raise RuntimeError("凭据类型不是个人账号登录，请重新运行 gcloud auth application-default login")
    body = urllib.parse.urlencode({"client_id": cred["client_id"], "client_secret": cred["client_secret"],
                                   "refresh_token": cred["refresh_token"], "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=20) as r:
        tok = json.load(r)
    _token.update(value=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3600)))
    return _token["value"]


def _call(url: str, project: str = "", body: dict | None = None) -> dict:
    """project = 计配额的项目（这个项目里要启用了对应的 API）；不传就记在 gcloud 自己的项目上。"""
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json",
               "User-Agent": f"MyAIBot/{__version__}"}
    if project:
        headers["x-goog-user-project"] = project
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def _projects(cfg: dict) -> list[str]:
    """结算账号下的所有项目（AI Studio 每建一个项目就多一个 gen-lang-client-…）。"""
    account = cfg.get("billing_account", "")
    key = ("projects", account)
    if key in _cache:
        return _cache[key]
    found = []
    if account:
        try:
            res = _call(f"https://cloudbilling.googleapis.com/v1/billingAccounts/{account}/projects")
            found = [p["projectId"] for p in res.get("projectBillingInfo", []) if p.get("billingEnabled")]
        except Exception:
            pass
    for p in [cfg.get("project")] + list(cfg.get("projects") or []):
        if p and p not in found:
            found.append(p)
    _cache[key] = found
    return found


def _enable_monitoring(project: str) -> None:
    for svc in ("serviceusage.googleapis.com", "monitoring.googleapis.com"):
        _call(f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{svc}:enable", body={})


# ------------------------------------------------------------------ 汇率（欧洲央行每日参考汇率，经 frankfurter.dev）
def _usd_to(cur: str, cfg: dict) -> float:
    if cur == "USD":
        return 1.0
    if cfg.get("usd_rate"):
        return float(cfg["usd_rate"])
    key = ("fx", cur, date.today())
    if key not in _cache:
        rate = 0.0
        try:  # frankfurter 提供欧洲央行每日参考汇率
            req = urllib.request.Request(f"https://api.frankfurter.dev/v1/latest?base=USD&symbols={cur}",
                                         headers={"User-Agent": f"MyAIBot/{__version__}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rate = float(json.load(r)["rates"][cur])
        except Exception:
            rate = {"EUR": 0.91, "GBP": 0.79, "CNY": 7.2}.get(cur, 0.0)
        _cache[key] = rate
    return _cache[key] or 1.0


# ------------------------------------------------------------------ Monitoring：每小时每个模型的 token
def _series(project: str, flt: str, start: datetime, end: datetime) -> list[dict]:
    out, page = [], ""
    while True:
        q = {"filter": flt, "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "aggregation.alignmentPeriod": f"{STEP.seconds}s", "aggregation.perSeriesAligner": "ALIGN_SUM", "pageSize": "1000"}
        if page:
            q["pageToken"] = page
        res = _call(f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries?{urllib.parse.urlencode(q)}", project)
        out += res.get("timeSeries", [])
        page = res.get("nextPageToken", "")
        if not page:
            return out


def _hourly(project: str, start: datetime, end: datetime) -> dict:
    """{(时间格结束时刻, 模型): {"in":…, "text":…, "image":…}}"""
    rows: dict = {}

    def add(s: dict, kind: str, keep_max: bool) -> None:
        model = s["metric"]["labels"].get("model", "?")
        for p in s["points"]:
            v = int(p["value"].get("int64Value", 0))
            if not v:
                continue
            t = datetime.fromisoformat(p["interval"]["endTime"].replace("Z", "+00:00"))
            r = rows.setdefault((t, model), {"in": 0, "text": 0, "image": 0})
            # 输入 token 同时记在「每分钟」「每用户每分钟」几个配额上，取最大的一份，不要重复加
            r[kind] = max(r[kind], v) if keep_max else r[kind] + v

    for s in _series(project, f'metric.type="{OUT_METRIC}"', start, end):
        add(s, "image" if s["metric"]["labels"].get("output_modality") == "image" else "text", False)
    for metric in IN_METRICS:  # 一次只能查一个指标
        try:
            for s in _series(project, f'metric.type="{metric}"', start, end):
                add(s, "in", True)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise
    return rows


def _price(model: str, day: date) -> tuple[tuple, bool]:
    key = max((k for k in PRICES if model.startswith(k)), key=len, default="")
    if not key:
        return FALLBACK, False
    rows = [r for r in PRICES[key] if date.fromisoformat(r[0]) <= day] or PRICES[key][:1]
    return rows[-1][1:], True


def _usd(model: str, day: date, t: dict) -> float:
    (p_in, p_text, p_img), _ = _price(model, day)
    return (t["in"] * p_in + t["text"] * p_text + t["image"] * (p_img or p_text)) / 1e6


# ------------------------------------------------------------------ BigQuery 账单导出（可选，有就用真实抵扣额）
BQ = "https://bigquery.googleapis.com/bigquery/v2/projects/{project}"


def _find_table(projects: list[str], account: str) -> tuple[str, str]:
    want = ("gcp_billing_export_v1_" + account.replace("-", "_")).upper()
    for project in projects:
        base = BQ.format(project=project)
        try:
            datasets = _call(f"{base}/datasets?all=true", project).get("datasets", [])
        except urllib.error.HTTPError:
            continue  # 这个项目没启用 BigQuery
        for ds in datasets:
            ds_id = ds["datasetReference"]["datasetId"]
            for t in _call(f"{base}/datasets/{ds_id}/tables?maxResults=1000", project).get("tables", []):
                name = t["tableReference"]["tableId"]
                if name.upper().startswith(want):
                    return project, f"`{project}.{ds_id}.{name}`"
    return "", ""


def _query(project: str, sql: str) -> list[dict]:
    res = _call(f"{BQ.format(project=project)}/queries", project, {"query": sql, "useLegacySql": False, "timeoutMs": 30000})
    while not res.get("jobComplete"):
        job = res["jobReference"]
        time.sleep(1)
        res = _call(f"{BQ.format(project=project)}/queries/{job['jobId']}?location={job.get('location', '')}&timeoutMs=30000", project)
    names = [f["name"] for f in res.get("schema", {}).get("fields", [])]
    return [{n: c["v"] for n, c in zip(names, row["f"])} for row in res.get("rows", [])]


def _bill_cutoff() -> datetime:
    """账单核对到「昨天 0 点」：再早的账单基本不会再变，之后的（昨天、今天）用估算。"""
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=1)).astimezone(timezone.utc)


def _billed(cfg: dict, projects: list[str], since: datetime) -> tuple[float, datetime | None, datetime | None]:
    """账单导出里 [max(since, 导出开始), 截止时刻) 实际抵扣掉的赠金和这段的起止；没开导出就是 (0, None, None)。
    一天只查一次，结果存在 data/gemini_bill.json。"""
    account = cfg.get("billing_account", "")
    if not account:
        return 0.0, None, None
    cutoff = _bill_cutoff()
    if cutoff <= since:  # 快照比截止时刻还新，账单里没有要扣的
        return 0.0, None, None
    try:
        saved = json.loads(BILL_PATH.read_text(encoding="utf-8"))
        if saved.get("cutoff") == cutoff.isoformat() and saved.get("since") == since.isoformat():
            return float(saved["used"]), datetime.fromisoformat(saved["start"]), cutoff
    except (OSError, ValueError, KeyError):
        pass
    key = ("table", account)
    if not _cache.get(key) or _cache[key][0] < time.time() - 3600:  # 还没开导出：一小时找一次表
        _cache[key] = (time.time(), *_find_table(projects, account))
    _, project, table = _cache[key]
    if not table:
        return 0.0, None, None
    since_s, cut_s = since.strftime("%Y-%m-%d %H:%M:%S+00"), cutoff.strftime("%Y-%m-%d %H:%M:%S+00")
    rows = _query(project, f"""
        SELECT -SUM(IF(usage_start_time >= TIMESTAMP("{since_s}") AND usage_start_time < TIMESTAMP("{cut_s}"),
                       IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0), 0)) AS used,
               MIN(usage_start_time) AS first, MAX(usage_end_time) AS last
        FROM {table}""")
    if not rows or not rows[0].get("last"):
        return 0.0, None, None
    first = datetime.fromtimestamp(float(rows[0]["first"]), timezone.utc)
    last = datetime.fromtimestamp(float(rows[0]["last"]), timezone.utc)
    start = max(since, first.replace(minute=0, second=0, microsecond=0))  # 导出是后来才开的：账单从导出开始算
    if last < cutoff or start >= cutoff:  # 还没导出到截止时刻：先全部用估算，明天再核对
        return 0.0, None, None
    used = float(rows[0]["used"] or 0)
    BILL_PATH.parent.mkdir(exist_ok=True)
    BILL_PATH.write_text(json.dumps({"cutoff": cutoff.isoformat(), "since": since.isoformat(), "start": start.isoformat(),
                                     "used": used}), encoding="utf-8")
    return used, start, cutoff


# ------------------------------------------------------------------ 赠金
def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v).strip())


def _as_of(v) -> datetime:
    """config 里的本地时间 → UTC。"""
    if isinstance(v, datetime):
        t = v
    elif isinstance(v, date):
        t = datetime(v.year, v.month, v.day)
    else:
        t = datetime.fromisoformat(str(v).strip())
    return (t if t.tzinfo else t.astimezone()).astimezone(timezone.utc)


def _credits(cfg: dict, used_since: float) -> dict | None:
    """各笔赠金的剩余：快照剩余减去之后的花费（先扣最早到期的），过期的不算。"""
    rows = []
    for c in cfg.get("credits") or []:
        try:
            rows.append({"remaining": float(c["remaining"]), "amount": float(c.get("amount", c["remaining"])),
                         "end": _date(c["end"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    today = date.today()
    rows.sort(key=lambda r: r["end"])
    left = used_since
    for r in rows:
        if r["end"] < today:
            continue
        take = min(left, r["remaining"])
        r["remaining"] -= take
        left -= take
    live = [r for r in rows if r["end"] >= today]
    remaining = sum(r["remaining"] for r in live)
    amount = sum(r["amount"] for r in live)
    nxt = next((r for r in live if r["remaining"] > 0.005), None)
    return {"remaining": round(remaining, 2), "amount": round(amount, 2), "used": round(amount - remaining, 2),
            "over": round(max(left, 0), 2),
            "next_end": f"{nxt['end'].year}年{nxt['end'].month}月{nxt['end'].day}日" if nxt else "",
            "next_left": round(nxt["remaining"], 2) if nxt else 0,
            "next_days": (nxt["end"] - today).days if nxt else 0,
            "items": [{"end": f"{r['end'].year}/{r['end'].month}/{r['end'].day}", "remaining": round(r["remaining"], 2),
                       "amount": r["amount"], "expired": r["end"] < today} for r in rows]}


# ------------------------------------------------------------------ 汇总
def usage(cfg: dict) -> dict:
    """近 7 天每天的 token 和折合金额（按模型）、本月合计、赠金剩余。cfg 是 config.yaml 里的 gemini 一节。"""
    if not cfg or not (cfg.get("project") or cfg.get("billing_account")):
        return {"need_config": True}
    currency = cfg.get("currency", "EUR")
    account = cfg.get("billing_account", "")
    if not ADC_PATH.exists():
        return {"need_login": True, "has_gcloud": bool(gcloud_path()), "currency": currency, "credit": _credits(cfg, 0.0)}
    # 结束时刻取下一个整 10 分钟，时间格就对齐到整点、整 10 分
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now += timedelta(minutes=-now.minute % 10 or 10)
    local_today = datetime.now().date()
    week0 = local_today - timedelta(days=6)
    month0 = local_today.replace(day=1)
    as_of = _as_of(cfg.get("as_of") or datetime.now())
    first_local = min(week0, month0)
    start = min(datetime(first_local.year, first_local.month, first_local.day).astimezone().astimezone(timezone.utc), as_of)
    start = max(start, now - RETENTION + timedelta(hours=1))
    start -= timedelta(minutes=start.minute % 10, seconds=start.second, microseconds=start.microsecond)
    warn = ""
    try:
        with _lock:
            projects = _projects(cfg)
            hourly: dict = {}
            for p in projects:
                try:
                    rows = _hourly(p, start, now)
                except urllib.error.HTTPError as e:
                    if e.code != 403:
                        raise
                    _enable_monitoring(p)  # 新建的项目还没启用 Monitoring：启用一下，下次刷新就有数据
                    warn = "刚为新项目启用了用量统计，几分钟后再刷新"
                    continue
                for k, v in rows.items():
                    r = hourly.setdefault(k, {"in": 0, "text": 0, "image": 0})
                    for f in r:
                        r[f] += v[f]
            try:
                billed, bill_from, billed_until = _billed(cfg, projects, as_of)
            except Exception:
                billed, bill_from, billed_until = 0.0, None, None
    except (FileNotFoundError, KeyError):
        return {"need_login": True, "has_gcloud": bool(gcloud_path()), "currency": currency, "credit": _credits(cfg, 0.0)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {})
            detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
        except Exception:
            pass
        if e.code in (400, 401) and ("invalid_grant" in detail or e.code == 401 or "invalid_grant" in str(e.reason)):
            return {"need_login": True, "expired": True, "has_gcloud": bool(gcloud_path()), "currency": currency,
                    "credit": _credits(cfg, 0.0)}
        return {"error": f"读取 Google 用量失败：HTTP {e.code} {detail[:200]}", "currency": currency, "credit": _credits(cfg, 0.0)}
    except Exception as e:
        return {"error": f"读取 Google 用量失败：{e}", "currency": currency, "credit": _credits(cfg, 0.0)}

    rate = _usd_to(currency, cfg)
    days = {week0 + timedelta(days=i): {} for i in range(7)}
    week: dict[str, dict] = {}
    month = {"cost": 0.0, "tokens": 0}
    since_cut = 0.0  # as_of 之后、账单没覆盖到的时间里的估算花费
    pre = 0.0  # 其中 as_of ～ 账单开始之间的那段（开导出之前），算一次存下来，不受 Monitoring 只保留 6 周的影响
    unknown = set()
    for (t, model), tok in hourly.items():
        slot = t - STEP
        day = slot.astimezone().date()
        cost = _usd(model, day, tok) * rate
        if not _price(model, day)[1]:
            unknown.add(model)
        total = tok["in"] + tok["text"] + tok["image"]
        mid = slot + STEP / 2  # 这一格大半落在哪段时间里就算哪段
        if mid >= as_of and not (bill_from and bill_from <= mid < billed_until):
            since_cut += cost
            if bill_from and mid < bill_from:
                pre += cost
        if day >= month0:
            month["cost"] += cost
            month["tokens"] += total
        if day in days:
            m = days[day].setdefault(model, {"in": 0, "text": 0, "image": 0, "cost": 0.0})
            w = week.setdefault(model, {"in": 0, "text": 0, "image": 0, "cost": 0.0})
            for d in (m, w):
                for f in ("in", "text", "image"):
                    d[f] += tok[f]
                d["cost"] += cost

    def images(model: str, tok: dict) -> int:
        per = next((v for k, v in IMAGE_TOKENS.items() if model.startswith(k)), 1120)
        return round(tok["image"] / per)

    daily = []
    for i, (d, models) in enumerate(days.items()):
        daily.append({"label": "今天" if d == local_today else f"{d.month}/{d.day}",
                      "cost": round(sum(m["cost"] for m in models.values()), 4),
                      "tokens": sum(m["in"] + m["text"] + m["image"] for m in models.values()),
                      "images": sum(images(n, m) for n, m in models.items()),
                      "models": sorted(([n, round(m["cost"], 4), m["in"] + m["text"] + m["image"], images(n, m)]
                                        for n, m in models.items()), key=lambda x: -x[1])})
    today = daily[-1]
    if bill_from:
        try:
            saved = json.loads(PRE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("as_of") == as_of.isoformat() and saved.get("start") == bill_from.isoformat():
            since_cut += float(saved["pre"]) - pre
        else:
            PRE_PATH.write_text(json.dumps({"as_of": as_of.isoformat(), "start": bill_from.isoformat(), "pre": pre}),
                                encoding="utf-8")
    used = billed + since_cut
    return {
        "currency": currency, "rate": round(rate, 4), "warn": warn, "projects": len(projects),
        "today": {"cost": today["cost"], "tokens": today["tokens"], "images": today["images"]},
        "week": {"cost": round(sum(d["cost"] for d in daily), 4), "tokens": sum(d["tokens"] for d in daily),
                 "images": sum(d["images"] for d in daily)},
        "month": {"cost": round(month["cost"], 4), "tokens": month["tokens"]},
        "daily": daily,
        "models": sorted(({"name": n, "in": m["in"], "text": m["text"], "image": m["image"], "images": images(n, m),
                           "cost": round(m["cost"], 4), "priced": n not in unknown} for n, m in week.items()),
                         key=lambda x: -x["cost"]),
        "credit": _credits(cfg, used),
        "used_billed": round(billed, 2), "used_est": round(since_cut, 2),
        "billed_until": billed_until.astimezone().strftime("%m-%d %H:%M") if billed_until else "",
        "has_export": billed_until is not None,
        "as_of": as_of.astimezone().strftime("%m-%d %H:%M"),
        "stale": as_of < now - RETENTION and bill_from is None,
    }
