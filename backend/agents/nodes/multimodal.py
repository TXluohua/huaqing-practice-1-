"""多模态与受限 agent 节点：ingest_image / augment（开发文档 4.3，平台侧甲实现）。

节点契约（见 nodes/__init__.py）：接收 AgentState，返回**增量字段**字典。

产出字段
--------
    ingest_image -> image_result（可选节点：无图片时不产生任何额外开销）
    augment      -> candidates / ranked / top_score / context / context_blocks /
                    evidence_sufficient / augment_rounds / tool_calls

硬边界（开发文档 4.3）
----------------------
    <= 2 轮、<= 3 次工具调用、15s 超时、工具白名单

这些边界**由代码强制**（开发文档 1.4 受限自主性原则），不依赖模型自觉；
取值全部来自 setting.py，调参不改代码（NFR-04）。

关于「谁来选工具」
------------------
开发文档 4.4.2 把「选哪个工具」交给模型（白名单内），但本项目当前未配置文本模型，
且补检索路径在实现上并不唯一：放宽过滤重检索 + 取邻接块已是确定性最优解。
因此本模块先用**确定性有界流程**实现，工具调用与轮数上限、超时、白名单
全部照旧 enforcement；后续要换成模型驱动时，只需替换 _augment_once 内部，
安全边界与状态契约均不变。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...mcp_servers.ticket_server import format_ticket
from ...rag.retriever import dedupe_evidence
from ...setting import get_settings
from ...tools import call_tool as call_registered_tool
from ...tools import is_registered, needs_external_lookup
from ...tools.kb_tools import vision_extract
from ..state import AgentState, Evidence, ImageExtraction
from . import NodeName, register_node
from .retrieval import build_context as build_context_node
from .retrieval import rerank as rerank_node

logger = logging.getLogger(__name__)


def _elapsed(started: float, name: str) -> dict[str, float]:
    return {name: round((time.perf_counter() - started) * 1000, 2)}


# --------------------------------------------------------------------------- #
# ingest_image：图片结构化识别（可选节点）
# --------------------------------------------------------------------------- #


def _to_extraction(image_id: str, payload: dict[str, Any]) -> ImageExtraction:
    """把 vision_extract 的返回值转成 ImageExtraction（契约字段一一对应）。"""

    extracted = payload.get("extracted")
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return ImageExtraction(
        image_id=image_id,
        image_type=str(payload.get("image_type") or "unknown"),
        extracted=dict(extracted) if isinstance(extracted, dict) else {},
        raw_text=str(payload.get("raw_text") or ""),
        confidence=max(0.0, min(1.0, confidence)),
    )


def _merge_extractions(items: list[ImageExtraction]) -> ImageExtraction:
    """多图合并：取置信度最高者为主，其余图片的抽取结果合并补充。"""

    primary = max(items, key=lambda item: item.confidence)
    extracted: dict[str, Any] = {}
    for item in items:
        for key, value in (item.extracted or {}).items():
            current = extracted.get(key)
            if current in (None, "", [], {}):
                extracted[key] = value
            elif isinstance(current, list) and isinstance(value, list):
                extracted[key] = list(dict.fromkeys([*current, *value]))
    raw_text = "\n".join(text for text in (item.raw_text for item in items) if text).strip()
    return ImageExtraction(
        image_id=primary.image_id,
        image_type=primary.image_type,
        extracted=extracted,
        raw_text=raw_text,
        confidence=primary.confidence,
    )


async def ingest_image(state: AgentState) -> dict[str, Any]:
    """图片 -> 结构化识别结果（只转写不判断）。

    失败处理（FR-02 硬性要求）：confidence 低于阈值或 image_type=unknown 时，
    明确提示「图片未能识别，请用文字描述现象」，**不允许猜测**。
    """

    image_ids = [str(item) for item in (state.get("image_ids") or []) if item]
    if not image_ids:
        # 可选节点：无图片时直接透传，不写任何字段（不产生额外开销）
        return {}

    started = time.perf_counter()
    settings = get_settings()
    errors: list[str] = []
    extractions: list[ImageExtraction] = []

    for image_id in image_ids[: settings.image_max_count]:
        try:
            payload = await asyncio.wait_for(
                vision_extract(image_id), timeout=settings.vision_timeout_s
            )
        except asyncio.TimeoutError:
            errors.append(f"图片识别超时（>{settings.vision_timeout_s:g}s）：{image_id}")
            continue
        except Exception as exc:  # noqa: BLE001 - 单图失败不中断链路
            logger.warning("图片 %s 识别失败：%s", image_id, exc)
            errors.append(f"图片识别失败：{image_id}（{exc}）")
            continue
        extractions.append(_to_extraction(image_id, payload))

    out: dict[str, Any] = {"timings": _elapsed(started, "ingest_image")}

    if not extractions:
        out["image_result"] = None
        out["errors"] = [*errors, "图片未能识别，请用文字描述现象"]
        return out

    merged = _merge_extractions(extractions)
    if merged.image_type == "unknown" or merged.confidence < settings.vision_min_confidence:
        errors.append(
            "图片未能识别"
            f"（type={merged.image_type}, confidence={merged.confidence:.2f}），"
            "请用文字描述现象"
        )
    out["image_result"] = merged
    if errors:
        out["errors"] = errors
    return out


# --------------------------------------------------------------------------- #
# augment：受限 agent 补检索
# --------------------------------------------------------------------------- #

async def _call_tool(name: str, *args: Any, **kwargs: Any) -> Any:
    """白名单内的工具调用；越权或未注册都直接抛错，不静默放行。

    工具注册表统一在 backend/tools/__init__.py（本地工具 + MCP 工具聚合），
    本函数只做「白名单 ∩ 已注册」的校验与派发：
        - 不在 setting.augment_tool_whitelist -> PermissionError
        - 在白名单但没注册（本地缺失 / MCP 未加载）-> KeyError，由调用方按 R7 降级
    """

    settings = get_settings()
    if name not in settings.augment_tool_whitelist:
        raise PermissionError(f"工具 {name} 不在 augment 白名单内")
    if not is_registered(name):
        raise KeyError(f"白名单工具 {name} 未注册（本地工具缺失或 MCP 未加载）")
    return await call_registered_tool(name, *args, **kwargs)


def _best_of(items: list[Evidence]) -> Evidence:
    return max(
        items,
        key=lambda item: item.rerank_score if item.rerank_score is not None else item.score,
    )


#: 结构化数据源（备件台账 / 工单系统）的等效归一化分：
#: 它们不是语义检索结果，而是精确查询结果，命中即高可信。
_STRUCTURED_NORMALIZED = 0.9


def _parts_evidence(payload: Any) -> Evidence | None:
    """备件查询结果 -> 证据块（FR-10）。

    无论查到还是没查到都产出证据块：查不到时也要让生成节点有**可引用的依据**
    去说明「需原厂确认」，而不是让 verify 因为「无引用」把整条回答判成未覆盖。
    """

    if not isinstance(payload, dict):
        return None

    code = str(payload.get("code") or "unknown")
    if payload.get("found"):
        lines = [
            f"备件：{payload.get('part')}（编码 {code}）",
            f"适用设备：{payload.get('device_model') or '通用'}",
            f"当前库存：{payload.get('stock')} {payload.get('unit') or ''}"
            f"（库位 {payload.get('location') or '未登记'}）",
            f"预计到货：{payload.get('lead_time_days')} 天",
        ]
        if payload.get("spec"):
            lines.append(f"规格：{payload['spec']}")
        substitutes = payload.get("substitutes") or []
        if substitutes:
            for sub in substitutes:
                lines.append(
                    f"可用替代件：{sub.get('part')}（{sub.get('code')}，库存 {sub.get('stock')}）"
                    f"—— 依据：{sub.get('basis')}"
                    + ("；须经设备工程师确认" if sub.get("requires_approval") else "")
                )
        else:
            lines.append("替代件：无可推荐的替代件（需原厂确认，不做无依据推断）")
    else:
        lines = [
            f"备件目录中未找到：{payload.get('part') or '该备件'}",
            "结论：不做无依据的替代推断，需原厂确认。",
        ]

    notes = payload.get("notes") or []
    if notes:
        lines.append("备注：" + "；".join(str(note) for note in notes))

    return Evidence(
        chunk_id=f"parts_{code}",
        text="\n".join(lines),
        score=_STRUCTURED_NORMALIZED,
        rerank_score=_STRUCTURED_NORMALIZED,
        metadata={
            "doc_title": "备件库存台账",
            "version": str(payload.get("updated_at") or "示例数据"),
            "section": code,
            "page": 1,
            "source_type": "kb_doc",
            "rerank_provider": "structured",
            "rerank_normalized": _STRUCTURED_NORMALIZED,
            "external": True,
        },
    )


def _ticket_evidence(rows: Any) -> list[Evidence]:
    """历史工单 -> 证据块（FR-03 跨源）。

    source_type 必须是 ticket：前端 CitationCard 按它切换渲染分支，
    填成 kb_doc 会把工单显示成手册文档。
    """

    items: list[Evidence] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticket_no = str(row.get("ticket_no") or "unknown")
        items.append(
            Evidence(
                chunk_id=f"ticket_{ticket_no}",
                text=format_ticket(row),
                score=float(row.get("score") or 0.0),
                rerank_score=_STRUCTURED_NORMALIZED,
                metadata={
                    "doc_title": f"历史工单 {ticket_no}",
                    "version": str(row.get("date") or ""),
                    "section": str(row.get("device_model") or ""),
                    "page": 1,
                    "source_type": "ticket",
                    "rerank_provider": "structured",
                    "rerank_normalized": _STRUCTURED_NORMALIZED,
                    "external": True,
                },
            )
        )
    return items


async def _augment_once(
    state: AgentState,
    *,
    rounds: int,
    calls: int,
    budget: int,
) -> dict[str, Any]:
    """一轮补检索：放宽过滤重检索 -> 取邻接块 -> 合并重排 -> 重建上下文。"""

    started = time.perf_counter()
    question = str(state.get("question") or "")
    queries = [q for q in (state.get("queries") or []) if q] or [question]
    errors: list[str] = []
    collected: list[Evidence] = []
    used = 0

    # 0) 外部数据源优先：备件库存（FR-10）与历史工单（FR-03）都不在知识库文档里，
    #    但它们是本类问题**真正的答案来源**，所以放在补检索之前。
    for tool_name in needs_external_lookup(question):
        if used >= budget:
            break
        try:
            if tool_name == "parts_query":
                payload = await _call_tool("parts_query", question, state.get("device_model"))
                used += 1
                evidence = _parts_evidence(payload)
                if evidence is not None:
                    collected.append(evidence)
            elif tool_name == "ticket_search":
                rows = await _call_tool(
                    "ticket_search",
                    symptom=question,
                    device_model=state.get("device_model"),
                    limit=2,
                )
                used += 1
                collected.extend(_ticket_evidence(rows))
        except Exception as exc:  # noqa: BLE001 - R7：外部源失败只降级，不中断主链路
            errors.append(f"外部数据源调用失败（{tool_name}）：{exc}")

    # 1) 放宽元数据过滤补检索（去掉型号/分类限制，扩大召回）
    for query in queries[: max(1, budget - 1)]:
        if used >= budget:
            break
        try:
            hits = await _call_tool("kb_search_expand", query, True)
        except Exception as exc:  # noqa: BLE001 - 工具失败按 R7 原则降级
            errors.append(f"补检索工具调用失败（kb_search_expand）：{exc}")
            break
        used += 1
        collected.extend(hits or [])

    # 2) 仍有预算则取回最佳新增块的邻接块，补全上下文
    if used < budget and collected:
        try:
            neighbors = await _call_tool("kb_get_chunk", _best_of(collected).chunk_id, 1)
            used += 1
            collected.extend(neighbors or [])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"邻接块获取失败（kb_get_chunk）：{exc}")

    base: dict[str, Any] = {
        "augment_rounds": rounds + 1,
        "tool_calls": calls + used,
    }

    if not collected:
        base["evidence_sufficient"] = False
        base["timings"] = _elapsed(started, "augment")
        base["errors"] = [
            *errors,
            "补检索未获得新证据：知识库可能确实未覆盖该问题（应走拒答）",
        ]
        return base

    # 3) 合并去重后复用检索侧节点重排与重建上下文（不重复实现算法，
    #    乙侧调参时 augment 自动跟随）
    existing = list(state.get("ranked") or state.get("candidates") or [])
    merged = dedupe_evidence([*collected, *existing])
    reranked = await rerank_node({**state, "candidates": merged})  # type: ignore[arg-type]
    built = await build_context_node({**state, "candidates": merged, **reranked})  # type: ignore[arg-type]

    timings: dict[str, float] = dict(_elapsed(started, "augment"))
    # 补检索路径的耗时单独命名，避免覆盖主链路的 rerank / build_context 耗时
    for source in (reranked.get("timings") or {}, built.get("timings") or {}):
        for key, value in source.items():
            timings[f"augment.{key}"] = value

    out: dict[str, Any] = {
        **base,
        "candidates": merged,
        "ranked": reranked.get("ranked", []),
        "top_score": reranked.get("top_score", 0.0),
        "context": built.get("context", ""),
        "context_blocks": built.get("context_blocks", []),
        "evidence_sufficient": bool(built.get("evidence_sufficient")),
        "timings": timings,
    }
    if errors:
        out["errors"] = errors
    logger.debug(
        "augment 第 %d 轮：新增 %d 块，合并后 %d 候选，证据充分=%s",
        rounds + 1,
        len(collected),
        len(merged),
        out["evidence_sufficient"],
    )
    return out


async def augment(state: AgentState) -> dict[str, Any]:
    """证据不足时的补检索（受限 agent，硬边界见模块 docstring）。"""

    settings = get_settings()
    rounds = int(state.get("augment_rounds") or 0)
    calls = int(state.get("tool_calls") or 0)

    if rounds >= settings.augment_max_rounds or calls >= settings.augment_max_tool_calls:
        return {
            "evidence_sufficient": bool(state.get("evidence_sufficient")),
            "errors": [
                "augment 已达硬边界"
                f"（轮数 {rounds}/{settings.augment_max_rounds}，"
                f"工具调用 {calls}/{settings.augment_max_tool_calls}），不再补检索"
            ],
        }

    budget = settings.augment_max_tool_calls - calls
    try:
        return await asyncio.wait_for(
            _augment_once(state, rounds=rounds, calls=calls, budget=budget),
            timeout=settings.augment_timeout_s,
        )
    except asyncio.TimeoutError:
        # 超时必须降级：按已有证据继续，由 generate/verify 决定拒答或低置信度
        return {
            "evidence_sufficient": bool(state.get("evidence_sufficient")),
            "augment_rounds": rounds + 1,
            "errors": [
                f"augment 超时（>{settings.augment_timeout_s:g}s），按现有证据继续"
            ],
        }


# ---- 注册（在各节点模块底部注册，不改注册表文件本身）----
register_node(NodeName.INGEST_IMAGE, ingest_image)
register_node(NodeName.AUGMENT, augment)

__all__ = ["augment", "ingest_image"]
