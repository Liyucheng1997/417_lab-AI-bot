"""Bot 引擎：包装 ClaudeSDKClient，把消息流转换成界面事件，并在高危操作前请求确认。

认证走本机 Claude Code 的订阅登录（claude login），不使用 API Key。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import platform
import shutil
import subprocess
import uuid
from typing import Any, Awaitable, Callable

from . import __version__

# 必须在 SDK 启动 CLI 之前移除，否则会改用 API Key 计费
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.setdefault("CLAUDE_AGENT_SDK_CLIENT_APP", f"my-ai-bot/{__version__}")


def _hide_child_consoles() -> None:
    """exe 没有控制台时，Claude CLI 等命令行子进程会各自弹出黑框；统一加 CREATE_NO_WINDOW。

    CLI 自己再开的 PowerShell 会继承这个看不见的控制台，也不会闪窗。
    """
    import ctypes
    if os.name != "nt" or ctypes.windll.kernel32.GetConsoleWindow():
        return
    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0)
        if not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE):
            kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init


_hide_child_consoles()

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
    PermissionResultDeny, RateLimitEvent, ResultMessage, StreamEvent, SystemMessage,
    TextBlock, ThinkingBlock, ToolPermissionContext, ToolResultBlock, ToolUseBlock, UserMessage,
)

from . import permissions  # noqa: E402
from .config import DATA_DIR, MEMORY_PATH, Config, read_memory  # noqa: E402
from .tools import desktop_server  # noqa: E402

Emit = Callable[[dict], Any]
# 返回 "allow" / "always" / "deny"
AskPermission = Callable[[dict], Awaitable[str]]

SYSTEM_PROMPT = """
# 你的身份
你是主人的私人 AI 助手「Bot」，常驻在主人的 Windows 电脑上（类似 Windows Copilot），可以像主人本人一样操作这台电脑。
现在时间：{now}；系统：{os}；用户目录：{home}。
始终用简体中文回答，语气简洁友好。

# 你能做什么
- 命令行：用 PowerShell / Bash 工具运行命令、脚本，管理文件，调用电脑上安装的任何命令行软件。
- 文件：用 Read / Write / Edit / Glob / Grep 读写搜索文件。
  找主人的文档、照片、视频、音乐、安装包、软件快捷方式时，先用 mcp__desktop__find_files（整机索引，毫秒出结果，按主人习惯排序）；
  代码文件、系统文件不在索引里，用 Glob。
- 上网：WebSearch / WebFetch。
- 操作图形界面（mcp__desktop__* 工具）：
  - screenshot 看屏幕；inspect_window 读取窗口控件（比看图找坐标更准）；
  - click / click_element / type_text / press_keys / scroll / drag 操作鼠标键盘；
  - open 按名称打开软件、文件或网址；list_apps 查已安装软件；focus_window 切换窗口。
- 主人在 Claude Code 里配置的 MCP 工具、技能也都能用。

# 做事方法
1. 能用命令行或文件工具完成的，优先用它们（快且准）；只有必须操作图形界面的软件时才用鼠标键盘。
2. 操作图形界面时：先截图或 inspect_window 看清状态 → 执行一步 → 再截图确认结果，不要盲点。
3. 屏幕右侧的「Bot」面板是你自己，你截图时它会自动隐身，截图里看不到它，不用管它。
4. 多步任务先简要说计划再执行；完成后用一两句话汇报结果。
5. 发消息、发邮件、付款、删除重要数据、对外发布等不可撤回的事，执行前先用文字向主人确认，得到同意再做。
   涉及密码、支付信息时，让主人自己输入。
6. 遇到需要主人决定的问题，直接用文字提问（不要使用 AskUserQuestion 工具）。
7. 主人用截图工具的「AI」按钮发来的截图，会以本地图片路径出现在消息里（如 …\\MyAIBot\\shot_*.png），用 Read 工具打开它再回答。

# 长期记忆
你的记忆文件在 {memory_path}。了解到主人的偏好、常用路径、习惯时，用 Edit 工具更新这个文件。当前内容：
<memory>
{memory}
</memory>
"""


def _system_prompt() -> dict:
    text = SYSTEM_PROMPT.format(
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M %A"),
        os=f"{platform.system()} {platform.release()} ({platform.version()})",
        home=os.path.expanduser("~"),
        memory_path=MEMORY_PATH,
        memory=read_memory().strip(),
    )
    return {"type": "preset", "preset": "claude_code", "append": text}


def _tool_summary(name: str, args: dict) -> str:
    short = name.replace("mcp__desktop__", "").replace("mcp__", "")
    detail = permissions.describe(name, args)
    return f"{short}  {detail}"[:300]


def _result_payload(block: ToolResultBlock) -> tuple[str, list[str]]:
    texts, images = [], []
    content = block.content
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif item.get("type") == "image":
                src = item.get("source") or {}
                data = src.get("data") or item.get("data")
                mime = src.get("media_type") or item.get("mimeType") or "image/png"
                if data:
                    images.append(f"data:{mime};base64,{data}")
    return "\n".join(texts)[:4000], images


class BotSession:
    def __init__(self, cfg: Config, emit: Emit, ask: AskPermission):
        self.cfg = cfg
        self.emit = emit
        self.ask = ask
        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None
        self.busy = False
        self._always: set[str] = set()
        self._streamed = False
        DATA_DIR.mkdir(exist_ok=True)
        self._log = open(DATA_DIR / "bot.log", "a", encoding="utf-8")

    # ------------------------------------------------------------ 生命周期
    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        mcp = {"desktop": desktop_server(), **(self.cfg.extra_mcp_servers or {})}
        return ClaudeAgentOptions(
            model=self.cfg.model,
            effort=self.cfg.effort,
            thinking={"type": "adaptive", "display": "summarized"},
            system_prompt=_system_prompt(),
            tools={"type": "preset", "preset": "claude_code"},
            disallowed_tools=["AskUserQuestion"],
            mcp_servers=mcp,
            setting_sources=["user", "project", "local"] if self.cfg.load_user_settings else [],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            cwd=self.cfg.work_dir,
            add_dirs=[str(DATA_DIR)],
            resume=resume,
            include_partial_messages=True,
            cli_path=shutil.which("claude"),
            stderr=self._on_stderr,
        )

    async def start(self, resume: str | None = None) -> None:
        await self.stop()
        self.emit({"type": "status", "state": "connecting"})
        self._always.clear()
        self.client = ClaudeSDKClient(self._options(resume))
        await self.client.connect()
        self.session_id = resume
        self.emit({"type": "status", "state": "idle"})

    async def stop(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def set_model(self, model: str) -> None:
        self.cfg.save_keys(model=model)
        if self.client:
            await self.client.set_model(model)

    async def interrupt(self) -> None:
        if self.client and self.busy:
            await self.client.interrupt()

    # ------------------------------------------------------------ 对话
    async def send(self, text: str) -> None:
        if self.busy:
            self.emit({"type": "error", "message": "上一个任务还在进行，可以先点「停止」。"})
            return
        if not self.client:
            await self.start()
        self.busy = True
        self.emit({"type": "status", "state": "thinking"})
        try:
            await self.client.query(text)
            async for msg in self.client.receive_response():
                self._handle(msg)
        except Exception as e:  # 连接断了等情况：提示并下次自动重连
            self.emit({"type": "error", "message": f"出错了：{e}"})
            self.client = None
        finally:
            self.busy = False
            self.emit({"type": "status", "state": "idle"})

    def _handle(self, msg: Any) -> None:
        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id:
                return
            ev = msg.event
            if ev.get("type") == "message_start":
                self._streamed = False
            elif ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    self._streamed = True
                    self.emit({"type": "text_delta", "text": delta.get("text", "")})
        elif isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id:  # 子代理内部过程不展示
                return
            if msg.error:
                self.emit({"type": "error", "message": f"模型返回错误：{msg.error}"})
            for b in msg.content:
                if isinstance(b, TextBlock) and not self._streamed:
                    self.emit({"type": "text", "text": b.text})
                elif isinstance(b, ThinkingBlock) and b.thinking.strip():
                    self.emit({"type": "thinking", "text": b.thinking})
                elif isinstance(b, ToolUseBlock):
                    self.emit({"type": "tool_use", "id": b.id, "name": b.name,
                               "summary": _tool_summary(b.name, b.input)})
        elif isinstance(msg, UserMessage):
            if msg.parent_tool_use_id or isinstance(msg.content, str):
                return
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    text, images = _result_payload(b)
                    self.emit({"type": "tool_result", "id": b.tool_use_id, "is_error": bool(b.is_error),
                               "text": text, "images": images})
        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self.session_id = msg.data.get("session_id") or self.session_id
                self.emit({"type": "session", "session_id": self.session_id,
                           "model": msg.data.get("model"),
                           "auth": msg.data.get("apiKeySource")})
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            self.emit({"type": "rate_limit", "status": info.status, "kind": info.rate_limit_type,
                       "utilization": info.utilization, "resets_at": info.resets_at})
        elif isinstance(msg, ResultMessage):
            self.session_id = msg.session_id
            err = None
            if msg.is_error:
                err = "; ".join(msg.errors or []) or msg.result or msg.subtype
            self.emit({"type": "result", "ok": not msg.is_error, "error": err,
                       "duration": msg.duration_ms / 1000, "turns": msg.num_turns,
                       "session_id": msg.session_id})

    # ------------------------------------------------------------ 权限
    async def _can_use_tool(self, tool: str, args: dict, ctx: ToolPermissionContext):
        need, reason = permissions.classify(tool, args, self.cfg.permission_policy)
        if not need or tool in self._always:
            return PermissionResultAllow()
        req = {"type": "permission", "id": uuid.uuid4().hex, "tool": tool,
               "short": tool.replace("mcp__desktop__", ""),
               "detail": permissions.describe(tool, args)[:1500], "reason": reason}
        decision = await self.ask(req)
        if decision == "always":
            self._always.add(tool)
        if decision in ("allow", "always"):
            return PermissionResultAllow()
        return PermissionResultDeny(message="主人拒绝了这个操作。请换个方式，或问主人想怎么做。")

    def _on_stderr(self, line: str) -> None:
        self._log.write(f"{dt.datetime.now():%H:%M:%S} {line.rstrip()}\n")
        self._log.flush()
