"""终端版：python -m bot.cli [一句话任务]。用于调试，或在没有界面的情况下使用。"""
from __future__ import annotations

import asyncio
import sys

from .agent import BotSession
from .config import Config

DIM, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


def emit(ev: dict) -> None:
    t = ev["type"]
    if t == "text_delta":
        print(ev["text"], end="", flush=True)
    elif t == "text":
        print(ev["text"])
    elif t == "tool_use":
        print(f"\n{CYAN}⚙ {ev['summary']}{RESET}")
    elif t == "tool_result":
        tag = f"{RED}✗" if ev["is_error"] else f"{DIM}↳"
        extra = f" [+{len(ev['images'])} 张截图]" if ev["images"] else ""
        print(f"{tag} {ev['text'][:300].strip()}{extra}{RESET}")
    elif t == "session":
        print(f"{DIM}[会话 {ev['session_id']} · 模型 {ev['model']} · 认证 {ev['auth']}]{RESET}")
    elif t == "result":
        status = "完成" if ev["ok"] else f"失败：{ev['error']}"
        print(f"\n{DIM}[{status} · {ev['duration']:.1f}s · {ev['turns']} 轮]{RESET}")
    elif t == "rate_limit" and ev["status"] != "allowed":
        print(f"{YELLOW}[订阅额度提醒：{ev['kind']} 已用 {ev['utilization']}]{RESET}")
    elif t == "error":
        print(f"{RED}{ev['message']}{RESET}")


async def ask(req: dict) -> str:
    print(f"\n{YELLOW}⚠ 需要确认（{req['reason']}）：{req['short']}\n  {req['detail']}{RESET}")
    ans = (await asyncio.to_thread(input, "允许吗？[y]是 / [a]本次会话都允许 / [n]否：")).strip().lower()
    return {"y": "allow", "a": "always"}.get(ans, "deny")


async def main() -> None:
    bot = BotSession(Config.load(), emit, ask)
    await bot.start()
    once = " ".join(sys.argv[1:]).strip()
    try:
        if once:
            await bot.send(once)
            return
        print("我的 AI Bot（终端版）。输入任务，/new 新对话，/exit 退出。")
        while True:
            text = (await asyncio.to_thread(input, "\n你 > ")).strip()
            if text in ("/exit", "/quit"):
                break
            if text == "/new":
                await bot.start()
                print("已开始新对话")
            elif text:
                await bot.send(text)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
