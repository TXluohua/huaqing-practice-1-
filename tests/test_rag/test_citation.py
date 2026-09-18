"""引用与拒答测试（开发文档 8.2 单元测试：引用校验，伪造引用必须被识别）。

这是项目的**质量底线**测试：引用真实性、引用覆盖率、置信度公式、拒答判定。
运行：
    .venv/bin/python -m pytest tests/test_rag/test_citation.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.state import Evidence  # noqa: E402
from backend.rag.citation import (  # noqa: E402
    anchor_check,
    enforce_citations,
    build_not_covered_answer,
    check_citations,
    compute_confidence,
    confidence_label,
    parse_citation_ids,
    should_reject,
)
from backend.setting import get_settings  # noqa: E402


def block(chunk_id: str, text: str, **meta) -> Evidence:
    base = {
        "doc_title": "刻蚀设备维护手册",
        "version": "V3.2",
        "page": 3,
        "section": "3.4",
        "category": "设备维护",
    }
    base.update(meta)
    return Evidence(chunk_id=chunk_id, text=text, score=1.0, metadata=base)


BLOCKS = [
    block("c_1", "腔体真空度异常时先检查腔体门 O-ring 有无压痕与开裂。"),
    block("c_2", "确认真空泵抽速：干泵 RP-300 额定抽速 300 m³/h。"),
]


# --------------------------------------------------------------------------- #
# 引用解析
# --------------------------------------------------------------------------- #
def test_parse_citation_ids_supports_common_forms() -> None:
    assert parse_citation_ids("结论一 [1]。") == [1]
    assert parse_citation_ids("多引用 [1,3] 与范围 [2-4]") == [1, 3, 2, 4]
    assert parse_citation_ids("全角［5］") == [5]
    assert parse_citation_ids("没有引用") == []


def test_check_citations_counts_coverage() -> None:
    answer = "腔体真空度异常先检查 O-ring [1]。真空泵抽速应确认达标 [2]。"
    result = check_citations(answer, BLOCKS)
    assert result.coverage == 1.0
    assert result.total_claims == 2 and result.cited_claims == 2


def test_fake_citation_is_detected() -> None:
    """伪造引用必须被识别（开发文档 8.2 明确要求）。"""

    answer = "腔体密封检查 [1]。这个型号的清洗周期是 30 天 [9]。"
    result = check_citations(answer, BLOCKS)
    assert result.fake_ids == [9]
    assert result.has_fake_citation is True
    assert result.coverage < 1.0


def test_reference_list_lines_are_not_claims() -> None:
    """文末「依据：」清单与纯引用行不算结论句，不应拉低覆盖率。"""

    answer = (
        "腔体真空度异常先检查 O-ring [1]。\n\n"
        "依据：\n- [1] 《刻蚀设备维护手册》 V3.2 第 3 页 章节 3.4\n"
    )
    result = check_citations(answer, BLOCKS)
    assert result.total_claims == 1, "依据清单不应计入结论句"
    assert result.coverage == 1.0


def test_disclaimer_sentences_are_exempt() -> None:
    answer = "知识库未覆盖该问题，不给出具体操作步骤。建议补充型号描述。"
    assert check_citations(answer, BLOCKS).total_claims == 0


# --------------------------------------------------------------------------- #
# 置信度
# --------------------------------------------------------------------------- #
def test_confidence_formula_and_labels() -> None:
    # 0.40×1.0 + 0.35×1.0 + 0.25×1.0 = 1.0
    assert compute_confidence(normalized_top=1.0, coverage=1.0, authority=1.0) == 1.0
    score = compute_confidence(normalized_top=0.5, coverage=1.0, authority=0.9)
    assert abs(score - (0.2 + 0.35 + 0.225)) < 1e-6

    settings = get_settings()
    assert confidence_label(0.9, settings=settings) == "high"
    assert confidence_label(0.7, settings=settings) == "medium"
    assert confidence_label(0.3, settings=settings) == "low"


# --------------------------------------------------------------------------- #
# 拒答判定
# --------------------------------------------------------------------------- #
def test_reject_when_no_evidence() -> None:
    decision = should_reject(
        answer="",
        blocks=[],
        check=check_citations("", []),
        normalized_top=0.0,
        confidence=0.0,
    )
    assert decision.reject is True
    assert decision.status == "NOT_COVERED"
    assert any("未检索到可用证据" in reason for reason in decision.reasons)


def test_reject_on_fake_citation_even_with_evidence() -> None:
    answer = "腔体密封检查 [1]。清洗周期 30 天 [7]。"
    check = check_citations(answer, BLOCKS)
    decision = should_reject(
        answer=answer,
        blocks=BLOCKS,
        check=check,
        normalized_top=0.9,
        confidence=0.9,
        question="腔体真空度异常怎么排查？",
    )
    assert decision.reject is True
    assert any("引用校验不通过" in reason for reason in decision.reasons)


def test_accept_when_everything_passes() -> None:
    answer = "腔体真空度异常先检查腔体门 O-ring [1]。再确认真空泵抽速 [2]。"
    check = check_citations(answer, BLOCKS)
    decision = should_reject(
        answer=answer,
        blocks=BLOCKS,
        check=check,
        normalized_top=0.9,
        confidence=0.9,
        question="腔体真空度异常怎么排查？",
        score_threshold=0.25,
    )
    assert decision.reject is False
    assert decision.status == "OK"


def test_not_covered_answer_gives_no_procedure() -> None:
    """拒答话术只给材料清单与建议，**不得给出操作步骤**。"""

    decision = should_reject(
        answer="",
        blocks=BLOCKS,
        check=check_citations("", BLOCKS),
        normalized_top=0.0,
        confidence=0.0,
    )
    text = build_not_covered_answer(decision)
    assert "知识库未覆盖" in text
    assert "刻蚀设备维护手册" in text, "应列出已检索到的材料"
    # 不得出现任何具体处置内容（模板里的「不给出具体操作步骤」是声明，不是步骤）
    for forbidden in ("O-ring", "先检查", "1.", "更换", "拧紧"):
        assert forbidden not in text, f"拒答话术不得包含处置内容：{forbidden}"


# --------------------------------------------------------------------------- #
# 锚点校验（负样本拒答的主要闸门）
# --------------------------------------------------------------------------- #
def test_anchor_check_detects_unknown_model() -> None:
    check = anchor_check("XH-900 型号腔体清洗周期是多少？", BLOCKS)
    assert check.ok is False
    assert check.missing_models == ["XH-900"]
    assert any("知识库未覆盖该型号" in reason for reason in check.reasons)


def test_anchor_check_passes_for_covered_question() -> None:
    check = anchor_check("腔体真空度异常时怎么检查 O-ring？", BLOCKS)
    assert check.ok is True
    assert check.failure_reasons == []


def test_anchor_check_flags_terms_absent_from_knowledge_base() -> None:
    corpus = " ".join(b.text for b in BLOCKS)
    check = anchor_check(
        "年度维保合同费用是多少？",
        BLOCKS,
        corpus_text=corpus,
        max_kb_missing_ratio=get_settings().anchor_kb_missing_ratio,
    )
    assert check.kb_missing_ratio > 0.4
    assert check.ok is False
    assert any("知识库中不存在" in reason for reason in check.reasons)


# --------------------------------------------------------------------------- #
# 引用归属（LLM 路径的覆盖率保障，实测缺陷 #10）
# --------------------------------------------------------------------------- #
def test_enforce_citations_attributes_and_drops() -> None:
    """缺引用的结论句：能归属的补上编号，归属不上的整句省略。"""

    answer = (
        "- 腔体真空度异常时先检查腔体门 O-ring [1]。\n"
        "- 干泵 RP-300 的额定抽速是 300 立方米每小时。\n"
        "- 今天股市大涨，建议买入科技股。"
    )
    fixed, stats = enforce_citations(answer, BLOCKS, threshold=0.45)
    assert stats["attributed"] == 1, stats
    assert stats["dropped"] == 1, stats
    assert "[2]" in fixed, fixed            # 归属到「真空泵抽速」那一块
    assert "股市" not in fixed              # 无依据的句子被省略
    assert check_citations(fixed, BLOCKS).coverage == 1.0


def test_enforce_citations_keeps_non_claim_lines() -> None:
    """标题、依据清单等非结论行原样保留（不能被当成无依据结论删掉）。"""

    answer = (
        "**基础理解**\n"
        "- 腔体真空度异常先检查 O-ring [1]。\n\n"
        "依据：\n- [1] 《刻蚀设备维护手册》 V3.2 第 3 页 章节 3.4"
    )
    fixed, stats = enforce_citations(answer, BLOCKS, threshold=0.45)
    assert "**基础理解**" in fixed
    assert "依据：" in fixed
    assert stats["dropped"] == 0
    assert fixed.count("[1]") >= 2


def test_enforce_citations_all_unattributable_yields_empty_and_rejects() -> None:
    """整条回答都没有依据时，答案被清空 → 仍然走拒答（fail-closed 不被破坏）。"""

    answer = "今天股市大涨，建议买入科技股。天气也不错，适合郊游。"
    fixed, stats = enforce_citations(answer, BLOCKS, threshold=0.45)
    assert fixed.strip() == ""
    assert stats["dropped"] >= 1

    decision = should_reject(
        answer=fixed,
        blocks=BLOCKS,
        check=check_citations(fixed, BLOCKS),
        normalized_top=0.9,
        confidence=0.9,
        question="今天天气怎么样？",
        score_threshold=0.25,
    )
    assert decision.reject is True and decision.status == "NOT_COVERED"


def test_partial_coverage_answer_is_repaired_not_rejected() -> None:
    """缺陷 #10 的回归：LLM 常见的「部分句子漏引用」应当被修复后放行，而不是整条拒答。"""

    llm_style = (
        "- 腔体真空度异常时应先检查腔体门 O-ring 有无压痕 [1]。\n"
        "- 再确认真空泵抽速是否达标。\n"
        "- 必要时检查压力传感器零点漂移。"
    )
    before = check_citations(llm_style, BLOCKS)
    assert before.coverage < 1.0, "构造的样例应当是不达标的"

    fixed, _stats = enforce_citations(llm_style, BLOCKS, threshold=0.45)
    after = check_citations(fixed, BLOCKS)
    assert after.coverage == 1.0, fixed

    decision = should_reject(
        answer=fixed,
        blocks=BLOCKS,
        check=after,
        normalized_top=0.9,
        confidence=0.9,
        question="腔体真空度异常怎么排查？",
        score_threshold=0.25,
    )
    assert decision.reject is False and decision.status == "OK"


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
