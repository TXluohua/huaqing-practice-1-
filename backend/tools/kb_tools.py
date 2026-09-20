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
import re
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
            if not synonym or synonym.lower() not in question.lower():
                continue
            # 边界感知替换：型号/报警码里的词不能动。
            # 实测踩到过 CVD-200 被替换成「薄膜沉积-200」，于是 200 变成孤立的数值锚点，
            # 把一个可答问题判成「材料里找不到该数值」；顺带也让改写后的检索式更干净。
            pattern = re.compile(
                r"(?<![A-Za-z0-9\-])" + re.escape(synonym) + r"(?![A-Za-z0-9\-]*\d)"
            )
            replaced, count = pattern.subn(canonical, normalized)
            if count:
                normalized = replaced
                hit_terms.append(canonical)
                break
    if normalized.strip() and normalized.strip() != question.strip():
        queries.append(normalized.strip())
    if hit_terms:
        queries.append(" ".join(dict.fromkeys([question.strip(), *hit_terms])))
    # 去掉重复并限制在 3 路以内
    return list(dict.fromkeys(q for q in queries if q))[:3]


# --------------------------------------------------------------------------- #
# 平台侧工具（甲）：图片识别与备件查询
# --------------------------------------------------------------------------- #

#: vision.md 的路径（prompt 与代码同仓，改提示词不改代码）
_VISION_PROMPT_PATH = Path(__file__).resolve().parents[1] / "agents" / "prompts" / "vision.md"

#: 阿里云 DashScope 的 OpenAI 兼容端点（qwen-vl-max 走这个入口）
_DASHSCOPE_COMPAT_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)


@lru_cache(maxsize=1)
def _load_vision_prompt() -> str:
    """加载图片转写提示词；文件缺失时用最小等价提示，不让链路挂掉。"""

    try:
        return _VISION_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 仅在提示词文件被删时触发
        logger.warning("vision.md 读取失败（%s），改用内置提示", exc)
        return (
            "把图片中的信息原样转写为 JSON："
            '{"image_type": ..., "raw_text": ..., "extracted": {...}, "confidence": 0~1}。'
            "只转写不判断，看不清就留空并降低 confidence，禁止猜测。"
        )


def _resolve_image_path(image_id: str) -> Path | None:
    """按 image_id 定位已上传的图片文件。

    约定（与服务层一致）：POST /api/upload/image 落盘为
    {upload_dir}/{image_id}.{ext}。同时兼容直接放在 image_dir 的样例图。
    """

    settings = get_settings()
    safe = Path(str(image_id)).name  # 去掉任何路径成分，防目录穿越
    if not safe or safe in {".", ".."}:
        return None

    for directory in (settings.upload_dir, settings.image_dir):
        base = Path(directory)
        if not base.is_dir():
            continue
        direct = base / safe
        if direct.is_file():
            return direct
        for candidate in sorted(base.glob(f"{safe}.*")):
            if candidate.is_file():
                return candidate
    return None


def _image_data_uri(path: Path) -> str:
    """图片转 data URI（base64 内联，避免依赖公网可访问的图片地址）。"""

    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _parse_vision_payload(content: str) -> dict[str, Any]:
    """解析 VLM 返回的 JSON；解析失败按「未能识别」处理，绝不猜。"""

    import json
    import re

    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("图片识别返回非 JSON，按未能识别处理")
        return {
            "image_type": "unknown",
            "extracted": {},
            "raw_text": (content or "")[:500],
            "confidence": 0.0,
        }
    if not isinstance(payload, dict):
        return {"image_type": "unknown", "extracted": {}, "raw_text": "", "confidence": 0.0}

    extracted = payload.get("extracted")
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "image_type": str(payload.get("image_type") or "unknown"),
        "extracted": extracted if isinstance(extracted, dict) else {},
        "raw_text": str(payload.get("raw_text") or ""),
        "confidence": max(0.0, min(1.0, confidence)),
    }


async def vision_extract(image_id: str) -> dict[str, Any]:
    """图片结构化识别（调阿里云 qwen-vl-max，temperature=0）。

    何时使用：ingest_image 节点处理用户上传的报警截图 / 参数表截图时。
    参数：
        image_id  由 POST /api/upload/image 返回的文件标识
    返回：
        {"image_type": str, "extracted": dict, "raw_text": str, "confidence": float}

    边界（开发文档 4.3）：**只转写不判断** —— 不做诊断、不给维修建议；
    识别不出来就降低 confidence，由 ingest_image 提示改用文字描述。
    未配置 DASHSCOPE_API_KEY 时直接抛错，不静默返回空结果。
    """

    import httpx

    settings = get_settings()
    path = _resolve_image_path(image_id)
    if path is None:
        raise FileNotFoundError(
            f"图片不存在：{image_id}（期望 {settings.upload_dir}/{image_id}.*）"
        )
    if not settings.dashscope_api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法识别图片，请改用文字描述现象")

    payload = {
        "model": settings.vlm_model,
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": _load_vision_prompt()}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _image_data_uri(path)}},
                    {"type": "text", "text": "请按系统提示的要求转写这张图片。"},
                ],
            },
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.vision_timeout_s) as client:
        resp = await client.post(_DASHSCOPE_COMPAT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    content = ""
    try:
        content = str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"图片识别返回结构异常：{exc}") from exc

    result = _parse_vision_payload(content)
    logger.debug(
        "vision_extract(%s) -> type=%s confidence=%.2f",
        image_id,
        result["image_type"],
        result["confidence"],
    )
    return result



@lru_cache(maxsize=1)
def _load_parts_inventory() -> dict[str, Any]:
    """加载备件数据源（YAML）。缺失或解析失败时返回空目录，由调用方明确拒答。"""

    settings = get_settings()
    path = Path(settings.parts_inventory_path)
    if not path.is_file():
        logger.warning("备件数据源不存在：%s", path)
        return {"items": [], "updated_at": None}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 数据源坏了不能让工具崩，转成「查不到」
        logger.warning("备件数据源解析失败：%s", exc)
        return {"items": [], "updated_at": None}
    return data if isinstance(data, dict) else {"items": []}


def reset_parts_cache() -> None:
    """清空备件数据缓存（更新数据文件后或测试用）。"""

    _load_parts_inventory.cache_clear()


def _match_part(items: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    """匹配备件：料号精确 > 料号包含 > 名称包含（双向）。"""

    if not needle:
        return None
    for item in items:
        if str(item.get("code", "")).lower() == needle:
            return item
    for item in items:
        if needle in str(item.get("code", "")).lower():
            return item
    for item in items:
        name = str(item.get("part", "")).lower()
        if name and (needle in name or name in needle):
            return item
    return None


def _draft_inquiry(
    item: dict[str, Any],
    substitutes: list[dict[str, Any]],
    device_model: str | None,
) -> str:
    """生成询价单草稿（草稿而已，采购流程与下单全部由人工完成）。"""

    unit = str(item.get("unit") or "")
    quantity = f"1 {unit}".strip()
    model = device_model or item.get("device_model") or "待确认"
    lines = [
        "备件询价单（草稿 —— 需人工确认后走原有采购流程；本系统不自动下单）",
        f"设备型号：{model}",
        f"备件名称：{item.get('part')}",
        f"备件编码：{item.get('code')}",
        f"规格：{item.get('spec') or '见备件目录'}",
        f"需求数量：{quantity}（请按实际需求填写）",
        f"当前库存：{item.get('stock')} {unit}（库位 {item.get('location') or '未登记'}）",
        f"预计到货：{item.get('lead_time_days')} 天",
    ]
    if substitutes:
        top = substitutes[0]
        lines.append(
            f"替代方案：{top.get('part')}（{top.get('code')}）—— 依据：{top.get('basis')}"
        )
    else:
        lines.append("替代方案：无可推荐的替代件（需原厂确认）")
    return "\n".join(lines)


async def parts_query(part: str, device_model: str | None = None) -> dict[str, Any]:
    """备件库存 / 替代件 / 到货时间查询（FR-10，**平台侧工具，甲实现**）。

    何时使用：用户问「某备件有没有库存 / 能不能替代 / 多久到货」时，或诊断结论指向换件时。
    参数：
        part         备件名称或料号（如「腔体门 O-ring」或 SP-ETA-0101）
        device_model 设备型号（可选，用于核对适用范围）
    返回：
        {"found", "part", "code", "spec", "stock", "stock_status", "unit", "location",
         "lead_time_days", "price_cny", "supplier",
         "substitutes": [{part, code, stock, unit, lead_time_days, price_cny, supplier, basis}],
         "forbidden": [...], "draft_inquiry", "notes": [...], "source", "updated_at"}

    说明：`price_cny` / `supplier` **只做台账原样透传**，台账没写就是 `None`
    （调用方按「未提供」处理，不估算价格）。

    边界（开发文档 4.2 / FR-10）：**不自动下单**；替代件必须附兼容性依据，
    无依据时明确输出「需原厂确认」，**不做「应该能替代」的推断** —— 备件用错会损坏设备。
    """

    settings = get_settings()
    catalog = _load_parts_inventory()
    items = [item for item in (catalog.get("items") or []) if isinstance(item, dict)]
    source = str(settings.parts_inventory_path)
    needle = (part or "").strip().lower()

    matched = _match_part(items, needle)
    if matched is None:
        return {
            "found": False,
            "part": part,
            "device_model": device_model,
            "substitutes": [],
            "forbidden": [],
            "draft_inquiry": None,
            "notes": [
                "备件目录中未找到该备件：不做无依据的替代推断，需原厂确认",
                f"数据源：{source}",
            ],
            "source": source,
        }

    stock = int(matched.get("stock") or 0)
    unit = str(matched.get("unit") or "")
    substitutes: list[dict[str, Any]] = []
    notes: list[str] = []

    for sub in matched.get("substitutes") or []:
        if not isinstance(sub, dict):
            continue
        basis = str(sub.get("basis") or "").strip()
        if not basis:
            # 零幻觉：没有权威依据的替代件一律不推荐
            notes.append(
                f"替代件 {sub.get('part')} 缺少兼容性依据，未纳入推荐，需原厂确认"
            )
            continue
        substitutes.append(
            {
                "part": sub.get("part"),
                "code": sub.get("code"),
                "stock": sub.get("stock"),
                "unit": sub.get("unit") or unit,
                "lead_time_days": sub.get("lead_time_days"),
                # 价格 / 供应商原样透传台账值；台账没写就是 None，**不估算、不编造**
                "price_cny": sub.get("price_cny"),
                "supplier": sub.get("supplier"),
                "basis": basis,
                "requires_approval": bool(sub.get("requires_approval")),
            }
        )

    if stock <= int(settings.parts_low_stock):
        notes.append(
            f"库存偏低（{stock} {unit}，阈值 {settings.parts_low_stock}），建议提前备件"
        )
    for bad in matched.get("forbidden") or []:
        if isinstance(bad, dict):
            notes.append(f"禁止替代：{bad.get('part')} —— {bad.get('reason')}")

    return {
        "found": True,
        "part": matched.get("part"),
        "code": matched.get("code"),
        "device_model": device_model or matched.get("device_model"),
        "spec": matched.get("spec"),
        "stock": stock,
        "stock_status": "low" if stock <= int(settings.parts_low_stock) else "ok",
        "unit": unit,
        "location": matched.get("location"),
        "lead_time_days": matched.get("lead_time_days"),
        # 价格与供应商：台账（config/parts_inventory.yaml）里没有就返回 None，
        # 调用方必须按「未提供」处理 —— 本项目不允许推测价格。
        "price_cny": matched.get("price_cny"),
        "supplier": matched.get("supplier"),
        "substitutes": substitutes,
        "forbidden": matched.get("forbidden") or [],
        "draft_inquiry": _draft_inquiry(matched, substitutes, device_model),
        "notes": notes,
        "updated_at": catalog.get("updated_at"),
        "source": source,
    }



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
