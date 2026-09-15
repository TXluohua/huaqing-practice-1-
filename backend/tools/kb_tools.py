"""本地知识库工具（开发文档 4.4.1）。

**docstring 是工具的唯一说明书**：何时使用、参数含义、返回结构都写在函数 docstring
里 —— 受限 agent（augment 节点）在选择工具时读的就是这些说明。

本文件归属 RAG 侧（乙）。以下两个工具属平台侧（甲），此处只留签名占位，
避免两端各写一份造成契约漂移：

    vision_extract(image_id)        图片结构化识别（调阿里云 qwen-vl-max）→ multimodal.py
    parts_query(part, device_model) 备件库存 / 替代件 / 到货时间（FR-10）→ 平台侧

工具总数控制在 10 个以内（开发文档 4.4.1），超过会明显拉低模型选择准确率。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from ..agents.state import Evidence
from ..rag.ingest import Chunk
from ..rag.retriever import (
    HybridRetriever,
    build_context,
    build_filters,
    dedupe_evidence,
)
from ..rag.store import get_store
from ..setting import get_settings

logger = logging.getLogger(__name__)

#: 工具白名单（augment 受限 agent 只允许调用这些，开发文档 4.3）
TOOL_WHITELIST: tuple[str, ...] = (
    "kb_search",
    "kb_search_expand",
    "kb_get_chunk",
    "kb_stats",
    "glossary_lookup",
    "ticket_search",
)

_RETRIEVER: HybridRetriever | None = None
_CORPUS_TEXT: str | None = None


async def get_retriever() -> HybridRetriever:
    """进程内共享的检索引擎（BM25 索引与精排器都只建一次）。"""

    global _RETRIEVER
    if _RETRIEVER is None:
        store = await get_store()
        _RETRIEVER = HybridRetriever(store=store, settings=get_settings())
    return _RETRIEVER


def reset_retriever() -> None:
    """清空检索引擎与知识库文本缓存（重建索引后或测试用）。"""

    global _RETRIEVER, _CORPUS_TEXT
    _RETRIEVER = None
    _CORPUS_TEXT = None


async def kb_corpus_text(*, force: bool = False) -> str:
    """整个知识库的检索文本（文档名 + 章节 + 正文），进程内缓存。

    用途：`citation.anchor_check` 的知识库级校验 —— 判断问题里的关键术语
    是否**在知识库中根本不存在**（这类问题必须拒答，而不是拿相似文档硬答）。
    """

    global _CORPUS_TEXT
    if _CORPUS_TEXT is None or force:
        store = await get_store()
        chunks = await store.all_chunks()
        _CORPUS_TEXT = "\n".join(
            f"{c.doc_title} {c.version} {c.section} {c.heading} {c.text}" for c in chunks
        )
    return _CORPUS_TEXT


async def kb_search(
    query: str,
    filters: dict[str, Any] | None = None,
    k: int | None = None,
) -> list[Evidence]:
    """混合检索知识库并精排，返回带元数据的证据块。

    何时使用：任何需要「查资料」的场合，主链路 retrieve 节点的固定调用即本工具。
    参数：
        query   检索式（可多路之一；中文自然语言即可）
        filters 元数据过滤，如 {"device_model": "Etcher-A", "category": "设备维护"}
        k       返回条数，缺省用 setting.rerank_top_n（默认 8）
    返回：Evidence 列表，按精排分降序；每项 metadata 含 doc_title / version /
          page / section / chunk_id，可直接用于引用。
    """

    settings = get_settings()
    retriever = await get_retriever()
    candidates = await retriever.retrieve([query], filters=filters or None)
    result = await retriever.rerank(query, candidates, top_n=k or settings.rerank_top_n)
    logger.debug(
        "kb_search(%r) -> %d 候选 / %d 精排（%s）",
        query,
        len(candidates),
        len(result.ranked),
        result.meta.get("rerank_provider"),
    )
    return result.ranked


async def kb_search_expand(query: str, relax: bool = True) -> list[Evidence]:
    """证据不足时的放宽检索：去掉元数据过滤、扩大召回。

    何时使用：首次检索的 `evidence_sufficient=False` 时（受限 agent 的兜底动作）。
    参数：
        query 检索式
        relax True 表示去掉型号/分类过滤（范围更宽，可能引入其他型号资料）
    返回：Evidence 列表；若知识库确实没有相关内容则返回空列表 ——
          **不要**因为返回空就编造答案，应当拒答。
    """

    settings = get_settings()
    retriever = await get_retriever()
    filters = None if relax else build_filters()
    candidates = await retriever.retrieve(
        [query], filters=filters, top_k=settings.retrieve_top_k * 2
    )
    result = await retriever.rerank(query, candidates, top_n=settings.rerank_top_n)
    return result.ranked


async def kb_get_chunk(id: str, neighbors: int = 1) -> list[Evidence]:
    """按 chunk_id 取原文（可带相邻块），用于溯源与上下文补全。

    何时使用：需要「查看原文/上下文」或引用卡片要展开原文时。
    参数：
        id        切片 ID（形如 c_1a2b3c4d）
        neighbors 前后各取几块，缺省 1
    返回：Evidence 列表（按页码与顺序排列）；ID 不存在时返回空列表。
    """

    store = await get_store()
    chunks: list[Chunk] = await store.neighbors(id, count=neighbors)
    if not chunks:
        single = await store.get_chunk(id)
        chunks = [single] if single else []
    return [
        Evidence(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            score=1.0,
            metadata={
                "doc_id": chunk.doc_id,
                "doc_title": chunk.doc_title,
                "version": chunk.version,
                "category": chunk.category,
                "device_model": chunk.device_model,
                "page": chunk.page,
                "section": chunk.section,
                "heading": chunk.heading,
                "index": chunk.index,
                "source_path": chunk.source_path,
                "retrieval": "kb_get_chunk",
            },
        )
        for chunk in chunks
    ]


async def kb_stats() -> dict[str, Any]:
    """知识库覆盖情况（文档数、切片数、涉及版本与页码）。

    何时使用：拒答时给用户「知识库覆盖了什么」的建议；或运维排查「为什么查不到」。
    返回：{"n_chunks", "n_documents", "documents", "provider", "backend", ...}
    """

    store = await get_store()
    stats = await store.stats()
    return {
        "n_chunks": stats["n_chunks"],
        "n_documents": stats["n_documents"],
        "documents": stats["documents"],
        "provider": stats["provider"],
        "backend": stats["backend"],
        "avg_tokens": stats["avg_tokens"],
    }


@lru_cache(maxsize=1)
def load_glossary() -> dict[str, dict[str, list[str]]]:
    """加载术语表（backend/config/glossary.yaml）；文件缺失时返回空表。"""

    path = Path(__file__).resolve().parents[1] / "config" / "glossary.yaml"
    if not path.exists():  # pragma: no cover - 语料配置缺失时的兜底
        logger.warning("术语表不存在：%s", path)
        return {"terms": {}, "device_models": {}}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 术语表损坏不应中断问答
        logger.warning("术语表解析失败（%s）：%s", path, exc)
        return {"terms": {}, "device_models": {}}
    terms = {str(k): [str(v) for v in (vals or [])] for k, vals in (data.get("terms") or {}).items()}
    models = {
        str(k): [str(v) for v in (vals or [])]
        for k, vals in (data.get("device_models") or {}).items()
    }
    return {"terms": terms, "device_models": models}


async def glossary_lookup(term: str) -> dict[str, Any]:
    """术语归一：把口语/英文写法映射到规范术语。

    何时使用：改写检索式时（rewrite 节点），或用户用了非规范说法。
    参数：term 待归一化的词，如「腔压」「Etcher」「O型圈」
    返回：{"term": 原词, "canonical": 规范词或 None, "synonyms": [...], "queries": [...]}；
          `queries` 是可直接拿去检索的规范写法列表。
    """

    glossary = load_glossary()
    terms: dict[str, list[str]] = glossary.get("terms", {})
    needle = (term or "").strip().lower()
    for canonical, synonyms in terms.items():
        pool = [canonical, *synonyms]
        if any(needle == item.lower() or needle in item.lower() for item in pool):
            return {
                "term": term,
                "canonical": canonical,
                "synonyms": synonyms,
                "queries": [canonical, *synonyms[:2]],
            }
    # 设备型号别名也做一次归一（元数据过滤要用规范型号）
    for canonical, aliases in glossary.get("device_models", {}).items():
        if any(needle == item.lower() or needle in item.lower() for item in [canonical, *aliases]):
            return {"term": term, "canonical": canonical, "synonyms": aliases, "queries": [canonical]}
    return {"term": term, "canonical": None, "synonyms": [], "queries": []}


def expand_queries(question: str) -> list[str]:
    """把问题扩展成 2~3 路检索式（术语归一 + 原始问题保留一路）。

    纯词典实现（确定性、零模型调用）；改写引入噪声的风险由「保留原始问题」
    这一路兜住 —— 见开发文档 7.2 D3。
    """

    glossary = load_glossary()
    queries: list[str] = [question.strip()]
    normalized = question
    hit_terms: list[str] = []
    for canonical, synonyms in glossary.get("terms", {}).items():
        for synonym in synonyms:
            if synonym and synonym.lower() in question.lower():
                normalized = normalized.replace(synonym, canonical)
                hit_terms.append(canonical)
                break
    if normalized.strip() and normalized.strip() != question.strip():
        queries.append(normalized.strip())
    if hit_terms:
        queries.append(" ".join(dict.fromkeys([question.strip(), *hit_terms])))
    # 去掉重复并限制在 3 路以内
    return list(dict.fromkeys(q for q in queries if q))[:3]


async def vision_extract(image_id: str) -> dict[str, Any]:  # pragma: no cover - 平台侧实现
    """图片结构化识别（**平台侧工具，尚未实现**）。

    何时使用：ingest_image 节点处理用户上传的报警截图 / 参数表截图时。
    参数：image_id 由 /api/upload/image 返回
    返回：{"image_type": ..., "extracted": {...}, "confidence": float}
    归属：platform（甲）—— 见 backend/agents/nodes/multimodal.py 与 prompts/vision.md。
    """

    raise NotImplementedError(
        "vision_extract 属平台侧工具（甲）：见 nodes/multimodal.py 与 prompts/vision.md"
    )


async def parts_query(part: str, device_model: str | None = None) -> dict[str, Any]:  # pragma: no cover
    """备件库存 / 替代件 / 到货时间查询（**平台侧工具，尚未实现**，FR-10）。

    何时使用：用户问「某备件有没有库存 / 替代件 / 多久到货」时。
    参数：part 备件名称或料号；device_model 设备型号（可选）
    返回：{"part", "stock", "substitutes": [...], "lead_time_days", "source"}
    归属：platform（甲）。
    """

    raise NotImplementedError("parts_query 属平台侧工具（甲）：FR-10 备件查询")


__all__ = [
    "TOOL_WHITELIST",
    "kb_corpus_text",
    "expand_queries",
    "get_retriever",
    "glossary_lookup",
    "kb_get_chunk",
    "kb_search",
    "kb_search_expand",
    "kb_stats",
    "load_glossary",
    "parts_query",
    "reset_retriever",
    "vision_extract",
]
