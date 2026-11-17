"""Bot 操作主人服务器的工具（进程内 MCP 服务器 server，工具名形如 mcp__server__run）。

服务器是主人在面板「手机 → 服务器」页添加的（SSH，密钥或密码），这里直接复用那边的连接。
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import server


def _text(s: str, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


def _pick(name: str) -> dict:
    servers = server.load()
    if not servers:
        raise RuntimeError("主人还没添加服务器。请主人点面板标题栏的「手机」按钮 → 「服务器」页添加（SSH 地址、用户名、密钥或密码）")
    s = server.get(name)
    if not s:
        names = "、".join(x["name"] for x in servers)
        raise RuntimeError(f"有好几台服务器，请用 server 参数指定名字：{names}" if not name else f"没有叫「{name}」的服务器，现有：{names}")
    return s


SERVER = Annotated[str, "服务器名字（面板里显示的名字、地址或 ~/.ssh/config 里的别名）；只有一台时留空"]


@tool("status", "查看主人的服务器：系统、运行时间、CPU、内存、磁盘、网速、最忙的进程、Docker 容器、失败的服务。"
      "server 留空 = 全部服务器。", {"server": str})
async def status(args):
    servers = server.load()
    if not servers:
        return _text("主人还没添加服务器（面板标题栏「手机」按钮 → 「服务器」页里添加）。")
    name = str(args.get("server") or "").strip()
    targets = [_pick(name)] if name else servers

    async def one(s):
        def work():
            c = server.conn(s["id"])
            code, out = c.run(server.STATUS_SCRIPT, timeout=20)
            return {"state": "ok", "stats": server.parse_stats(out)}
        try:
            st = await asyncio.to_thread(work)
            server.STATS[s["id"]] = {**st, "at": __import__("time").time()}
        except Exception as e:
            st = {"state": "error", "error": server.friendly_error(e)}
        return server.summary(s, st)

    texts = await asyncio.gather(*(one(s) for s in targets))
    return _text("\n\n".join(texts))


@tool("run", "在主人的服务器上通过 SSH 执行一条 shell 命令（非交互，最长 timeout 秒，默认 30），返回输出和退出码。"
      "密码登录的服务器 sudo 会自动填密码。需要交互或长时间运行的（vim、top、tail -f）别用这个，让主人点「终端」。",
      {"command": str, "server": SERVER, "timeout": int})
async def run(args):
    s = _pick(str(args.get("server") or "").strip())
    timeout = max(1, min(int(args.get("timeout") or 30), 600))
    r = await asyncio.to_thread(server.run, s["id"], str(args["command"]), timeout)
    if not r["ok"]:
        return _text(f"{s['name']}：{r['error']}", error=True)
    out = r["output"].strip() or "（没有输出）"
    if len(out) > 12000:
        out = "…（前面省略）\n" + out[-12000:]
    return _text(f"[{s['name']} 退出码 {r['code']}]\n{out}", error=r["code"] != 0)


@tool("terminal", "在电脑上打开一个连到服务器的 SSH 终端窗口（Windows Terminal），给主人自己操作。", {"server": SERVER})
async def terminal(args):
    s = _pick(str(args.get("server") or "").strip())
    r = server.open_terminal(s["id"])
    return _text(f"已打开 {s['name']} 的 SSH 终端" + ("（要在终端里输一次密码）" if r.get("password") else "")
                 if r["ok"] else r["error"], error=not r["ok"])


ALL_TOOLS = [status, run, terminal]


def server_server():
    return create_sdk_mcp_server(name="server", version="1.0.0", tools=ALL_TOOLS)
