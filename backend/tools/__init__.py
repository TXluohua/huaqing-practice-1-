"""本地工具 + MCP 工具聚合（开发文档 6.2 的 tools/__init__.py）。

职责
----
1. **登记本地工具**（平台侧 + RAG 侧）：检索、术语、图片识别、备件查询；
2. **加载 MCP 工具**（工单检索）并做异常降级（R7）；
3. 对外提供**统一调用入口** call_tool，屏蔽「本地 Python 函数」与
   「LangChain 工具（MCP）」两种不同的调用约定；
4. 把实际可用的工具名暴露给 augment 节点与 /api/health。

白名单为什么在这里做
--------------------
开发文档 4.4.1 要求工具总数可控、4.3 要求 augment 只能调白名单内的工具。
把「注册表」与「白名单校验」放在同一处，可以避免 augmentation 与路由层各写一份判断。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .kb_tools import (
    glossary_lookup,
    kb_get_chunk,
    kb_search,
    kb_search_expand,
    kb_stats,
    parts_query,
    vision_extract,
)

logger = logging.getLogger(__name__)

#: 本地工具注册表：名称 -> 异步可调用
LOCAL_TOOLS: dict[str, Callable[..., Any]] = {
    "kb_search": kb_search,
    "kb_search_expand": kb_search_expand,
    "kb_get_chunk": kb_get_chunk,
    "kb_stats": kb_stats,
    "glossary_lookup": glossary_lookup,
    "vision_extract": vision_extract,
    "parts_query": parts_query,
}

#: MCP 工具注册表：名称 -> LangChain 工具对象（启动时加载；失败为空）
MCP_TOOLS: dict[str, Any] = {}


async def load_all_tools() -> dict[str, Any]:
    """加载 MCP 工具并刷新注册表（启动时调用一次；失败不影响本地工具）。"""

    from .. import mcp_client

    MCP_TOOLS.clear()
    try:
        tools = await mcp_client.load_tools()
    except Exception as exc:  # noqa: BLE001 - 双保险：R7 要求这里绝不抛出
        logger.warning("MCP 工具加载异常，按空列表处理：%s", exc)
        tools = []

    for tool in tools:
        name = getattr(tool, "name", None)
        if name:
            MCP_TOOLS[str(name)] = tool
    if MCP_TOOLS:
        logger.info("工具注册表就绪：本地 %d 个 + MCP %d 个", len(LOCAL_TOOLS), len(MCP_TOOLS))
    return dict(MCP_TOOLS)


def needs_external_lookup(question: str | None, *, toggles: dict[str, bool] | None = None) -> list[str]:
    """问题是否指向**知识库文档之外**的数据源，返回命中的工具名列表。

    两类外部源（开发文档 FR-03 / FR-10）：
        parts_query   备件库存 / 替代件 / 到货时间 —— 不在手册里，在台账里
        ticket_search 历史工单案例 —— 不在手册里，在工单系统里

    这两类问题即使文档证据充分也必须补外部源，因此条件边 should_augment
    会用它来强制走 augment 分支（判定逻辑放在这里，避免两边各写一份关键词表）。
    """

    from ..setting import get_settings

    settings = get_settings()
    text = (question or "").lower()
    hits: list[str] = []
    if any(keyword.lower() in text for keyword in settings.parts_question_keywords):
        hits.append("parts_query")
    if any(keyword.lower() in text for keyword in settings.ticket_question_keywords):
        hits.append("ticket_search")
    return hits


def registered_names() -> list[str]:
    """当前可用的全部工具名（本地 + 已加载的 MCP）。"""

    return sorted({*LOCAL_TOOLS, *MCP_TOOLS})


def is_registered(name: str) -> bool:
    return name in LOCAL_TOOLS or name in MCP_TOOLS


def mcp_status() -> dict[str, Any]:
    """MCP 加载状态（给 /api/health 用）。"""

    from .. import mcp_client

    payload = mcp_client.status()
    payload["registered"] = sorted(MCP_TOOLS)
    return payload


def _unwrap_mcp_result(raw: Any) -> Any:
    """把 MCP 工具的返回值解包成服务端函数的原始对象。

    langchain-mcp-adapters 返回的是**内容块列表**而非原始返回值：
        [{"type": "text", "text": "<JSON 字符串>"}]
    这里还原成 dict / list，否则上层拿到的是文本块，取不到 ticket_no 这类字段。
    """

    import json

    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(block, dict) and block.get("type") == "text" for block in raw)
    ):
        joined = "\n".join(str(block.get("text") or "") for block in raw).strip()
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined
    return raw


async def call_tool(name: str, *args: Any, **kwargs: Any) -> Any:
    """统一调用入口。

    - 本地工具：按位置 / 关键字参数直接调用；
    - MCP 工具（LangChain 对象）：把关键字参数打包成 dict 交给 ainvoke。
    """

    if name in LOCAL_TOOLS:
        return await LOCAL_TOOLS[name](*args, **kwargs)

    tool = MCP_TOOLS.get(name)
    if tool is None:
        raise KeyError(f"工具 {name} 未注册（本地与 MCP 都没有）")

    payload = dict(kwargs)
    if args:
        # MCP 工具只接受 dict 入参：单参数字典可直接透传
        if len(args) == 1 and isinstance(args[0], dict):
            payload = {**args[0], **payload}
        else:
            raise TypeError(f"MCP 工具 {name} 只接受关键字参数或单个 dict")
    return _unwrap_mcp_result(await tool.ainvoke(payload))


__all__ = [
    "LOCAL_TOOLS",
    "MCP_TOOLS",
    "_unwrap_mcp_result",
    "call_tool",
    "is_registered",
    "load_all_tools",
    "mcp_status",
    "needs_external_lookup",
    "registered_names",
]
