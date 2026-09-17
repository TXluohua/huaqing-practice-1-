"""单例依赖与启动预热（开发文档 6.2 的 dependency.py：settings / agent / service）。

为什么预热必须放在这里
----------------------
RAG 侧实测：本地 embedding 与精排模型首次加载合计约 10.2 秒。
如果不预热，这 10 秒会算进**第一个请求**的首 Token，直接顶穿 NFR-01（首 Token <= 2s）。
因此 lifespan 启动时就把向量库与精排器加载好。

降级原则
--------
任何一项预热失败都**不让应用起不来**（对齐开发文档 R7）：
记录到 WarmupReport，由 /api/health 以 degraded 暴露出来。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .setting import get_settings

logger = logging.getLogger(__name__)


@dataclass
class WarmupReport:
    """启动预热的实际结果（供 /api/health 展示）。"""

    graph_ready: bool = False
    vector_store: str | None = None
    chunks: int = 0
    reranker: str | None = None
    db_ready: bool = False
    #: 已注册的 MCP 工具名（工单检索）；空表示已降级
    mcp_tools: list[str] = field(default_factory=list)
    #: 工具注册表总数（本地 + MCP）
    tool_count: int = 0
    elapsed_ms: float = 0.0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """主链路（图 + 向量库）可用即视为预热成功。"""

        return self.graph_ready and self.vector_store is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_ready": self.graph_ready,
            "vector_store": self.vector_store,
            "chunks": self.chunks,
            "reranker": self.reranker,
            "db_ready": self.db_ready,
            "mcp_tools": list(self.mcp_tools),
            "tool_count": self.tool_count,
            "elapsed_ms": self.elapsed_ms,
            "problems": list(self.problems),
            "ok": self.ok,
        }


#: 最近一次预热结果（进程级）
warmup = WarmupReport()


async def warm_up() -> WarmupReport:
    """应用启动预热：目录 -> 数据库 -> 图 -> 向量库 -> 精排器。"""

    global warmup
    started = time.perf_counter()
    report = WarmupReport()

    settings = get_settings()
    settings.ensure_dirs()

    # ---- 1) 业务数据库 ----
    try:
        from . import db

        available, detail = await db.init_db()
        report.db_ready = available
        if not available:
            report.problems.append(detail)
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"数据库初始化异常：{exc}")
        logger.warning("数据库预热失败：%s", exc)

    # ---- 2) LangGraph 图（挂检查点）----
    try:
        from .agents.agent_factory import get_agent, graph_summary

        await get_agent()
        report.graph_ready = bool(graph_summary().get("ready"))
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"图初始化失败：{exc}")
        logger.warning("图预热失败：%s", exc)

    # ---- 3) 向量库（本地模型加载，最慢的一步）----
    try:
        from .rag.store import get_store

        store = await get_store()
        stats = await store.stats()
        report.vector_store = f"{stats.get('provider')}/{stats.get('backend')}"
        report.chunks = int(stats.get("n_chunks") or 0)
        if report.chunks == 0:
            report.problems.append("索引为空：请先运行 scripts/ingest.py --rebuild")
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"向量库预热失败：{exc}")
        logger.warning("向量库预热失败：%s", exc)

    # ---- 4) 精排器 ----
    try:
        from .rag.retriever import create_reranker

        reranker = await create_reranker()
        report.reranker = type(reranker).__name__
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"精排器预热失败：{exc}")
        logger.warning("精排器预热失败：%s", exc)

    # ---- 5) MCP 工具（工单检索）：加载失败降级为空工具，绝不影响启动（R7）----
    try:
        from .tools import load_all_tools, mcp_status, registered_names

        await load_all_tools()
        status = mcp_status()
        report.mcp_tools = list(status.get("registered") or [])
        report.tool_count = len(registered_names())
        if not status.get("ok"):
            report.problems.append(f"MCP 未就绪（已降级）：{status.get('detail')}")
    except Exception as exc:  # noqa: BLE001 - 双保险
        report.problems.append(f"MCP 工具加载异常（已降级）：{exc}")
        logger.warning("MCP 工具加载异常：%s", exc)

    report.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    warmup = report

    if report.ok:
        logger.info(
            "预热完成（%.1fms）：图=%s 向量库=%s(%d 片) 精排=%s 数据库=%s",
            report.elapsed_ms,
            "就绪" if report.graph_ready else "未就绪",
            report.vector_store,
            report.chunks,
            report.reranker,
            "就绪" if report.db_ready else "未就绪",
        )
    else:
        logger.warning("预热未完全成功（%.1fms）：%s", report.elapsed_ms, "；".join(report.problems))
    return report


async def shutdown() -> None:
    """释放进程级资源（图检查点、数据库连接池）。"""

    try:
        from .agents.agent_factory import close_agent

        await close_agent()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭图实例失败：%s", exc)

    try:
        from . import db

        await db.dispose_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭数据库失败：%s", exc)


__all__ = ["WarmupReport", "shutdown", "warm_up", "warmup"]
