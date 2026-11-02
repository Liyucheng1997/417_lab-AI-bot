from claude_agent_sdk import create_sdk_mcp_server

from .desktop import ALL_TOOLS


def desktop_server():
    """进程内 MCP 服务器，工具名形如 mcp__desktop__screenshot。"""
    return create_sdk_mcp_server(name="desktop", version="1.0.0", tools=ALL_TOOLS)
