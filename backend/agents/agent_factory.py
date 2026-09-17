"""图实例的异步单例（开发文档 6.2 的 agent_factory.py）。

开发规范（6.4）：异步初始化不使用缓存装饰器，避免阻塞事件循环。
这里用显式 asyncio.Lock + 模块级变量，初始化过程全程 await。

线程 / 事件循环说明：
    生产环境只有一个事件循环（uvicorn），单例只初始化一次；
    测试里若多次调用 asyncio.run()，检测到事件循环变化会自动重建单例，
    避免异步锁与 aiosqlite 连接跨循环复用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..memory import (
    CheckpointerHandle,
    create_checkpointer,
    session_registry,
    thread_config,
)
from .graph import GRAPH_NAME, compile_graph, node_names
from .state import AgentState, make_initial_state

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

#: 已编译图与检查点单例
_agent: "CompiledStateGraph | None" = None
_handle: CheckpointerHandle | None = None
_owner_loop: asyncio.AbstractEventLoop | None = None

#: 初始化用的异步锁（随事件循环重建）
_async_lock: asyncio.Lock | None = None
_async_lock_loop: asyncio.AbstractEventLoop | None = None


def _lock() -> asyncio.Lock:
    """取当前事件循环对应的初始化锁。"""

    global _async_lock, _async_lock_loop
    loop = asyncio.get_running_loop()
    if _async_lock is None or _async_lock_loop is not loop:
        _async_lock = asyncio.Lock()
        _async_lock_loop = loop
    return _async_lock


async def get_agent() -> "CompiledStateGraph":
    """取全局唯一的已编译图，首次调用时完成初始化（懒加载 + 单例）。

    返回
    ----
    CompiledStateGraph：骨架阶段为「零节点 + START -> END」的可执行图。
    """

    global _agent, _handle, _owner_loop

    loop = asyncio.get_running_loop()
    if _agent is not None and _owner_loop is loop:
        return _agent

    async with _lock():
        if _agent is not None and _owner_loop is loop:
            return _agent

        if _agent is not None:
            # 事件循环变了（例如测试里第二次 asyncio.run），释放旧实例
            logger.debug("检测到事件循环变化，重建 agent 单例")
            await _dispose()

        _handle = await create_checkpointer()
        _agent = compile_graph(checkpointer=_handle.saver)
        _owner_loop = loop

        nodes = node_names(_agent)
        logger.info(
            "LangGraph 图已就绪：name=%s backend=%s nodes=%s",
            GRAPH_NAME,
            _handle.backend,
            nodes or "无（骨架阶段）",
        )
        return _agent


async def _dispose() -> None:
    """释放单例持有的资源（内部使用，不重置变量）。"""

    global _agent, _handle, _owner_loop
    if _handle is not None:
        try:
            await _handle.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不应抛给调用方
            logger.warning("关闭检查点失败：%s", exc)
    _agent = None
    _handle = None
    _owner_loop = None


async def close_agent() -> None:
    """释放单例（应用 lifespan 关闭时调用）。"""

    async with _lock():
        await _dispose()
        logger.info("LangGraph 图实例已释放")


async def reset_agent() -> None:
    """释放并允许重新初始化（测试与热更新用）。close_agent() 的别名语义。"""

    await close_agent()


def is_ready() -> bool:
    """单例是否已初始化（不触发初始化）。"""

    return _agent is not None


def graph_summary() -> dict[str, Any]:
    """图的当前状态摘要，供 /api/health 暴露。"""

    return {
        "name": GRAPH_NAME,
        "ready": _agent is not None,
        "checkpointer": _handle.backend if _handle is not None else None,
        "persistent": _handle.persistent if _handle is not None else False,
        "nodes": node_names(_agent) if _agent is not None else [],
    }


async def ainvoke(
    question: str,
    *,
    session_id: str | None = None,
    image_ids: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    thread_id: str | None = None,
    trace_id: str | None = None,
    device_model: str | None = None,
    category: str | None = None,
    answer_mode: str = "qa",
    user_role: str = "engineer",
    config: dict[str, Any] | None = None,
) -> AgentState:
    """跑一次问答链路（阻塞式等待完整结果）。

    session_id 与 thread_id 的对应关系由 memory.session_registry 维护，
    同一 session_id 复用同一 thread_id，从而命中同一份会话检查点。

    trace_id / device_model / category / answer_mode / user_role 为接口层透传字段
    （对应接口文档 §8 缺口①②）：trace_id 让 SSE 的 meta 事件与图内状态同源，
    其余三个进入检索过滤与提示词选择。均有默认值，不影响既有调用方。
    """

    agent = await get_agent()
    session_id = session_id or session_registry.new_session_id()
    resolved_thread = thread_id or session_registry.thread_id(session_id)
    run_config = config or thread_config(resolved_thread)

    initial = make_initial_state(
        question,
        session_id=session_id,
        thread_id=resolved_thread,
        image_ids=image_ids,
        history=history,
        trace_id=trace_id,
        device_model=device_model,
        category=category,
        answer_mode=answer_mode,
        user_role=user_role,
    )

    started = time.perf_counter()
    result: AgentState = await agent.ainvoke(initial, run_config)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "问答链路完成 trace_id=%s thread_id=%s status=%s 耗时=%.1fms",
        result.get("trace_id"),
        resolved_thread,
        result.get("status"),
        elapsed_ms,
    )
    return result


async def astream(
    question: str,
    *,
    session_id: str | None = None,
    image_ids: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    thread_id: str | None = None,
    stream_mode: str = "values",
    trace_id: str | None = None,
    device_model: str | None = None,
    category: str | None = None,
    answer_mode: str = "qa",
    user_role: str = "engineer",
) -> AsyncIterator[Any]:
    """流式跑一次问答链路，供 SSE 接口消费。

    stream_mode="values" 时每个 chunk 是**完整状态**（不是增量），
    chat_service 据此按字段取值做 SSE 事件映射。
    """

    agent = await get_agent()
    session_id = session_id or session_registry.new_session_id()
    resolved_thread = thread_id or session_registry.thread_id(session_id)

    initial = make_initial_state(
        question,
        session_id=session_id,
        thread_id=resolved_thread,
        image_ids=image_ids,
        history=history,
        trace_id=trace_id,
        device_model=device_model,
        category=category,
        answer_mode=answer_mode,
        user_role=user_role,
    )

    async for chunk in agent.astream(
        initial, thread_config(resolved_thread), stream_mode=stream_mode
    ):
        yield chunk
