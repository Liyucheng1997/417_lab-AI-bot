"""判断一次工具调用的风险等级，决定直接执行还是先问主人。"""
from __future__ import annotations

import re

# 只看不改的工具：任何策略下都直接放行
READ_ONLY_TOOLS = {
    "Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "TodoWrite", "TaskOutput",
    "BashOutput", "Task", "Agent", "Skill", "ToolSearch", "ListMcpResourcesTool",
    "ReadMcpResourceTool", "EnterPlanMode", "ExitPlanMode",
    "mcp__desktop__screenshot", "mcp__desktop__screen_info", "mcp__desktop__list_windows",
    "mcp__desktop__inspect_window", "mcp__desktop__get_clipboard", "mcp__desktop__wait",
    "mcp__desktop__list_processes", "mcp__desktop__cursor_position", "mcp__desktop__list_apps",
    "mcp__desktop__find_files", "mcp__desktop__claude_usage",
    "mcp__phone__status", "mcp__phone__screenshot", "mcp__phone__inspect", "mcp__phone__list_apps",
}

# 操作鼠标键盘、打开软件等：smart 下放行，strict 下询问
DESKTOP_ACTION_TOOLS = {
    "mcp__desktop__click", "mcp__desktop__move_mouse", "mcp__desktop__drag",
    "mcp__desktop__scroll", "mcp__desktop__type_text", "mcp__desktop__press_keys",
    "mcp__desktop__focus_window", "mcp__desktop__open", "mcp__desktop__set_clipboard",
    "mcp__desktop__click_element",
    "mcp__phone__tap", "mcp__phone__tap_element", "mcp__phone__swipe", "mcp__phone__type_text",
    "mcp__phone__press_key", "mcp__phone__open_app", "mcp__phone__mirror",
}

# 手机上的 adb shell 命令：这些会删数据、卸载、恢复出厂、重启、改系统设置，要先问
DANGEROUS_PHONE_SHELL = [
    (r"\brm\s", "删除手机上的文件"),
    (r"\bpm\s+(uninstall|clear|disable|disable-user|reset-permissions)\b", "卸载 App / 清空 App 数据"),
    (r"\b(reboot|wipe|recovery|factory|fastboot)\b", "重启 / 恢复出厂"),
    (r"\bsettings\s+(put|delete|reset)\b", "修改手机系统设置"),
    (r"\b(svc|cmd\s+(wifi|connectivity|bluetooth_manager))\b", "开关网络 / 蓝牙"),
    (r"\b(dd|mkfs|format)\b", "写入磁盘"),
    (r"\bam\s+(force-stop|kill)\b", "结束 App"),
    (r"\bcontent\s+(insert|update|delete)\b", "改写手机数据库（短信、联系人等）"),
    (r"\bservice\s+call\b", "调用系统服务"),
]

SHELL_TOOLS = {"Bash", "PowerShell"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# 高危命令（大小写不敏感）
DANGEROUS_SHELL = [
    (r"\brm\s+(-\w*\s+)*-\w*[rf]", "递归/强制删除文件"),
    (r"\b(Remove-Item|rmdir|rd|del|erase)\b", "删除文件或目录"),
    (r"\bri\s", "删除文件或目录"),
    (r"\b(Clear-RecycleBin|cipher\s+/w|sdelete)\b", "清空回收站/彻底擦除"),
    (r"\bformat(\.com)?\s+[A-Za-z]:", "格式化磁盘"),
    (r"\b(diskpart|bcdedit|mountvol|Format-Volume|Clear-Disk|Initialize-Disk)\b", "磁盘/引导操作"),
    (r"\b(shutdown|Stop-Computer|Restart-Computer|logoff)\b", "关机/重启/注销"),
    (r"\breg(\.exe)?\s+(add|delete|import|restore)\b", "修改注册表"),
    (r"\b(Set-ItemProperty|New-ItemProperty|Remove-ItemProperty)\b.*\b(HK|Registry::)", "修改注册表"),
    (r"\b(net\s+user|net\s+localgroup|New-LocalUser|Set-LocalUser|Add-LocalGroupMember)\b", "修改系统账户"),
    (r"\b(Set-ExecutionPolicy|Set-MpPreference|netsh|New-NetFirewallRule|Set-NetFirewall)", "修改安全/网络设置"),
    (r"\b(takeown|icacls|cacls)\b", "修改文件权限"),
    (r"\b(sc(\.exe)?\s+(delete|config|stop)|Stop-Service|Set-Service)\b", "修改系统服务"),
    (r"\b(taskkill|Stop-Process|kill)\b", "结束进程"),
    (r"\b(winget|choco|scoop)\s+(install|uninstall|upgrade|remove)", "安装/卸载软件"),
    (r"\b(pip|npm|pnpm|yarn|uv)\s+(uninstall|remove)\b", "卸载依赖包"),
    (r"msiexec", "安装/卸载软件"),
    (r"\bgit\s+(push\s+.*--force|push\s+-f|reset\s+--hard|clean\s+-\w*f)", "不可逆的 git 操作"),
    (r"\b(Invoke-WebRequest|iwr|curl|wget)\b.*\|\s*(iex|Invoke-Expression|sh|bash|powershell)", "下载并直接执行脚本"),
    (r"\b(schtasks\s+/(create|delete)|Register-ScheduledTask|Unregister-ScheduledTask)", "修改计划任务"),
    (r"\bvssadmin\b|\bwbadmin\b", "备份/卷影操作"),
    (r"(^|[\s;|&])>\s*[A-Za-z]:\\(Windows|Program Files)", "写入系统目录"),
]

PROTECTED_PATHS = re.compile(r"^[A-Za-z]:[\\/](Windows|Program Files|Program Files \(x86\)|ProgramData)([\\/]|$)", re.I)

# 外部 MCP 工具：名字里有写类动词就询问，只有读类动词就放行
MCP_WRITE_VERBS = re.compile(
    r"(^|_)(send|delete|remove|trash|create|update|write|edit|post|publish|move|share|merge|add|set|"
    r"upload|pay|buy|order|transfer|reply|forward|archive|batch|manage|rename|insert|drop)(_|$)", re.I)
MCP_READ_VERBS = re.compile(
    r"(^|_)(get|list|search|read|fetch|query|find|lookup|status|describe|view|show|check)(_|$)", re.I)


def classify(tool: str, args: dict, policy: str) -> tuple[bool, str]:
    """返回 (是否需要确认, 原因)。"""
    if policy == "full":
        return False, ""
    if tool in READ_ONLY_TOOLS:
        return False, ""

    if policy == "strict":
        return True, "每步确认模式"

    # ---- smart ----
    if tool in DESKTOP_ACTION_TOOLS:
        return False, ""
    if tool in SHELL_TOOLS:
        cmd = str(args.get("command", ""))
        for pattern, reason in DANGEROUS_SHELL:
            if re.search(pattern, cmd, re.I):
                return True, reason
        return False, ""
    if tool == "mcp__phone__shell":
        cmd = str(args.get("command", ""))
        for pattern, reason in DANGEROUS_PHONE_SHELL:
            if re.search(pattern, cmd, re.I):
                return True, reason
        return False, ""
    if tool in WRITE_TOOLS:
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        if PROTECTED_PATHS.match(path):
            return True, "修改系统目录下的文件"
        return False, ""
    if tool.startswith("mcp__"):
        action = tool.split("__", 2)[-1]
        if not MCP_WRITE_VERBS.search(action) and MCP_READ_VERBS.search(action):
            return False, ""
        return True, "调用外部工具执行写操作"
    return False, ""


def describe(tool: str, args: dict) -> str:
    """一行人话描述，用于确认卡片。"""
    if tool in SHELL_TOOLS or tool == "mcp__phone__shell":
        return str(args.get("command", ""))
    if tool in WRITE_TOOLS:
        return str(args.get("file_path") or args.get("notebook_path") or "")
    short = {k: (str(v)[:200]) for k, v in args.items()}
    return ", ".join(f"{k}={v}" for k, v in short.items())
