"""verify 节点测试（开发文档 8.2：引用校验 + 拒答；6.3：verify 不可绕过）。

verify 是确定性代码，这里直接喂人工构造的状态，不依赖知识库索引 ——
因此测试快、稳定，且能精确覆盖四道关的每一条。

运行：
    .venv/bin/python -m pytest tests/test_agents/test_verify.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.nodes.generation import generate, verify  # noqa: E402
from backend.agents.state import Evidence, make_initial_state  # noqa: E402


def block(chunk_id: str, text: str, **meta) -> Evidence:
    base = {
        "doc_title": "刻蚀设备维护手册",
        "version": "V3.2",
        "page": 3,
        "section": "3.4",
        "category": "设备维护",
        "rerank_normalized": 0.9,
        "rerank_provider": "lexical",
    }
    base.update(meta)
    return Evidence(chunk_id=chunk_id, text=text, score=1.0, metadata=base)


BLOCKS = [
    block("c_1", "腔体真空度异常时先检查腔体门 O-ring 有无压痕与开裂。"),
    block("c_2", "确认真空泵抽速：干泵 RP-300 额定抽速 300 m³/h。"),
]


def state_with(*, answer: str, blocks: list[Evidence], question: str = "腔体真空度异常怎么排查？"):
    state = make_initial_state(question, device_model="Etcher-A")
    state["context_blocks"] = blocks
    state["context"] = "\n\n".join(f"[{i}] {b.text}" for i, b in enumerate(blocks, start=1))
    state["ranked"] = blocks
    state["answer"] = answer
    state["answer_structured"] = {"generator": "test"}
    return state


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_verify_accepts_fully_cited_answer() -> None:
    state = state_with(
        answer="腔体真空度异常先检查腔体门 O-ring [1]。再确认真空泵抽速 [2]。",
        blocks=BLOCKS,
    )
    result = asyncio.run(verify(state))
    assert result["status"] == "OK"
    assert len(result["citations"]) == 2
    assert result["citations"][0].id == 1, "引用编号由代码分配，从 1 开始"
    assert result["citations"][0].doc == "刻蚀设备维护手册"
    assert result["citations"][0].page == 3
    assert result["confidence_label"] in {"high", "medium", "low"}
    verification = result["answer_structured"]["verification"]
    assert verification["coverage"] == 1.0
    assert verification["fake_citation_ids"] == []


def test_verify_rejects_fake_citation_and_clears_citations() -> None:
    state = state_with(
        answer="腔体真空度先检查 O-ring [1]。清洗周期是 30 天 [9]。",
        blocks=BLOCKS,
    )
    result = asyncio.run(verify(state))
    assert result["status"] == "NOT_COVERED"
    assert result["citations"] == [], "拒答时不得返回引用"
    assert any("引用校验不通过" in note for note in result["uncertain"])
    assert result["answer_structured"]["verification"]["fake_citation_ids"] == [9]


def test_verify_rejects_when_question_is_out_of_scope() -> None:
    """问「知识库里没有的型号」必须拒答，而不是拿别的型号的参数硬答。"""

    state = state_with(
        answer="腔体真空度先检查 O-ring [1]。",
        blocks=BLOCKS,
        question="XH-900 型号腔体清洗周期是多少？",
    )
    result = asyncio.run(verify(state))
    assert result["status"] == "NOT_COVERED"
    assert any("XH-900" in note for note in result["uncertain"])


def test_verify_replaces_answer_with_not_covered_text() -> None:
    """拒答时答案必须被改写为「未覆盖」话术，且不含操作步骤。"""

    state = state_with(answer="", blocks=[], question="腔体真空度异常怎么排查？")
    result = asyncio.run(verify(state))
    assert result["status"] == "NOT_COVERED"
    assert "知识库未覆盖" in result["answer"]
    for forbidden in ("O-ring", "先检查", "1.", "更换"):
        assert forbidden not in result["answer"], f"拒答话术不得包含处置内容：{forbidden}"


def test_initial_state_is_fail_closed() -> None:
    """图内初始 status 必须是 NOT_COVERED：忘记判定时默认拒答，而不是默认放行。"""

    state = make_initial_state("任意问题")
    assert state["status"] == "NOT_COVERED"
    assert state["confidence"] == 0.0
    assert state["confidence_label"] == "low"


# --------------------------------------------------------------------------- #
# generate 节点
# --------------------------------------------------------------------------- #
def test_generate_without_blocks_returns_empty_answer() -> None:
    state = make_initial_state("腔体真空度异常怎么排查？")
    state["context_blocks"] = []
    result = asyncio.run(generate(state))
    assert result["answer"] == ""
    assert result["answer_structured"]["generator"] == "none"


def test_generate_fallback_attaches_citation_to_every_sentence() -> None:
    """抽句式降级路径：每句都挂引用，且引用编号落在句末标点之前。"""

    state = make_initial_state("腔体真空度异常怎么排查？")
    state["context_blocks"] = BLOCKS
    state["context"] = "\n\n".join(b.text for b in BLOCKS)
    result = asyncio.run(generate(state))
    assert result["answer_structured"]["generator"] == "extractive-fallback"
    assert "[1]" in result["answer"]
    assert " [1]。" in result["answer"], "引用必须写在句末标点之前（否则覆盖率统计会漏算）"

    # 结论句（去掉「依据：」清单后）应当 100% 带引用
    from backend.rag.citation import check_citations

    check = check_citations(result["answer"], state["context_blocks"])
    assert check.total_claims > 0
    assert check.coverage == 1.0


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
