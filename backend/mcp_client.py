"""MCP 客户端封装 + 异常降级（开发文档 6.2 / 风险 R7）。

设计要点
--------
1. **加载失败绝不中断主链路**：任何异常都降级为空工具列表，把原因记进 state，
   由 /api/health 如实暴露（对齐 R7：MCP 工具加载失败影响主链路 中/中）。
2. **解释器与脚本都用绝对路径**（开发文档 D7 要点）：stdio 子进程不继承 cwd 假设。
3. 加载带超时：server 起不来时必须能及时放弃，不能把应用启动卡死。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .setting import get_settings

logger = logging.getLogger(__name__)


@dataclass
class McpState:
    """MCP 加载状态（给 /api/health 用）。"""

    enabled: bool = False
    ok: bool = False
    degraded: bool = False
    tools: list[str] = field(default_factory=list)
    detail: str = "尚未加载"
    _tools: list[Any] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "tools": list(self.tools),
            "detail": self.detail,
        }


#: 进程级状态
state = McpState()


def server_config() -> dict[str, dict[str, Any]]:
    """构造 MultiServerMCPClient 的连接配置（stdio 传输）。"""

    settings = get_settings()
    return {
        "ticket": {
            "command": sys.executable,
            "args": [str(Path(settings.mcp_ticket_server).resolve())],
            "transport": "stdio",
            # 子进程不需要继承业务密钥；只保留解释器需要的最小环境
            "env": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        }
    }


async def load_tools() -> list[Any]:
    """加载 MCP 工具；**任何失败都降级为空列表**，并记录原因。"""

    global state
    settings = get_settings()

    state.enabled = bool(settings.mcp_enabled)
    if not settings.mcp_enabled:
        state.ok, state.degraded = False, True
        state.detail = "MCP 已在配置中关闭（mcp_enabled=False）"
        state.tools, state._tools = [], []
        logger.info("MCP 已关闭，跳过加载")
        return []

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        state.ok, state.degraded = False, True
        state.detail = f"未安装 langchain-mcp-adapters（{exc}）"
        state.tools, state._tools = [], []
        logger.warning("MCP 客户端库缺失，已降级为空工具列表：%s", exc)
        return []

    try:
        client = MultiServerMCPClient(server_config())
        tools = await asyncio.wait_for(client.get_tools(), timeout=settings.mcp_load_timeout_s)
    except asyncio.TimeoutError:
        state.ok, state.degraded = False, True
        state.detail = f"MCP 加载超时（>{settings.mcp_load_timeout_s:g}s）"
        state.tools, state._tools = [], []
        logger.warning("MCP 加载超时，已降级为空工具列表")
        return []
    except Exception as exc:  # noqa: BLE001 - R7：绝不能因为 MCP 失败而中断主链路
        state.ok, state.degraded = False, True
        state.detail = f"MCP 加载失败：{exc}"
        state.tools, state._tools = [], []
        logger.warning("MCP 加载失败，已降级为空工具列表：%s", exc)
        return []

    names = [getattr(tool, "name", str(tool)) for tool in tools]
    state.ok, state.degraded = bool(tools), False
    state.tools, state._tools = names, list(tools)
    state.detail = (
        f"已连接 {settings.mcp_ticket_server}（{len(names)} 个工具）"
        if tools
        else "MCP server 已连接但未暴露任何工具"
    )
    logger.info("MCP 工具加载完成：%s", names or "（空）")
    return list(tools)


def loaded_tools() -> list[Any]:
    """已加载的 MCP 工具（未加载时为空列表）。"""

    return list(state._tools)


def status() -> dict[str, Any]:
    """给 /api/health 的状态字典。"""

    return state.as_dict()


__all__ = ["McpState", "load_tools", "loaded_tools", "server_config", "state", "status"]
