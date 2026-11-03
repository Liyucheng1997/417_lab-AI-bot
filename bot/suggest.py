"""欢迎页的「猜你想做」：根据最近的对话、长期记忆和当前时间，让 Haiku 猜几个主人接下来可能要做的事。

结果缓存在 data/suggestions.json，先显示缓存，后台刷新后再推给界面。用的是订阅额度（Haiku、低思考深度，很省）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from .config import DATA_DIR, Config, read_memory

CACHE = DATA_DIR / "suggestions.json"
WEEKDAYS = "一二三四五六日"
SCHEMA = {
    "type": "object",
    "properties": {
        "hint": {"type": "string", "description": "一句话：你猜主人此刻在忙什么或想做什么，口语化，不超过 24 个字"},
        "items": {
            "type": "array", "minItems": 4, "maxItems": 4,
            "items": {"type": "object", "properties": {
                "text": {"type": "string", "description": "发给 Bot 的完整请求，具体可执行，不超过 28 个字"},
                "icon": {"type": "string", "description": "一个贴切的 emoji"},
            }, "required": ["text", "icon"], "additionalProperties": False},
        },
    },
    "required": ["hint", "items"],
    "additionalProperties": False,
}
PROMPT = """你在为一个 Windows 桌面 AI 助手（能运行命令、读写文件、操作任意软件、上网）生成欢迎页上的 4 个快捷请求。
请根据主人最近的对话和长期记忆，猜他此刻最可能想让助手做的事：
- 至少 2 条延续最近在做的事（下一步、收尾、检查结果），不要原样重复已经做完的请求；
- 结合现在的时间（{now}）给 1 条应景的（比如早上看日程/邮件，晚上整理、备份、复盘）；
- 每条都要具体到能直接执行（写清对象、文件夹、软件名），用主人的口吻写成请求；
- 不要出现工具名、函数名等内部细节。

长期记忆：
{memory}

最近的对话（新的在前）：
{history}
"""


def load_cached() -> dict | None:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _history(cfg: Config) -> str:
    from claude_agent_sdk import list_sessions
    lines = []
    for s in list_sessions(directory=cfg.work_dir, limit=20):
        title = (s.custom_title or s.summary or s.first_prompt or "").strip().replace("\n", " ")
        if title:
            when = datetime.fromtimestamp(s.last_modified / 1000).strftime("%m-%d %H:%M")
            lines.append(f"- [{when}] {title[:80]}")
    return "\n".join(lines) or "（还没有对话）"


async def generate(cfg: Config) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    now = datetime.now()
    prompt = PROMPT.format(now=f"{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}",
                           memory=read_memory()[:2000], history=_history(cfg))
    opts = ClaudeAgentOptions(model="claude-haiku-5-5", effort="low", tools=[], max_turns=2,
                              setting_sources=[], cwd=str(DATA_DIR), output_format={"type": "json_schema", "schema": SCHEMA},
                              extra_args={"no-session-persistence": None})
    result = None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, ResultMessage):
            result = msg.structured_output
    if not result or len(result.get("items", [])) < 2:
        raise ValueError("没有得到建议")
    data = {"hint": result["hint"], "items": result["items"][:4], "ts": time.time()}
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
