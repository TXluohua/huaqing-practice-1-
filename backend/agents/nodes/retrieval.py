"""检索侧节点：rewrite / retrieve / rerank / build_context（开发文档 4.3）。

节点契约（详见 nodes/__init__.py）：接收 AgentState，返回**增量字段**字典。
本模块只做「编排」——真正的算法在 backend/rag/ 与 backend/tools/ 里。

产出字段
--------
    rewrite       -> queries
    retrieve      -> candidates
    rerank        -> ranked / top_score
    build_context -> context / context_blocks / evidence_sufficient

关于「证据是否足够」的时序（协作规范 §6.3 待决项的落地口径）
------------------------------------------------------------
`build_context` 节点**先给初值**（按精排归一化分与阈值判定），紧接着图上的
条件边 should_augment() 据此分流；若走 augment（受限 agent，平台侧实现），
由 augment 在补检索后回写同一字段。这样 condition 永远有值可用，
不依赖「augment 之后再判定」这种拿不到顺序的写法。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...setting import get_settings
from ...tools.kb_tools import expand_queries, get_retriever
from ..state import AgentState
from . import NodeName, register_node

logger = logging.getLogger(__name__)


def _elapsed(started: float, name: str) -> dict[str, float]:
    return {name: round((time.perf_counter() - started) * 1000, 2)}


async def rewrite(state: AgentState) -> dict[str, Any]:
    """问题 → 2~3 路检索式（术语归一 + 保留原始问题一路）。

    图片识别结果（image_result）会作为一路补充检索式 —— 报警码这类强标识符
    往往只出现在图片里（FR-02）。
    """

    started = time.perf_counter()
    question = (state.get("question") or "").strip()
    queries = expand_queries(question)

    image_result = state.get("image_result")
    if image_result is not None:
        extra: list[str] = []
        raw_text = getattr(image_result, "raw_text", "") or ""
        extracted = getattr(image_result, "extracted", {}) or {}
        if raw_text.strip():
            extra.append(raw_text.strip()[:200])
        codes = extracted.get("alarm_codes") or extracted.get("alarm_code")
        if isinstance(codes, str):
            codes = [codes]
        if codes:
            extra.append(" ".join(str(c) for c in codes))
        for item in extra:
            if item and item not in queries:
                queries.append(item)
    # 图片识别失败（confidence 极低且无内容）时明确记一笔，便于排障
    errors: list[str] = []
    if image_result is not None and not (getattr(image_result, "extracted", {}) or {}):
        errors.append("图片识别结果为空，本次仅按文本问题检索")

    result: dict[str, Any] = {
        "queries": queries[:3] or [question],
        "timings": _elapsed(started, "rewrite"),
    }
    if errors:
        result["errors"] = errors
    return result


async def retrieve(state: AgentState) -> dict[str, Any]:
    """混合检索（BM25 + 向量 + RRF），产出粗排候选。"""

    started = time.perf_counter()
    settings = get_settings()
    queries = state.get("queries") or [state.get("question") or ""]

    filters: dict[str, Any] = {}
    if state.get("device_model"):
        filters["device_model"] = {"$in": [state["device_model"], "通用"]}
    if state.get("category"):
        filters["category"] = {"$eq": state["category"]}

    retriever = await get_retriever()
    candidates = await retriever.retrieve(
        queries, filters=filters or None, top_k=settings.retrieve_top_k
    )
    logger.debug("retrieve：%d 路检索式 -> %d 候选", len(queries), len(candidates))

    errors: list[str] = []
    if not candidates:
        errors.append("检索无候选：知识库可能为空或过滤条件过严（device_model/category）")
    result: dict[str, Any] = {
        "candidates": candidates,
        "timings": _elapsed(started, "retrieve"),
    }
    if errors:
        result["errors"] = errors
    return result


async def rerank(state: AgentState) -> dict[str, Any]:
    """Cross-Encoder 精排 Top 5~8（不可用时降级 lexical，并在 errors 里如实记录）。"""

    started = time.perf_counter()
    settings = get_settings()
    question = state.get("question") or ""
    candidates = list(state.get("candidates") or [])

    retriever = await get_retriever()
    result = await retriever.rerank(question, candidates, top_n=settings.rerank_top_n)

    provider = str(result.meta.get("rerank_provider", ""))
    # 把精排提供方写进证据元数据：绝对分阈值与置信度口径都要看它是哪种精排
    for item in result.ranked:
        item.metadata["rerank_provider"] = provider

    out: dict[str, Any] = {
        "ranked": result.ranked,
        "top_score": round(float(result.top_score), 4),
        "timings": _elapsed(started, "rerank"),
    }
    if provider == "lexical":
        out["errors"] = [
            "精排已降级为 lexical（Cross-Encoder 模型不可用）：排序质量低于 bge-reranker"
        ]
    elif provider == "failed":
        out["errors"] = ["精排调用失败，已按融合序返回"]
    return out


def rerank_provider(state: AgentState) -> str:
    """当前这次问答实际使用的精排提供方（lexical / cross-encoder / ...）。"""

    items = list(state.get("ranked") or state.get("candidates") or [])
    for item in items:
        provider = (item.metadata or {}).get("rerank_provider")
        if provider:
            return str(provider)
    return ""


def sufficiency_threshold(state: AgentState) -> float:
    """证据充分性阈值：按精排提供方取（lexical 与 Cross-Encoder 分数量纲不同）。"""

    settings = get_settings()
    if rerank_provider(state) == "lexical":
        return settings.sufficient_score_threshold_lexical
    return settings.sufficient_score_threshold


def normalized_top_score(state: AgentState) -> float:
    """从 ranked / candidates 里取精排归一化最高分（供条件边与 verify 复用）。"""

    items = list(state.get("ranked") or state.get("candidates") or [])
    best = 0.0
    for item in items:
        meta = item.metadata or {}
        value = meta.get("rerank_normalized")
        if isinstance(value, (int, float)):
            best = max(best, float(value))
    return best


async def build_context(state: AgentState) -> dict[str, Any]:
    """去重 + 版本择优 + [n] 编号 + token 预算裁剪，并给出证据是否充分的初值。"""

    started = time.perf_counter()
    from ...rag.retriever import build_context as _build_context

    settings = get_settings()
    items = list(state.get("ranked") or state.get("candidates") or [])
    context, blocks = _build_context(items, budget_tokens=settings.context_token_budget)

    normalized = normalized_top_score(state)
    sufficient = bool(blocks) and normalized >= sufficiency_threshold(state)

    out: dict[str, Any] = {
        "context": context,
        "context_blocks": blocks,
        "evidence_sufficient": sufficient,
        "timings": _elapsed(started, "build_context"),
    }
    if not blocks:
        out["errors"] = ["上下文为空：无证据可注入生成节点"]
    return out


def should_augment(state: AgentState) -> str:
    """条件边「证据是否足够?」（graph.build_graph 使用）。

    返回下一个节点名：AUGMENT（补检索）或 GENERATE（直接生成）。
    判定完全基于确定性字段与计数上限，不交给模型（开发文档 4.4.2）。
    """

    settings = get_settings()
    rounds = int(state.get("augment_rounds") or 0)
    calls = int(state.get("tool_calls") or 0)
    enough = bool(state.get("evidence_sufficient"))
    normalized = normalized_top_score(state)
    if enough or normalized >= sufficiency_threshold(state):
        return NodeName.GENERATE
    if rounds >= 2 or calls >= 3:
        # 已到硬边界：不再补检索，直接交给 generate 走拒答路径
        return NodeName.GENERATE
    return NodeName.AUGMENT


# ---- 注册（在各节点模块底部注册，不改注册表文件本身）----
register_node(NodeName.REWRITE, rewrite)
register_node(NodeName.RETRIEVE, retrieve)
register_node(NodeName.RERANK, rerank)
register_node(NodeName.BUILD_CONTEXT, build_context)

__all__ = [
    "build_context",
    "normalized_top_score",
    "rerank_provider",
    "sufficiency_threshold",
    "rerank",
    "retrieve",
    "rewrite",
    "should_augment",
]
