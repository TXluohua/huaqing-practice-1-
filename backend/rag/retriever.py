"""混合检索：BM25(jieba) + 向量 + RRF 融合 + 精排 + 上下文构建（开发文档 4.3）。

流程
----
    queries（1~3 路改写式）
        -> 每路都做 BM25 与向量检索        （retriever.py 本文件）
        -> RRF 融合去重                    （bm25_weight / vector_weight 可调）
        -> 元数据过滤（device_model / category）
        -> 精排 Top-N                      （cross-encoder 优先，缺失降级 lexical）
        -> 上下文构建（[n] 编号 + token 预算 + 版本择优）

降级策略（离线可用是硬要求）
----------------------------
- 分词：jieba 缺失 → 内置「中文字 + 二元组 + 英文词」分词；
- BM25：rank_bm25 缺失 → 内置 BM25Okapi 实现；
- 精排：bge-reranker 未缓存 → lexical 打分（词覆盖 + 章节号命中 + 短语命中）。
  降级会在 `rerank_provider` 字段里如实标注，绝不假装是 Cross-Encoder 的结果。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..agents.state import Citation, Evidence
from ..setting import Settings, get_settings
from .ingest import Chunk, estimate_tokens
from .store import SearchHit, VectorStore, configure_hf_env, get_store, resolve_model_ref

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
#: 「5.2」这类章节号引用，命中时给额外加分（用户常直接问小节）
_SECTION_REF_RE = re.compile(r"\d+(?:\.\d+)+")

#: 权威度缺省值（未在 authority_weights 中出现的分类）
_DEFAULT_AUTHORITY = 0.8


# --------------------------------------------------------------------------- #
# 分词
# --------------------------------------------------------------------------- #


class Tokenizer:
    """中文分词器：jieba 优先，缺失时退化为确定性字符级分词。

    退化分词 = 连续中文串的「单字 + 相邻二元组」+ 英文/数字小写词。
    对 BM25 而言这个退化仍然可用（中文检索的关键是别把整句当一个 token）。
    """

    name = "jieba"

    def __init__(self) -> None:
        try:
            import jieba  # noqa: F401

            self._jieba = jieba
            # 关闭 jieba 的初始化日志噪音
            jieba.setLogLevel(logging.WARNING)
        except ImportError:  # pragma: no cover - 依赖缺失时的兜底
            self._jieba = None
            self.name = "char-bigram"

    def cut(self, text: str) -> list[str]:
        text = text.lower()
        if self._jieba is not None:
            tokens = [t.strip() for t in self._jieba.lcut(text) if t.strip()]
            return [t for t in tokens if t not in _STOPWORDS]
        return [t for t in self._fallback_cut(text) if t not in _STOPWORDS]

    @staticmethod
    def _fallback_cut(text: str) -> list[str]:
        tokens: list[str] = []
        for chunk in _CJK_RE.findall(text):
            tokens.extend(chunk)
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        tokens.extend(_ASCII_WORD_RE.findall(text))
        return tokens


#: 极简停用词（中文虚词 + 英文功能词）；只影响词法检索的噪声
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "或", "及", "有", "我", "你", "他", "它",
        "这", "那", "什么", "怎么", "如何", "为什么", "请问", "一下", "可以", "需要",
        "以及", "并且", "但是", "如果", "那么", "就", "都", "很", "把", "被", "给",
        "上", "下", "中", "个", "吗", "呢", "吧", "啊", "么", "对", "从", "到", "为",
        "the", "a", "an", "of", "to", "is", "are", "and", "or", "in", "on", "for",
        "how", "what", "why", "do", "does", "i", "you", "it", "this", "that",
    }
)

_TOKENIZER: Tokenizer | None = None


def get_tokenizer() -> Tokenizer:
    """进程内共享的分词器（jieba 首次加载较慢，只做一次）。"""

    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = Tokenizer()
    return _TOKENIZER


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #


class BM25Index:
    """BM25 词法索引：rank_bm25 优先，缺失时用内置实现。

    内置实现为 Okapi BM25（k1=1.5, b=0.75），与 rank_bm25 的默认参数一致，
    保证两种路径的打分口径接近。
    """

    def __init__(self, chunks: Sequence[Chunk], *, tokenizer: Tokenizer | None = None) -> None:
        self.chunks = list(chunks)
        self.tokenizer = tokenizer or get_tokenizer()
        self.corpus: list[list[str]] = [self.tokenizer.cut(c.search_text) for c in self.chunks]
        self.avgdl = (sum(len(d) for d in self.corpus) / len(self.corpus)) if self.corpus else 0.0
        self.df: dict[str, int] = {}
        for doc in self.corpus:
            for token in set(doc):
                self.df[token] = self.df.get(token, 0) + 1
        self.n_docs = len(self.corpus)
        self._rank_bm25 = None
        try:
            from rank_bm25 import BM25Okapi

            self._rank_bm25 = BM25Okapi(self.corpus) if self.corpus else None
            self.name = "rank_bm25"
        except ImportError:  # pragma: no cover - 依赖缺失时的兜底
            self.name = "builtin-bm25"

    # ---- 内置 BM25 ----
    def _idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def _score_builtin(self, query_tokens: Sequence[str], doc_index: int) -> float:
        doc = self.corpus[doc_index]
        if not doc:
            return 0.0
        length = len(doc)
        counts: dict[str, int] = {}
        for token in doc:
            counts[token] = counts.get(token, 0) + 1
        k1, b = 1.5, 0.75
        score = 0.0
        for token in query_tokens:
            freq = counts.get(token, 0)
            if not freq:
                continue
            denom = freq + k1 * (1 - b + b * length / (self.avgdl or 1))
            score += self._idf(token) * freq * (k1 + 1) / denom
        return score

    def search(
        self, query: str, top_k: int, *, where: dict[str, Any] | None = None
    ) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        tokens = self.tokenizer.cut(query)
        if not tokens:
            return []
        if self._rank_bm25 is not None:
            scores = self._rank_bm25.get_scores(tokens)
            pairs = [(i, float(s)) for i, s in enumerate(scores)]
        else:  # pragma: no cover - 仅在缺依赖时走到
            pairs = [(i, self._score_builtin(tokens, i)) for i in range(len(self.chunks))]
        pairs.sort(key=lambda item: -item[1])

        hits: list[tuple[Chunk, float]] = []
        for index, score in pairs:
            if score <= 0:
                break
            chunk = self.chunks[index]
            if not _match_filters(chunk, where):
                continue
            hits.append((chunk, score))
            if len(hits) >= top_k:
                break
        return hits


# --------------------------------------------------------------------------- #
# 元数据过滤
# --------------------------------------------------------------------------- #


def build_filters(
    *,
    device_model: str | None = None,
    category: str | None = None,
    user_role: str = "engineer",
) -> dict[str, Any]:
    """构造检索过滤条件（开发文档 4.3 retrieve：元数据过滤）。

    「检索即鉴权」落在这一层：目前角色只做透传记录，等权限分级表（FR-11）
    落地后在此扩展，不需要改上层节点。
    """

    where: dict[str, Any] = {}
    if device_model:
        where["device_model"] = {"$in": [device_model, "通用"]}
    if category:
        where["category"] = {"$eq": category}
    return where


def _match_filters(chunk: Chunk, where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    for key, value in where.items():
        actual = getattr(chunk, key, None)
        if isinstance(value, dict):
            if "$eq" in value and actual != value["$eq"]:
                return False
            if "$in" in value and actual not in value["$in"]:
                return False
        elif actual != value:
            return False
    return True


# --------------------------------------------------------------------------- #
# RRF 融合
# --------------------------------------------------------------------------- #


def rrf_fuse(
    ranked_lists: Sequence[tuple[Sequence[str], float]],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion：输入 (chunk_id 有序列表, 权重) 序列，返回融合分。"""

    scores: dict[str, float] = {}
    for ids, weight in ranked_lists:
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return scores


# --------------------------------------------------------------------------- #
# 精排
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RerankResult:
    """精排结果：粗排候选 + 精排 Top-N + 供拒答判定的原始/归一化最高分。"""

    candidates: list[Evidence]
    ranked: list[Evidence]
    #: 精排原始分（Cross-Encoder 为 logits，可正可负）
    top_score: float
    #: 归一化到 0~1 的最高分（拒答阈值与置信度公式使用）
    normalized_top: float
    meta: dict[str, Any] = field(default_factory=dict)


#: 精排模型的「是否真的可用」自检探针：(查询, 相关文档, 无关文档)
#: 未训练/权重不完整的模型在相关与无关文档上打分几乎无差别，必须被识别出来
_RERANK_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "刻蚀机腔体真空度异常怎么排查",
        "腔体真空度出现异常波动时，检查腔体门 O-ring 有无压痕与开裂。",
        "本章介绍公司员工的考勤制度、报销流程与年假申请方式。",
    ),
    (
        "O-ring 更换周期与规格",
        "腔体门 O-ring 规格为 FKM 氟橡胶，硬度 70 Shore A，更换周期 300 小时。",
        "今日天气晴朗，气温适宜，适合户外郊游与野餐活动。",
    ),
)


class CrossEncoderReranker:
    """Cross-Encoder 精排（bge-reranker-v2-m3）。

    **加载后必须自检**：sentence-transformers 在拿不到权重时不会报错，
    而是「按 config 新建一个未训练模型」，其打分毫无区分度。
    自检（探针排序）不通过的模型一律视为不可用，由 create_reranker() 降级 lexical。
    """

    name = "cross-encoder"

    def __init__(self, model: str, *, offline: bool = True) -> None:
        # 支持「仓库名」与「项目内模型目录路径」两种写法（见 resolve_model_ref）
        self.model = resolve_model_ref(model, get_settings().model_dir)
        self.offline = offline
        self._model: Any = None
        self._lock = asyncio.Lock()
        self.smoke_test_passed: bool | None = None

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._model is not None:
                return self._model
            configure_hf_env(get_settings(), offline=self.offline)

            def load() -> Any:
                from sentence_transformers import CrossEncoder

                return CrossEncoder(self.model)

            self._model = await asyncio.to_thread(load)
            ok = await self._smoke_test()
            self.smoke_test_passed = ok
            if not ok:
                self._model = None
                raise RuntimeError(
                    f"精排模型 {self.model} 自检未通过（相关文档得分未高于无关文档）："
                    "很可能是未训练或权重不完整的模型"
                )
            logger.info("精排就绪：cross-encoder/%s（自检通过）", self.model)
            return self._model

    async def _smoke_test(self) -> bool:
        """探针排序自检：相关文档的分数必须高于无关文档。"""

        model = self._model
        pairs = []
        for query, relevant, irrelevant in _RERANK_PROBES:
            pairs.extend([(query, relevant), (query, irrelevant)])

        def run() -> list[float]:
            return [float(x) for x in model.predict(pairs)]

        try:
            scores = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - 自检失败即视为不可用
            logger.warning("精排自检执行失败：%s", exc)
            return False
        for index in range(0, len(scores) - 1, 2):
            if not scores[index] > scores[index + 1]:
                return False
        return True

    @staticmethod
    def absolute_score(raw: float) -> float:
        """logits → 0~1 概率（sigmoid）。

        **绝对分**才能用于拒答阈值：Cross-Encoder 的 logits 越大越相关，
        sigmoid 后 0.5 近似「与问题相关」的分界。
        """

        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, raw))))

    async def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        """对候选切片打分（CPU 上较慢，调用方需先限流，见 rerank_max_candidates）。"""

        model = await self._ensure()
        settings = get_settings()
        # 字符预算与 max_length 大致对应（中文约 1 字 1 token），tokenizer 还会再截断一次
        char_budget = settings.rerank_max_length * 2
        pairs = [(query, c.search_text[:char_budget]) for c in chunks]

        def run() -> list[float]:
            raw = model.predict(
                pairs,
                batch_size=settings.rerank_batch_size,
                max_length=settings.rerank_max_length,
            )
            return [float(x) for x in raw]

        return await asyncio.to_thread(run)


class LexicalReranker:
    """lexical 精排（降级实现）：词覆盖 + 章节号命中 + 短语命中 + BM25 归一。

    它不是语义精排，但**确定性、零依赖、可离线**，且比随机好得多；
    输出会在 `rerank_provider="lexical"` 里如实标注，评测报告也会写明。
    """

    name = "lexical"

    def __init__(self, *, tokenizer: Tokenizer | None = None) -> None:
        self.tokenizer = tokenizer or get_tokenizer()

    @staticmethod
    def absolute_score(raw: float) -> float:
        """lexical 打分已在 0~1 区间（词覆盖/短语/标题/章节号加权），直接使用。"""

        return max(0.0, min(1.0, raw))

    async def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        q_tokens = set(self.tokenizer.cut(query))
        sections = set(_SECTION_REF_RE.findall(query))
        scores: list[float] = []
        for chunk in chunks:
            c_tokens = set(self.tokenizer.cut(chunk.search_text))
            if not q_tokens:
                scores.append(0.0)
                continue
            coverage = len(q_tokens & c_tokens) / len(q_tokens)
            # 连续短语命中（查询里长度 >= 4 的中文片段出现在切片中）
            phrase = 0.0
            for fragment in _CJK_RE.findall(query):
                if len(fragment) >= 4 and fragment in chunk.text:
                    phrase = 1.0
                    break
            section_hit = 1.0 if sections & {chunk.section} else 0.0
            heading_tokens = set(self.tokenizer.cut(f"{chunk.heading} {chunk.section}"))
            heading_hit = (
                len(q_tokens & heading_tokens) / len(q_tokens) if heading_tokens else 0.0
            )
            scores.append(0.55 * coverage + 0.2 * phrase + 0.15 * heading_hit + 0.1 * section_hit)
        return scores


async def create_reranker(settings: Settings | None = None) -> CrossEncoderReranker | LexicalReranker:
    """按配置选择精排器（auto：Cross-Encoder 可用则用，否则 lexical）。"""

    settings = settings or get_settings()
    choice = (settings.rerank_provider or "auto").lower()
    if choice == "lexical":
        return LexicalReranker()
    if choice == "cross-encoder":
        return CrossEncoderReranker(settings.rerank_model, offline=settings.embedding_offline)
    reranker = CrossEncoderReranker(settings.rerank_model, offline=settings.embedding_offline)
    try:  # pragma: no cover - 依赖本机模型缓存
        await reranker._ensure()
        return reranker
    except Exception as exc:  # noqa: BLE001 - 逐级降级
        logger.warning("Cross-Encoder 精排不可用（%s），降级为 lexical 精排", exc)
        return LexicalReranker()


# --------------------------------------------------------------------------- #
# 混合检索
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RetrievalTrace:
    """一次混合检索的可观测信息（评测脚本与排障使用，不进 AgentState）。"""

    n_bm25: int = 0
    n_vector: int = 0
    n_fused: int = 0
    filters: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _hit_to_evidence(chunk: Chunk, score: float, *, source: str) -> Evidence:
    return Evidence(
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        score=float(score),
        rerank_score=None,
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
            "retrieval": source,
        },
    )


def _evidence_to_chunk(evidence: Evidence) -> Chunk:
    meta = evidence.metadata or {}
    return Chunk(
        chunk_id=evidence.chunk_id,
        doc_id=str(meta.get("doc_id", "")),
        doc_title=str(meta.get("doc_title", "")),
        version=str(meta.get("version", "")),
        category=str(meta.get("category", "")),
        device_model=str(meta.get("device_model", "")),
        page=int(meta.get("page", 0) or 0),
        section=str(meta.get("section", "")),
        heading=str(meta.get("heading", "")),
        index=int(meta.get("index", 0) or 0),
        text=evidence.text,
        n_chars=len(evidence.text),
        n_tokens=estimate_tokens(evidence.text),
        source_path=str(meta.get("source_path", "")),
    )


class HybridRetriever:
    """BM25 + 向量 + RRF + 精排（节点与工具层共用的检索引擎）。"""

    def __init__(
        self,
        *,
        store: VectorStore,
        settings: Settings,
        reranker: CrossEncoderReranker | LexicalReranker | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.tokenizer = tokenizer or get_tokenizer()
        self.reranker = reranker
        self._bm25: BM25Index | None = None
        self._bm25_signature = ""

    # ---- 索引 ----
    async def _ensure_indices(self) -> BM25Index:
        chunks = await self.store.all_chunks()
        signature = f"{len(chunks)}:{hash(tuple(c.chunk_id for c in chunks))}"
        if self._bm25 is None or signature != self._bm25_signature:
            self._bm25 = BM25Index(chunks, tokenizer=self.tokenizer)
            self._bm25_signature = signature
            logger.debug("BM25 索引就绪：%d 个切片（%s）", len(chunks), self._bm25.name)
        return self._bm25

    async def _ensure_reranker(self) -> CrossEncoderReranker | LexicalReranker:
        if self.reranker is None:
            self.reranker = await create_reranker(self.settings)
        return self.reranker

    # ---- 检索 ----
    async def retrieve(
        self,
        queries: Sequence[str],
        *,
        filters: dict[str, Any] | None = None,
        top_k: int | None = None,
    ) -> list[Evidence]:
        """多路 query 的混合检索 + RRF 融合，返回粗排候选。"""

        top_k = top_k or self.settings.retrieve_top_k
        queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        if not queries:
            return []
        bm25 = await self._ensure_indices()
        if bm25.n_docs == 0:
            return []

        bm25_ranked: list[str] = []
        vector_ranked: list[str] = []
        bm25_scores: dict[str, float] = {}
        vector_scores: dict[str, float] = {}
        chunk_by_id: dict[str, Chunk] = {}

        for query in queries:
            for chunk, score in bm25.search(query, top_k, where=filters):
                chunk_by_id.setdefault(chunk.chunk_id, chunk)
                bm25_scores[chunk.chunk_id] = max(bm25_scores.get(chunk.chunk_id, 0.0), score)
                if chunk.chunk_id not in bm25_ranked:
                    bm25_ranked.append(chunk.chunk_id)
            try:
                hits: list[SearchHit] = await self.store.search_vector(
                    query, top_k=top_k, where=filters
                )
            except Exception as exc:  # noqa: BLE001 - 向量召回失败不应拖垮词法召回
                logger.warning("向量检索失败（%s），本次仅用 BM25 结果", exc)
                hits = []
            for hit in hits:
                chunk_by_id.setdefault(hit.chunk.chunk_id, hit.chunk)
                vector_scores[hit.chunk.chunk_id] = max(
                    vector_scores.get(hit.chunk.chunk_id, 0.0), hit.score
                )
                if hit.chunk.chunk_id not in vector_ranked:
                    vector_ranked.append(hit.chunk.chunk_id)

        fused = rrf_fuse(
            [
                (bm25_ranked[:top_k], self.settings.bm25_weight),
                (vector_ranked[:top_k], self.settings.vector_weight),
            ],
            k=self.settings.rrf_k,
        )
        order = sorted(fused.items(), key=lambda item: -item[1])
        evidence: list[Evidence] = []
        for chunk_id, fused_score in order[:top_k]:
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:  # pragma: no cover - 理论不可达
                continue
            item = _hit_to_evidence(chunk, fused_score, source="hybrid")
            item.metadata["bm25_score"] = round(bm25_scores.get(chunk_id, 0.0), 4)
            item.metadata["vector_score"] = round(vector_scores.get(chunk_id, 0.0), 4)
            item.metadata["fused_score"] = round(fused_score, 6)
            evidence.append(item)
        return evidence

    async def rerank(
        self,
        question: str,
        candidates: Sequence[Evidence],
        *,
        top_n: int | None = None,
    ) -> RerankResult:
        """精排：返回 Top-N 与归一化最高分（拒答判定用）。"""

        top_n = top_n or self.settings.rerank_top_n
        if not candidates:
            return RerankResult([], [], 0.0, 0.0, {"rerank_provider": "none"})
        reranker = await self._ensure_reranker()

        # 候选限流：Cross-Encoder 逐对打分，CPU 上代价随候选数线性增长。
        # 只精排融合分最高的前 N 条（默认 12），其余不再送模型 ——
        # 这是「P95 ≤ 8s」与「召回质量」之间刻意选的折中点，可用配置调整。
        limit = max(top_n, self.settings.rerank_max_candidates)
        candidates_sorted = sorted(candidates, key=lambda e: -float(e.score))
        reranked_candidates = candidates_sorted[:limit]
        truncated = len(candidates_sorted) - len(reranked_candidates)
        candidates = reranked_candidates
        chunks = [_evidence_to_chunk(e) for e in candidates]
        try:
            raw_scores = await reranker.score(question, chunks)
        except Exception as exc:  # noqa: BLE001 - 精排失败降级为融合序
            logger.warning("精排失败（%s），按融合序返回", exc)
            ranked = list(candidates)[:top_n]
            for item in ranked:
                item.rerank_score = float(item.score)
            return RerankResult(
                list(candidates),
                ranked,
                float(ranked[0].score) if ranked else 0.0,
                0.0,
                {"rerank_provider": "failed"},
            )

        # 关键：归一化必须是**绝对分**而非 min-max。
        # min-max 会把候选里的最高分永远拉到 1.0，于是「检索到的东西都不相关」
        # 也会被判为证据充分 —— 拒答闸门形同虚设（负样本会拿到高置信度答案）。
        normalized = [reranker.absolute_score(raw) for raw in raw_scores]
        pairs = sorted(
            zip(candidates, raw_scores, normalized), key=lambda item: -item[1]
        )
        ranked: list[Evidence] = []
        for evidence, raw, norm in pairs[:top_n]:
            evidence.rerank_score = float(raw)
            evidence.metadata["rerank_normalized"] = round(float(norm), 4)
            ranked.append(evidence)
        top_raw = float(pairs[0][1]) if pairs else 0.0
        top_norm = float(pairs[0][2]) if pairs else 0.0
        return RerankResult(
            list(candidates),
            ranked,
            top_raw,
            top_norm,
            {
                "rerank_provider": reranker.name,
                "rerank_model": getattr(reranker, "model", ""),
                "rerank_n_candidates": len(candidates),
                "rerank_truncated": truncated,
            },
        )


# --------------------------------------------------------------------------- #
# 上下文构建
# --------------------------------------------------------------------------- #


def dedupe_evidence(items: Sequence[Evidence]) -> list[Evidence]:
    """按 chunk_id 去重（保留首个，即排名更高的那个）。"""

    seen: set[str] = set()
    out: list[Evidence] = []
    for item in items:
        if item.chunk_id in seen:
            continue
        seen.add(item.chunk_id)
        out.append(item)
    return out


def prefer_newer_versions(items: Sequence[Evidence]) -> list[Evidence]:
    """版本择优：同一「文档名 + 章节」只保留版本号更大的那条（开发文档 4.3）。

    版本号按「数字段逐个比较」解析（V3.2 > V3.10 > V2.0 这类常见写法）。
    """

    best: dict[tuple[str, str], Evidence] = {}
    order: list[tuple[str, str]] = []
    passthrough: list[Evidence] = []
    for item in items:
        meta = item.metadata or {}
        title = str(meta.get("doc_title", ""))
        section = str(meta.get("section", ""))
        if not title or not section:
            passthrough.append(item)
            continue
        key = (title, section)
        current = best.get(key)
        if current is None:
            best[key] = item
            order.append(key)
            continue
        if _version_key(str(meta.get("version", ""))) > _version_key(
            str((current.metadata or {}).get("version", ""))
        ):
            best[key] = item
    return [best[key] for key in order] + passthrough


def _version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers) if numbers else (0,)


def build_context(
    items: Sequence[Evidence],
    *,
    budget_tokens: int | None = None,
) -> tuple[str, list[Evidence]]:
    """构建带 [n] 编号的上下文文本（token 预算内裁剪）。

    编号由本函数分配，`context_blocks` 的顺序即 [n] 顺序 ——
    generate 只能引用这里出现过的编号，verify 也据此校验引用真实性。
    """

    settings = get_settings()
    budget = budget_tokens or settings.context_token_budget
    blocks = dedupe_evidence(prefer_newer_versions(items))
    lines: list[str] = []
    kept: list[Evidence] = []
    used = 0
    for item in blocks:
        meta = item.metadata or {}
        header = (
            f"[{len(kept) + 1}] 《{meta.get('doc_title', '')}》"
            f"{(' ' + str(meta.get('version'))) if meta.get('version') else ''}"
            f" 第 {meta.get('page', 0)} 页"
            f"{(' 章节 ' + str(meta.get('section'))) if meta.get('section') else ''}"
        )
        body = item.text.strip()
        cost = estimate_tokens(header) + estimate_tokens(body)
        if kept and used + cost > budget:
            break
        kept.append(item)
        lines.append(f"{header}\n{body}")
        used += cost
    return "\n\n".join(lines), kept


def evidence_to_citations(items: Sequence[Evidence]) -> list[Citation]:
    """把上下文块转成引用项（编号由代码分配，禁止模型编造）。"""

    citations: list[Citation] = []
    for index, item in enumerate(items, start=1):
        meta = item.metadata or {}
        citations.append(
            Citation(
                id=index,
                source_type="image" if meta.get("source_type") == "image" else "kb_doc",
                doc=str(meta.get("doc_title") or "") or None,
                version=str(meta.get("version") or "") or None,
                section=str(meta.get("section") or "") or None,
                page=int(meta.get("page") or 0) or None,
                chunk_id=item.chunk_id,
                image_url=meta.get("image_url"),
                snippet=item.text[:300].strip(),
            )
        )
    return citations


def is_evidence_sufficient(
    normalized_top: float,
    items: Sequence[Evidence],
    *,
    threshold: float | None = None,
) -> bool:
    """「证据是否足够」判定（条件边与 augment 的入口）。

    判定只看两件事：精排归一化最高分是否过阈值、是否真的有证据块。
    纯确定性代码 —— 不交给模型决定（开发文档 4.4.2）。
    """

    settings = get_settings()
    threshold = settings.sufficient_score_threshold if threshold is None else threshold
    if not items:
        return False
    return normalized_top >= threshold


__all__ = [
    "BM25Index",
    "CrossEncoderReranker",
    "HybridRetriever",
    "LexicalReranker",
    "RerankResult",
    "RetrievalResult",
    "Tokenizer",
    "build_context",
    "build_filters",
    "create_reranker",
    "dedupe_evidence",
    "evidence_to_citations",
    "get_tokenizer",
    "is_evidence_sufficient",
    "prefer_newer_versions",
    "rrf_fuse",
]
