#!/usr/bin/env python
"""跑黄金集出指标（开发文档 8.1–8.3）。

用法
    .venv/bin/python scripts/eval.py                    # 跑全部 20 条
    .venv/bin/python scripts/eval.py --limit 5           # 只跑前 5 条（快速回归）
    .venv/bin/python scripts/eval.py --badcases          # 失败样例追加到 badcases.md
    .venv/bin/python scripts/eval.py --out report.json   # 指定报告路径

指标（目标见开发文档 8.3）
    Top-5 检索命中率 ≥ 80%（可降至 70%）
    引用覆盖率 100%（**不可降级**）
    引用真实性 ≥ 95%（本实现要求伪造引用 = 0）
    答案要点命中率 ≥ 80%（可降至 70%）
    负样本正确拒答率 ≥ 90%（可降至 80%）
    逐条延迟（文字类 P95 ≤ 8s）

说明：本脚本走**完整图**（rewrite → retrieve → rerank → build_context → generate →
verify），因此测得的是端到端真实指标，而不是「只测检索」的乐观数字。
图片类样例需要 fixtures 图片与多模态节点（平台侧），默认跳过并如实标注。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.graph import compile_graph  # noqa: E402
from backend.agents.state import make_initial_state  # noqa: E402
from backend.setting import get_settings  # noqa: E402

#: 验收目标（开发文档 8.3）：(指标名, 目标值, 可降级值, 是否不可降级)
TARGETS: tuple[tuple[str, float, float, bool], ...] = (
    ("top5_hit_rate", 0.80, 0.70, False),
    ("citation_coverage", 1.00, 1.00, True),
    ("citation_precision", 0.95, 0.90, False),
    ("answer_point_hit_rate", 0.80, 0.70, False),
    ("negative_reject_rate", 0.90, 0.80, False),
)

_PUNCT_RE = re.compile(r"[\s，。、；：？！“”‘’（）()\[\]【】,.;:?!—－\-_/\\]+")


def normalize(text: str) -> str:
    """去掉空白与标点，便于中文模糊比对。"""

    return _PUNCT_RE.sub("", (text or "").lower())


def bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def point_hit(point: str, haystack: str) -> bool:
    """要点是否命中：先精确子串，再做二元组重叠（阈值 0.5）。"""

    p, h = normalize(point), normalize(haystack)
    if not p:
        return False
    if p in h:
        return True
    pb, hb = bigrams(p), bigrams(h)
    if not pb:
        return False
    return len(pb & hb) / len(pb) >= 0.5


def parse_must_cite(entry: str) -> tuple[str, str]:
    """`文档名#3.4` → (文档名, 3.4)。"""

    doc, _, section = (entry or "").partition("#")
    return doc.strip(), section.strip()


def ndcg_at_k(relevances: Sequence[int], k: int = 5) -> float:
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))
    ideal = sorted(relevances, reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


@dataclass
class ItemResult:
    """单条样例的评测结果。"""

    id: str
    question: str
    negative: bool
    status: str
    latency_s: float
    confidence: float
    labels: list[str] = field(default_factory=list)
    answer_points_total: int = 0
    answer_points_hit: int = 0
    answer_points_hit_strict: int = 0
    n_citations: int = 0
    rerank_provider: str = ""
    top5_hit: float = 0.0
    reciprocal_rank: float = 0.0
    ndcg5: float = 0.0
    coverage: float = 0.0
    fake_citations: list[int] = field(default_factory=list)
    cited_blocks: int = 0
    retrieved_sections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    answer_head: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "negative": self.negative,
            "status": self.status,
            "latency_s": round(self.latency_s, 3),
            "confidence": self.confidence,
            "labels": self.labels,
            "top5_hit": self.top5_hit,
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "ndcg5": round(self.ndcg5, 4),
            "coverage": self.coverage,
            "fake_citations": self.fake_citations,
            "cited_blocks": self.cited_blocks,
            "answer_points": f"{self.answer_points_hit}/{self.answer_points_total}",
            "answer_points_strict": f"{self.answer_points_hit_strict}/{self.answer_points_total}",
            "n_citations": self.n_citations,
            "rerank_provider": self.rerank_provider,
            "retrieved_sections": self.retrieved_sections,
            "answer_head": self.answer_head,
            "notes": self.notes,
        }


def evaluate_item(item: dict[str, Any], result: dict[str, Any], latency_s: float) -> ItemResult:
    """把一次问答的状态折算成该样例的指标。"""

    question = str(item.get("question", ""))
    negative = bool(item.get("negative"))
    status = str(result.get("status", ""))
    answer = str(result.get("answer") or "")
    blocks = list(result.get("context_blocks") or [])
    ranked = list(result.get("ranked") or [])
    structured = result.get("answer_structured") or {}
    verification = structured.get("verification") or {}

    out = ItemResult(
        id=str(item.get("id", "")),
        question=question,
        negative=negative,
        status=status,
        latency_s=latency_s,
        confidence=float(result.get("confidence") or 0.0),
        answer_head=answer[:120].replace("\n", " "),
    )
    out.coverage = float(verification.get("coverage") or 0.0)
    out.fake_citations = list(verification.get("fake_citation_ids") or [])
    out.cited_blocks = len(blocks)
    out.n_citations = len(result.get("citations") or [])
    ranked = list(result.get("ranked") or [])
    if ranked:
        out.rerank_provider = str((ranked[0].metadata or {}).get("rerank_provider") or "")

    # ---- 检索侧：以 must_cite 的「文档名#章节」在精排结果中的位置判定 ----
    labels = [parse_must_cite(entry) for entry in (item.get("must_cite") or [])]
    out.labels = [f"{doc}#{sec}" for doc, sec in labels]

    graded: list[int] = []
    for evidence in ranked:
        meta = evidence.metadata or {}
        pair = (str(meta.get("doc_title", "")), str(meta.get("section", "")))
        graded.append(1 if pair in labels else 0)
    out.retrieved_sections = [
        f"{(e.metadata or {}).get('doc_title', '')}#{(e.metadata or {}).get('section', '')}"
        for e in ranked[:5]
    ]
    if labels:
        out.top5_hit = 1.0 if any(graded[:5]) else 0.0
        first = next((i for i, rel in enumerate(graded) if rel), None)
        out.reciprocal_rank = 1.0 / (first + 1) if first is not None else 0.0
        out.ndcg5 = ndcg_at_k(graded, 5)

    # ---- 生成侧：要点命中（在答案与引用原文中找依据）----
    points = list(item.get("answer_points") or [])
    cited_text = "\n".join(block.text for block in blocks)
    haystack = f"{answer}\n{cited_text}"
    out.answer_points_total = len(points)
    out.answer_points_hit = sum(1 for p in points if point_hit(str(p), haystack))
    # 严格口径：只看答案正文（不借引用原文），更接近「答案到底有没有回答到」
    out.answer_points_hit_strict = sum(1 for p in points if point_hit(str(p), answer))

    if negative:
        if status == "NOT_COVERED":
            out.notes.append("负样本正确拒答")
        else:
            out.notes.append(f"负样本未拒答（status={status}），属于严重问题")
    else:
        if status != "OK":
            out.notes.append(f"正样本未返回答案（status={status}；{'; '.join(result.get('uncertain') or [])}）")
        if out.fake_citations:
            out.notes.append(f"存在伪造引用编号 {out.fake_citations}")
        if not blocks:
            out.notes.append("无证据块（检索为空或过滤过严）")
    return out


def summarize(results: Sequence[ItemResult], *, latency_p95: float) -> dict[str, Any]:
    """汇总指标（与开发文档 8.3 的验收口径一致）。"""

    positives = [r for r in results if not r.negative]
    negatives = [r for r in results if r.negative]
    with_labels = [r for r in positives if r.labels]

    def mean(values: Iterable[float]) -> float:
        values = list(values)
        return round(sum(values) / len(values), 4) if values else 0.0

    total_points = sum(r.answer_points_total for r in positives)
    hit_points = sum(r.answer_points_hit for r in positives)
    fake_total = sum(len(r.fake_citations) for r in results)
    refs_total = sum(r.n_citations for r in results)
    # 引用真实性 = 已发出的引用项中真实存在的比例（无引用可校验时记 1.0 并注明）
    citation_precision = (
        (refs_total - fake_total) / refs_total if refs_total else 1.0
    )

    summary = {
        "n_items": len(results),
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "top5_hit_rate": mean(r.top5_hit for r in with_labels),
        "mrr": mean(r.reciprocal_rank for r in with_labels),
        "ndcg5": mean(r.ndcg5 for r in with_labels),
        "citation_coverage": mean(r.coverage for r in positives),
        "citation_precision": round(citation_precision, 4),
        "citation_precision_note": (
            f"已发出引用 {refs_total} 项，伪造 {fake_total} 项"
            if refs_total
            else "本轮没有任何引用可校验（全部样例被 verify 拒答）"
        ),
        "total_citations": refs_total,
        "fake_citation_count": fake_total,
        "answer_point_hit_rate": round(hit_points / total_points, 4) if total_points else 0.0,
        "answer_point_hit_rate_strict": (
            round(sum(r.answer_points_hit_strict for r in positives) / total_points, 4)
            if total_points
            else 0.0
        ),
        "answer_points": f"{hit_points}/{total_points}",
        "negative_reject_rate": (
            round(sum(1 for r in negatives if r.status == "NOT_COVERED") / len(negatives), 4)
            if negatives
            else 0.0
        ),
        "latency_p95_s": round(latency_p95, 3),
        "rerank_provider": next((r.rerank_provider for r in results if r.rerank_provider), ""),
        "status_counts": {
            status: sum(1 for r in results if r.status == status)
            for status in sorted({r.status for r in results})
        },
    }
    verdicts = []
    for name, target, degraded, mandatory in TARGETS:
        value = float(summary.get(name, 0.0))
        if value >= target:
            verdict, threshold = "达标", target
        elif value >= degraded:
            verdict, threshold = "仅达降级线", degraded
        else:
            verdict = "未达标"
            threshold = target
        verdicts.append(
            {
                "metric": name,
                "value": value,
                "target": target,
                "degraded_target": degraded,
                "mandatory": mandatory,
                "verdict": verdict,
                "threshold_used": threshold,
            }
        )
    summary["verdicts"] = verdicts
    return summary


def print_summary(summary: dict[str, Any], results: Sequence[ItemResult]) -> None:
    print("=" * 78)
    print("评测结果（开发文档 8.3 验收标准）")
    print("=" * 78)
    print(f"  样例 {summary['n_items']} 条（正 {summary['n_positive']} / 负 {summary['n_negative']}）")
    print(f"  status 分布：{summary['status_counts']}")
    print("-" * 78)
    for row in summary["verdicts"]:
        flag = "★不可降级" if row["mandatory"] else ""
        print(
            f"  {row['metric']:<24} {row['value']:>7.2%}  目标 {row['target']:.0%}"
            f"（降级线 {row['degraded_target']:.0%}）  → {row['verdict']} {flag}"
        )
    print("-" * 78)
    print(f"  MRR {summary['mrr']:.4f} / NDCG@5 {summary['ndcg5']:.4f}")
    print(
        f"  答案要点命中：{summary['answer_points']}（{summary['answer_point_hit_rate']:.1%}）"
        f"／仅看答案正文 {summary['answer_point_hit_rate_strict']:.1%}"
    )
    print(f"  引用真实性：{summary['citation_precision']:.1%}（{summary['citation_precision_note']}）")
    print(f"  伪造引用总数：{summary['fake_citation_count']}（必须为 0）")
    print(
        f"  文字类延迟 P95：{summary['latency_p95_s']}s（目标 ≤ 8s）"
        f"／预热 {summary['warmup_s']}s（模型加载，生产由 lifespan 承担）"
    )
    print(
        f"  精排提供方：{summary.get('rerank_provider') or '-'}"
        "（cross-encoder = 真 Cross-Encoder；lexical = 无模型时的确定性降级）"
    )
    print("-" * 78)
    print("  逐条：")
    for result in results:
        mark = "✓" if (result.negative and result.status == "NOT_COVERED") or (
            not result.negative and result.status == "OK"
        ) else "✗"
        line = (
            f"    {mark} {result.id} {result.status:<11} "
            f"top5={result.top5_hit:.0f} cov={result.coverage:.0%} "
            f"要点={result.answer_points_hit}/{result.answer_points_total} "
            f"{result.latency_s:.2f}s  {result.question[:26]}"
        )
        print(line)
        for note in result.notes:
            print(f"        - {note}")
    print("=" * 78)


def append_badcases(results: Sequence[ItemResult], path: Path) -> int:
    """把失败样例追加到 data/eval/badcases.md（表头由人工维护）。"""

    failures = [
        r
        for r in results
        if r.notes
        and not (r.negative and r.status == "NOT_COVERED")
        or r.fake_citations
    ]
    if not failures:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"| {stamp} | {r.id} | {r.question[:40]} | "
        f"status={r.status} top5={r.top5_hit:.0f} cov={r.coverage:.0%} "
        f"要点={r.answer_points_hit}/{r.answer_points_total} | {'；'.join(r.notes) or '指标未达预期'} | 待处理 |"
        for r in failures
    ]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(failures)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跑黄金集出指标")
    parser.add_argument("--golden", type=Path, default=None, help="黄金集路径（缺省 data/eval/golden_qa.jsonl）")
    parser.add_argument("--images", type=Path, default=None, help="图片类样例（默认跳过）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--out", type=Path, default=None, help="报告 JSON 输出路径")
    parser.add_argument("--badcases", action="store_true", help="失败样例追加到 badcases.md")
    parser.add_argument("--ids", default=None, help="只跑指定 id，逗号分隔，如 q001,q002")
    return parser


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    golden = args.golden or (ROOT / "data" / "eval" / "golden_qa.jsonl")
    if not golden.exists():
        print(f"黄金集不存在：{golden}")
        return 1
    items = load_jsonl(golden)
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [it for it in items if str(it.get("id")) in wanted]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有可评测的样例")
        return 1

    if args.images and args.images.exists():
        image_items = load_jsonl(args.images)
        print(f"图片类样例 {len(image_items)} 条：需要 fixtures 图片与多模态节点（平台侧），本次跳过")

    graph = compile_graph()

    # 预热：模型加载（embedding + 精排）与索引懒加载都发生在首个请求上。
    # 生产环境由 backend/main.py 的 lifespan 预热，因此这里也先跑一次不计入统计，
    # 否则会把「启动 10s」当成「单次问答 10s」，P95 指标失真。
    warmup_started = time.perf_counter()
    try:
        await graph.ainvoke(
            make_initial_state("预热：设备维护知识库", device_model=None, category=None)
        )
    except Exception as exc:  # noqa: BLE001 - 预热失败不阻断评测
        print(f"预热失败（不影响评测）：{type(exc).__name__}: {exc}")
    warmup_s = time.perf_counter() - warmup_started

    results: list[ItemResult] = []
    print(f"开始评测：{len(items)} 条（图：{len(graph.get_graph().nodes) - 2} 个节点）")
    print(f"预热耗时 {warmup_s:.2f}s（模型加载 + 索引懒加载，不计入 P95）\n")
    for item in items:
        state = make_initial_state(
            str(item.get("question", "")),
            device_model=item.get("device_model"),
            category=item.get("category"),
            answer_mode="qa",
        )
        started = time.perf_counter()
        try:
            result = await graph.ainvoke(state)
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整轮评测
            latency = time.perf_counter() - started
            failed = ItemResult(
                id=str(item.get("id", "")),
                question=str(item.get("question", "")),
                negative=bool(item.get("negative")),
                status="ERROR",
                latency_s=latency,
                confidence=0.0,
                notes=[f"链路异常：{type(exc).__name__}: {exc}"],
            )
            results.append(failed)
            print(f"  ✗ {failed.id} 链路异常：{exc}")
            continue
        latency = time.perf_counter() - started
        results.append(evaluate_item(item, result, latency))

    latencies = [r.latency_s for r in results if r.status != "ERROR"]
    p95 = statistics.quantiles(latencies, n=20)[-1] if len(latencies) >= 20 else (
        max(latencies) if latencies else 0.0
    )
    summary = summarize(results, latency_p95=p95)
    summary["warmup_s"] = round(warmup_s, 3)
    print_summary(summary, results)

    report_path = args.out or (ROOT / "data" / "eval" / f"report_{datetime.now():%Y%m%d_%H%M%S}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "golden": str(golden),
                "provider": (await (await __import__("backend.rag.store", fromlist=["get_store"]).get_store()).stats())["provider"],
                "summary": summary,
                "items": [r.to_dict() for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"报告已写入：{report_path}")

    if args.badcases:
        added = append_badcases(results, settings.badcases_path)
        print(f"badcase 追加 {added} 条 → {settings.badcases_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
