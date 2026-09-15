"""检索测试（开发文档 8.2 检索评测的单元层：分词 / BM25 / RRF / 去重 / 版本择优 / 上下文）。

测试用内置 hashing 向量化 + 临时目录，**不依赖 bge 模型与网络**，
保证 CI 与离线环境都能跑；真实检索质量由 `scripts/eval.py` 在完整语料上度量。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.state import Evidence  # noqa: E402
from backend.rag.ingest import Chunk  # noqa: E402
from backend.rag.retriever import (  # noqa: E402
    BM25Index,
    HybridRetriever,
    Tokenizer,
    build_context,
    dedupe_evidence,
    is_evidence_sufficient,
    prefer_newer_versions,
    rrf_fuse,
)
from backend.rag.store import HashingEmbedder, VectorStore  # noqa: E402
from backend.rag.store import _SimpleBackend  # noqa: E402
from backend.setting import get_settings  # noqa: E402


def make_chunk(
    chunk_id: str,
    text: str,
    *,
    section: str = "1.1",
    page: int = 1,
    version: str = "V1.0",
    doc_title: str = "测试手册",
    device_model: str = "Etcher-A",
    category: str = "设备维护",
    index: int = 0,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc_test",
        doc_title=doc_title,
        version=version,
        category=category,
        device_model=device_model,
        page=page,
        section=section,
        heading="标题",
        index=index,
        text=text,
        n_chars=len(text),
        n_tokens=len(text),
        source_path="test",
    )


CHUNKS = [
    make_chunk(
        "c_1", "腔体真空度异常时先检查 O-ring 密封，再确认真空泵抽速是否达标。", section="3.4"
    ),
    make_chunk(
        "c_2", "真空泵维护：干泵每 4000 小时更换泵油，分子泵需检查掉速报警。", section="3.5"
    ),
    make_chunk(
        "c_3",
        "CVD 沉积温度窗口为 700~820 摄氏度，膜厚均匀性超差时调整前驱体流量。",
        section="2.1",
        device_model="CVD-200",
        category="工艺",
    ),
    make_chunk("c_4", "上锁挂牌 LOTO 作业顺序：切断能源、挂牌、验证零能量状态。", section="1.1"),
]


async def build_store(tmp: Path) -> VectorStore:
    root = tmp / "idx"
    store = VectorStore(root=root, embedder=HashingEmbedder(dim=256), backend=_SimpleBackend(root))
    await store.add_chunks(CHUNKS)
    return store


# --------------------------------------------------------------------------- #
# 分词与 BM25
# --------------------------------------------------------------------------- #
def test_tokenizer_produces_chinese_tokens() -> None:
    """中文必须分词：整句当一个 token 会让关键词检索完全失效（风险 R5）。"""

    tokens = Tokenizer().cut("刻蚀机腔体真空度异常")
    assert len(tokens) > 1
    assert any("真空" in token for token in tokens)


def test_tokenizer_fallback_is_deterministic() -> None:
    tokens = Tokenizer._fallback_cut("真空度异常")
    assert "真" in tokens and "真空" in tokens
    assert tokens == Tokenizer._fallback_cut("真空度异常")


def test_bm25_ranks_relevant_chunk_first() -> None:
    hits = BM25Index(CHUNKS).search("真空度异常怎么排查", top_k=3)
    assert hits, "BM25 应当有命中"
    assert hits[0][0].chunk_id == "c_1"


def test_bm25_respects_metadata_filter() -> None:
    hits = BM25Index(CHUNKS).search("真空度", top_k=5, where={"category": {"$eq": "工艺"}})
    assert all(hit[0].category == "工艺" for hit in hits)


# --------------------------------------------------------------------------- #
# RRF 与混合检索
# --------------------------------------------------------------------------- #
def test_rrf_fuse_weights_and_order() -> None:
    fused = rrf_fuse([(["a", "b"], 0.6), (["b", "c"], 0.4)], k=60)
    assert fused["b"] > fused["a"], "两路都出现的块应当得分最高"
    assert fused["a"] > fused["c"]


def test_hybrid_retrieve_returns_fused_candidates() -> None:
    async def scenario() -> list[Evidence]:
        with tempfile.TemporaryDirectory() as tmp:
            store = await build_store(Path(tmp))
            retriever = HybridRetriever(store=store, settings=get_settings())
            return await retriever.retrieve(["真空度异常排查"], top_k=4)

    evidence = asyncio.run(scenario())
    assert evidence, "混合检索应有结果"
    assert evidence[0].chunk_id == "c_1"
    meta = evidence[0].metadata or {}
    assert "bm25_score" in meta and "vector_score" in meta and "fused_score" in meta


def test_rerank_uses_lexical_fallback_without_model() -> None:
    """缺少 Cross-Encoder 时必须降级为 lexical，且如实标注提供方。"""

    async def scenario() -> tuple[list[Evidence], str]:
        with tempfile.TemporaryDirectory() as tmp:
            store = await build_store(Path(tmp))
            retriever = HybridRetriever(store=store, settings=get_settings())
            candidates = await retriever.retrieve(["真空度异常排查"], top_k=4)
            result = await retriever.rerank("真空度异常怎么排查", candidates, top_n=2)
            return result.ranked, str(result.meta.get("rerank_provider"))

    ranked, provider = asyncio.run(scenario())
    assert ranked, "精排应返回结果"
    assert provider in {"lexical", "cross-encoder"}
    assert ranked[0].rerank_score is not None


# --------------------------------------------------------------------------- #
# 去重 / 版本择优 / 上下文构建
# --------------------------------------------------------------------------- #
def _evidence(chunk: Chunk, score: float = 1.0) -> Evidence:
    return Evidence(
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        score=score,
        metadata={
            "doc_title": chunk.doc_title,
            "version": chunk.version,
            "page": chunk.page,
            "section": chunk.section,
            "category": chunk.category,
        },
    )


def test_dedupe_and_version_preference() -> None:
    old = make_chunk("c_old", "旧版内容", version="V2.0", section="3.4")
    new = make_chunk("c_new", "新版内容", version="V3.10", section="3.4")
    duplicated = [_evidence(old), _evidence(old), _evidence(new)]

    deduped = dedupe_evidence(duplicated)
    assert [e.chunk_id for e in deduped] == ["c_old", "c_new"], "同 chunk_id 只保留一次"

    preferred = prefer_newer_versions(deduped)
    assert [e.chunk_id for e in preferred] == ["c_new"], "版本择优应保留 V3.10"


def test_build_context_numbers_blocks_and_respects_budget() -> None:
    items = [_evidence(chunk) for chunk in CHUNKS]
    context, blocks = build_context(items, budget_tokens=10_000)
    assert len(blocks) == len(CHUNKS)
    assert "[1]" in context and f"[{len(blocks)}]" in context

    _tight_context, tight_blocks = build_context(items, budget_tokens=12)
    assert len(tight_blocks) < len(items), "超出预算时必须裁剪"


def test_is_evidence_sufficient_uses_absolute_score() -> None:
    """充分性判定依赖绝对分：相对分（min-max）会让任何检索结果都「充分」。"""

    items = [_evidence(CHUNKS[0])]
    assert is_evidence_sufficient(0.9, items, threshold=0.5) is True
    assert is_evidence_sufficient(0.2, items, threshold=0.5) is False
    assert is_evidence_sufficient(0.9, [], threshold=0.5) is False


if __name__ == "__main__":  # pragma: no cover - 无 pytest 时的兜底运行入口
    import traceback

    cases = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, case in cases:
        try:
            case()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    raise SystemExit(1 if failures else 0)
