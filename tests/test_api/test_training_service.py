"""考核认证 service 测试（出题 / 判分 / 发证 / 到期提醒）。

覆盖（对齐交付要求的 7 条）：
    ① 出题成功且**每题 evidence 非空**（真实语料：5 篇 / 32 切片已入库）
    ② 判分正确（单选 / 判断 / 多选 / 简答各一例，含「全对」与「全错」）
    ③ `passed` 与分数计算正确（含 PASS_LINE 边界）
    ④ 未通过的 attempt 发证被拒（400 ATTEMPT_NOT_PASSED）
    ⑤ 通过的 attempt 发证成功且 `expires_at == issued_at + valid_days`
    ⑥ `list_expiring` 能捞出即将到期 / 已过期（吊销的不提醒）
    ⑦ 找不到 quiz 返回 None（另含 422 不出无依据之题、LLM 路径丢弃非法题）
    ⑧ 数值溯源两级口径：干扰项可跨片段取真实值，自造数值仍被拦；
       正确答案（含简答要点）必须落在所引片段；缩水时如实回传 requested/dropped

不依赖 DEEPSEEK_API_KEY：`tests/conftest.py` 的 autouse 夹具默认清空云端密钥，
本文件又在用例内显式 monkeypatch `llm_client.available -> False` / 假 `chat_json`，
所以有密钥与无密钥环境下走的都是同一条确定性路径（不发起任何外部请求）。

运行：
    .venv/bin/python -m pytest tests/test_api/test_training_service.py -q
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from backend import db  # noqa: E402
from backend.schemas import (  # noqa: E402
    CertificationListResponse,
    CertificationRequest,
    CertificationResponse,
    QuizGenerateRequest,
    QuizResponse,
    QuizSubmitRequest,
    QuizSubmitResponse,
    ServiceError,
)
from backend.services import training_service  # noqa: E402
from backend.setting import get_settings  # noqa: E402

#: 唯一标记本次测试数据，避免污染（也便于人工清理）
RUN_TAG = uuid.uuid4().hex[:8]
TRAINEE_GRADE = f"TEST-TRAIN-G-{RUN_TAG}"
TRAINEE_CERT = f"TEST-TRAIN-C-{RUN_TAG}"
TRAINEE_EXP = f"TEST-TRAIN-E-{RUN_TAG}"

#: 不存在的 id（SQLite INTEGER 为 64 位，给一个远超自增范围的值即可）
MISSING_QUIZ_ID = 9_999_999_999
MISSING_ATTEMPT_ID = 9_999_999_998


async def _with_db(scenario: Awaitable[Any]) -> Any:
    """每个场景自带一次 init_db / dispose_db（连接不跨事件循环复用）。

    必须与场景**在同一个事件循环内**：`db._engine` 是模块级全局缓存，
    若在 loop A 里 init、在 loop B 里用，MySQL（aiomysql）会报
    「Future attached to a different loop」；SQLite 的连接实现恰好容忍这种用法，
    所以这个错误只在 MySQL 下暴露（与 test_parts_service.py 同一范式）。
    """

    ok, detail = await db.init_db()
    assert ok, f"数据库不可用：{detail}"
    try:
        return await scenario
    finally:
        await db.dispose_db()


def _run_db(scenario: Awaitable[Any]) -> Any:
    return asyncio.run(_with_db(scenario))


# --------------------------------------------------------------------------- #
# 夹具数据：一张四题型齐全的试卷（直接入库，判分用，与出题解耦）
# --------------------------------------------------------------------------- #


def _evidence(chunk_id: str = "c_fixture_0001") -> list[dict[str, Any]]:
    """与 retriever.evidence_to_citations 同形的引用项。"""

    return [
        {
            "id": 1,
            "source_type": "kb_doc",
            "doc": "刻蚀设备维护手册",
            "version": "V3.2",
            "section": "3.5",
            "page": 4,
            "chunk_id": chunk_id,
            "image_url": None,
            "snippet": "干泵 DP-600 排气过滤器每 4000 运行小时更换一次；罗茨泵 RP-300 换油周期为 4000 运行小时或 12 个月。",
        }
    ]


QUIZ_ITEMS: list[dict[str, Any]] = [
    {
        "no": 1,
        "question": "干泵 DP-600 排气过滤器多久更换一次？",
        "type": "single",
        "options": ["4000 运行小时", "8000 运行小时", "12 个月", "90 天"],
        "answer": 0,
        "explanation": "原文：排气过滤器每 4000 运行小时更换一次。",
        "evidence": _evidence(),
    },
    {
        "no": 2,
        "question": "判断：罗茨泵 RP-300 的换油周期为 4000 运行小时或 12 个月，以先到者为准。",
        "type": "judgement",
        "options": ["正确", "错误"],
        "answer": True,
        "explanation": "原文：换油周期为 4000 运行小时或 12 个月，以先到者为准。",
        "evidence": _evidence(),
    },
    {
        "no": 3,
        "question": "关于 Etcher-A 真空系统的下列说法，正确的有哪些？（多选）",
        "type": "multiple",
        "options": [
            "主抽干泵 DP-600 抽速 600 m³/h",
            "主抽干泵 DP-600 抽速 300 m³/h",
            "罗茨泵 RP-300 抽速 300 m³/h",
            "分子泵 TMP-1600 额定转速 300 r/min",
        ],
        "answer": [0, 2],
        "explanation": "原文：DP-600 抽速 600 m³/h，RP-300 抽速 300 m³/h。",
        "evidence": _evidence(),
    },
    {
        "no": 4,
        "question": "请写出罗茨泵 RP-300 的换油周期要点（至少两项）。",
        "type": "short",
        "options": [],
        "answer": ["4000 运行小时", "12 个月"],
        "explanation": "原文：换油周期为 4000 运行小时或 12 个月，以先到者为准。",
        "evidence": _evidence(),
    },
]

#: 全部答对（单选下标 / 判断 bool / 多选完全一致 / 简答覆盖两个要点）
ANSWERS_ALL_RIGHT: list[Any] = [
    0,
    True,
    [0, 2],
    "换油周期为 4000 运行小时或 12 个月，以先到者为准",
]
#: 全部答错
ANSWERS_ALL_WRONG: list[Any] = [1, False, [1, 3], "与题目无关的回答"]
#: 2 对 2 错（多选只选了一个，属「不完全一致」→ 判错；简答两个要点都写到 → 判对）
ANSWERS_HALF: list[Any] = [0, False, [0], "换油周期为 4000 运行小时或 12 个月，以先到者为准"]
#: 3 对 1 错
ANSWERS_MOSTLY: list[Any] = [0, True, [1, 3], "4000 运行小时 与 12 个月"]


def _insert_quiz(items: list[dict[str, Any]], *, topic: str) -> int:
    """直接入库一张试卷，返回 quiz_id（判分测试不依赖出题链路）。"""

    async def _inner() -> int:
        row = db.TrainingQuiz(
            device_model="Etcher-A",
            topic=topic,
            level="basic",
            n_items=len(items),
            items=items,
            generator="test-fixture",
            created_at=datetime.now(timezone.utc),
        )
        async with db.session_scope() as session:
            session.add(row)
            await session.flush()
            return int(row.id or 0)

    return _run_db(_inner())


def _submit(quiz_id: int, trainee: str, answers: list[Any]) -> dict[str, Any]:
    payload = QuizSubmitRequest(trainee=trainee, answers=answers, duration_s=123.0)
    result = _run_db(training_service.submit_attempt(quiz_id, payload, trace_id="test-submit"))
    assert result is not None
    return result


def _insert_certification(
    *,
    trainee: str,
    device_model: str,
    expires_in_days: int,
    status: str = "valid",
    level: str = "L1",
) -> int:
    """直接入库一条认证（用于构造「已过期」「已吊销」等提醒场景）。"""

    async def _inner() -> int:
        now = datetime.now(timezone.utc)
        row = db.Certification(
            trainee=trainee,
            device_model=device_model,
            level=level,
            attempt_id=0,
            quiz_id=0,
            score=88.0,
            issuer="内部授权",
            issued_at=now - timedelta(days=365),
            expires_at=now + timedelta(days=expires_in_days),
            status=status,
            note="test fixture",
            created_at=now,
        )
        async with db.session_scope() as session:
            session.add(row)
            await session.flush()
            return int(row.id or 0)

    return _run_db(_inner())


# --------------------------------------------------------------------------- #
# ① 出题：真实语料 + 每题 evidence 非空
# --------------------------------------------------------------------------- #


def test_generate_quiz_every_item_has_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级路径出题：真实语料出题成功，且每题都带非空 evidence。"""

    monkeypatch.setattr(training_service.llm_client, "available", lambda: False)

    payload = QuizGenerateRequest(
        device_model="Etcher-A",
        topic="真空系统",
        level="basic",
        n_items=4,
        question_types=["single", "judgement", "multiple", "short"],
    )
    result = _run_db(training_service.generate_quiz(payload, trace_id="test-generate"))

    assert result["id"] > 0
    assert result["generator"] == "extractive-fallback"
    assert result["n_items"] == len(result["items"]) >= 1
    assert result["created_at"]

    for item in result["items"]:
        # 零幻觉硬要求：每题都必须能给出来源
        assert item["evidence"], f"第 {item['no']} 题没有依据"
        citation = item["evidence"][0]
        assert citation["chunk_id"] and citation["doc"] and citation["page"]
        assert item["question"].strip() and item["explanation"].strip()
        assert item["type"] in {"single", "judgement", "multiple", "short"}

        if item["type"] == "single":
            assert isinstance(item["answer"], int)
            assert 0 <= item["answer"] < len(item["options"]) >= 2
        elif item["type"] == "judgement":
            assert isinstance(item["answer"], bool)
        elif item["type"] == "multiple":
            assert item["answer"] and all(0 <= idx < len(item["options"]) for idx in item["answer"])
        else:
            assert item["answer"] and all(str(point).strip() for point in item["answer"])

    # 契约：service 返回值必须能直接过响应模型
    QuizResponse.model_validate(result)

    # 落库可读回
    fetched = _run_db(training_service.get_quiz(result["id"], trace_id="test-generate"))
    assert fetched is not None
    assert fetched["id"] == result["id"]
    assert fetched["n_items"] == result["n_items"]
    assert fetched["items"][0]["evidence"] == result["items"][0]["evidence"]


def test_generate_quiz_without_evidence_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """检索不到任何片段时宁可不出题（422），也不生成无依据的题。"""

    monkeypatch.setattr(training_service.llm_client, "available", lambda: False)

    payload = QuizGenerateRequest(device_model="不存在的型号-ZZZ", topic="不存在", n_items=2)
    with pytest.raises(ServiceError) as excinfo:
        _run_db(training_service.generate_quiz(payload, trace_id="test-empty"))

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "QUIZ_GENERATION_FAILED"


def test_generate_quiz_rejects_unknown_question_type() -> None:
    """未知题型直接 400，而不是静默换成别的题型。"""

    payload = QuizGenerateRequest(device_model="Etcher-A", question_types=["essay"])
    with pytest.raises(ServiceError) as excinfo:
        _run_db(training_service.generate_quiz(payload, trace_id="test-bad-type"))
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------- #
# LLM 路径：evidence_index 越界 / 缺失、数值编造的题目必须丢弃
# --------------------------------------------------------------------------- #


def test_llm_path_drops_items_with_invalid_evidence_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """假 chat_json：越界/缺失 evidence_index 与编造数值的题被丢弃，只留合规题。"""

    settings = get_settings()
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-not-used")
    captured: dict[str, Any] = {}

    async def fake_chat_json(messages: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        captured["messages"] = messages
        return [
            {  # 合法：evidence_index=1，题干无数值 → 保留
                "question": "日常点检由设备操作员在首片生产前完成，说法是否正确？",
                "type": "judgement",
                "options": [],
                "answer": True,
                "explanation": "片段 1 原文如此",
                "evidence_index": 1,
            },
            {  # evidence_index 越界 → 丢弃
                "question": "以上说法都正确吗？",
                "type": "judgement",
                "options": [],
                "answer": True,
                "explanation": "x",
                "evidence_index": 99,
            },
            {  # 缺 evidence_index → 丢弃（不猜是哪一段）
                "question": "本手册适用于哪种设备？",
                "type": "judgement",
                "options": [],
                "answer": True,
                "explanation": "x",
            },
            {  # 数值在片段原文中查不到（编造干扰项）→ 丢弃
                "question": "腔体净容积是多少？",
                "type": "single",
                "options": ["7777 L", "8888 L"],
                "answer": 0,
                "explanation": "x",
                "evidence_index": 1,
            },
        ]

    monkeypatch.setattr(training_service.llm_client, "chat_json", fake_chat_json)

    payload = QuizGenerateRequest(
        device_model="Etcher-A", topic="日常点检", n_items=4, question_types=["single", "judgement"]
    )
    result = _run_db(training_service.generate_quiz(payload, trace_id="test-llm"))

    assert result["generator"] == f"llm:{settings.deepseek_model}"
    assert result["n_items"] == 1, "只有 1 道题同时满足 evidence_index 与数值可溯源"
    item = result["items"][0]
    assert item["type"] == "judgement" and item["answer"] is True
    assert item["evidence"] and item["evidence"][0]["id"] == 1

    # 提示词里必须给出片段编号与 evidence_index 要求（否则模型无从给出合法序号）
    prompt = captured["messages"][-1]["content"]
    assert "[1]" in prompt and "evidence_index" in prompt


# --------------------------------------------------------------------------- #
# 数值溯源的两级口径：题干/正确答案须落在所引片段，干扰项允许跨段取真实值
# --------------------------------------------------------------------------- #


def _block(index: int, text: str) -> Any:
    """造一个可直接喂给校验器的片段（metadata 只需满足 _evidence_for 读取）。"""

    from backend.agents.state import Evidence

    return Evidence(
        chunk_id=f"c_test_{index}",
        text=text,
        score=1.0,
        metadata={
            "doc_title": f"测试手册-{index}",
            "version": "V1.0",
            "page": index,
            "section": f"{index}.1",
        },
    )


def test_distractor_may_come_from_another_block() -> None:
    """干扰项取自**另一段**的真实数值 → 必须保留（回归：曾因误杀导致出题缩水）。

    真实场景：极限压力 3.0×10⁻² 在 §1.1、5.0×10⁻² 在 §3.5，任何单段都不同时含两者，
    旧口径下「谁引谁死」，题目被成批丢弃。
    """

    from backend.services import training_service as ts

    blocks = [
        _block(1, "主抽干式真空泵 DP-600 的极限压力为 3.0×10⁻² Pa，抽速 600 m³/h。"),
        _block(2, "极限压力高于 5.0×10⁻² Pa 时须返修，额定转速 42000 r/min。"),
    ]
    # 正确答案 3.0×10⁻² 落在所引的片段 1；干扰项 5.0×10⁻²、42000 来自片段 2
    candidate = {
        "question": "DP-600 的极限压力是多少？",
        "type": "single",
        "options": ["3.0×10⁻² Pa", "5.0×10⁻² Pa", "42000 r/min", "600 m³/h"],
        "answer": 0,
        "explanation": "片段 1 原文",
        "evidence_index": 1,
    }
    item, reason = ts._validate_llm_item(candidate, blocks, ["single"])
    assert item is not None, f"跨段干扰项被误杀：{reason}"
    assert item["answer"] == 0


def test_distractor_still_rejects_fabricated_number() -> None:
    """干扰项里的自造数值（全语料都没有）→ 仍然丢弃（防编造红线不放宽）。"""

    from backend.services import training_service as ts

    blocks = [
        _block(1, "主抽干式真空泵 DP-600 的极限压力为 3.0×10⁻² Pa，抽速 600 m³/h。"),
    ]
    candidate = {
        "question": "DP-600 的极限压力是多少？",
        "type": "single",
        "options": ["3.0×10⁻² Pa", "7777 Pa"],
        "answer": 0,
        "explanation": "x",
        "evidence_index": 1,
    }
    item, reason = ts._validate_llm_item(candidate, blocks, ["single"])
    assert item is None
    assert "干扰项" in reason and "7777" in reason


def test_correct_answer_must_be_traceable_to_cited_block() -> None:
    """正确答案的数值不在**本题所引那一段**里 → 丢弃（防「引用撑不起答案」）。"""

    from backend.services import training_service as ts

    blocks = [
        _block(1, "主抽干式真空泵 DP-600 的抽速为 600 m³/h。"),
        _block(2, "极限压力高于 5.0×10⁻² Pa 时须返修。"),
    ]
    # 正确答案用了片段 2 的 5.0×10⁻²，却声称依据是片段 1
    candidate = {
        "question": "须返修的极限压力阈值是多少？",
        "type": "single",
        "options": ["5.0×10⁻² Pa", "3.0×10⁻² Pa"],
        "answer": 0,
        "explanation": "x",
        "evidence_index": 1,
    }
    item, reason = ts._validate_llm_item(candidate, blocks, ["single"])
    assert item is None
    assert "正确答案" in reason


def test_short_answer_points_traced_to_cited_block() -> None:
    """简答题的要点属于「正确答案」，跨段取值同样要被拦下。"""

    from backend.services import training_service as ts

    blocks = [
        _block(1, "主抽干式真空泵 DP-600 的抽速为 600 m³/h。"),
        _block(2, "极限压力高于 5.0×10⁻² Pa 时须返修。"),
    ]
    candidate = {
        "question": "干泵返修的判据是什么？",
        "type": "short",
        "options": [],
        "answer": ["5.0×10⁻² Pa"],
        "explanation": "x",
        "evidence_index": 1,
    }
    item, reason = ts._validate_llm_item(candidate, blocks, ["short"])
    assert item is None
    assert "正确答案" in reason


def test_generate_quiz_reports_requested_and_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """出题缩水必须如实回传 requested_items / dropped_items，不静默。"""

    settings = get_settings()
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-not-used")

    async def fake_chat_json(messages: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {  # 合规
                "question": "日常点检由设备操作员在首片生产前完成，说法是否正确？",
                "type": "judgement",
                "options": [],
                "answer": True,
                "explanation": "片段 1 原文如此",
                "evidence_index": 1,
            },
            {  # 缺 evidence_index → 丢弃
                "question": "本手册适用于哪种设备？",
                "type": "judgement",
                "options": [],
                "answer": True,
                "explanation": "x",
            },
        ]

    monkeypatch.setattr(training_service.llm_client, "chat_json", fake_chat_json)

    payload = QuizGenerateRequest(
        device_model="Etcher-A", topic="日常点检", n_items=2,
        question_types=["single", "judgement"],
    )
    result = _run_db(training_service.generate_quiz(payload, trace_id="test-shortfall"))

    assert result["requested_items"] == 2
    assert result["n_items"] == 1
    assert result["dropped_items"] == 1

    # 回看试卷时这两个值不可知，必须给 None（不能编一个 0）
    fetched = _run_db(training_service.get_quiz(result["id"], trace_id="test-shortfall"))
    assert fetched is not None
    assert fetched["requested_items"] is None
    assert fetched["dropped_items"] is None


# --------------------------------------------------------------------------- #
# ② ③ 判分：全对 / 全错 / 部分对，分数与 passed
# --------------------------------------------------------------------------- #


def test_grading_all_correct_and_all_wrong() -> None:
    quiz_id = _insert_quiz(QUIZ_ITEMS, topic=f"judge-all-{RUN_TAG}")

    right = _submit(quiz_id, TRAINEE_GRADE, ANSWERS_ALL_RIGHT)
    assert right["quiz_id"] == quiz_id
    assert right["trainee"] == TRAINEE_GRADE
    assert right["score"] == 100.0
    assert right["passed"] is True
    assert right["pass_line"] == training_service.PASS_LINE == 60.0
    assert [entry["correct"] for entry in right["detail"]] == [True, True, True, True]
    assert [entry["no"] for entry in right["detail"]] == [1, 2, 3, 4]
    # 判分不自动发证：发证必须显式调用 issue_certification
    assert right["certification_id"] is None
    assert all(entry["evidence"] for entry in right["detail"])
    QuizSubmitResponse.model_validate(right)

    wrong = _submit(quiz_id, TRAINEE_GRADE, ANSWERS_ALL_WRONG)
    assert wrong["attempt_id"] != right["attempt_id"]
    assert wrong["score"] == 0.0
    assert wrong["passed"] is False
    assert all(entry["correct"] is False for entry in wrong["detail"])
    # 批改明细里带上正确答案与依据，便于人工复核
    assert wrong["detail"][0]["expected"] == 0 and wrong["detail"][0]["got"] == 1
    assert wrong["detail"][3]["expected"] == ["4000 运行小时", "12 个月"]

    # 判分没有顺手发证
    listed = _run_db(
        training_service.list_certifications(
            trainee=TRAINEE_GRADE,
            device_model=None,
            status=None,
            limit=20,
            offset=0,
            trace_id="test-list",
        )
    )
    assert listed["total"] == 0


def test_grading_partial_score_and_pass_line() -> None:
    quiz_id = _insert_quiz(QUIZ_ITEMS, topic=f"judge-partial-{RUN_TAG}")

    half = _submit(quiz_id, TRAINEE_GRADE, ANSWERS_HALF)  # 单选对 / 判断错 / 多选不全 / 简答命中 1/2
    assert [entry["correct"] for entry in half["detail"]] == [True, False, False, True]
    assert half["score"] == 50.0, "2/4 * 100 保留 1 位小数"
    assert half["passed"] is False, "50.0 < PASS_LINE(60)"

    mostly = _submit(quiz_id, TRAINEE_GRADE, ANSWERS_MOSTLY)  # 3 对 1 错
    assert mostly["score"] == 75.0
    assert mostly["passed"] is True, "75.0 >= PASS_LINE(60)"

    # 简答阈值边界：2 个要点只命中 1 个 = 0.5 < 0.6 → 判错
    only_one_point = _submit(quiz_id, TRAINEE_GRADE, [0, True, [0, 2], "只有 4000 运行小时"])
    assert only_one_point["detail"][3]["correct"] is False
    assert only_one_point["score"] == 75.0

    # 判断型的宽容写法（"正确"/"错误" 文本）同样能判对
    tolerant = _submit(quiz_id, TRAINEE_GRADE, [0, "正确", "0,2", ANSWERS_ALL_RIGHT[3]])
    assert tolerant["detail"][1]["correct"] is True
    assert tolerant["detail"][2]["correct"] is True
    assert tolerant["score"] == 100.0


# --------------------------------------------------------------------------- #
# ④ ⑤ 发证：未通过被拒 / 通过后成功且到期日正确
# --------------------------------------------------------------------------- #


def test_issue_certification_requires_passed_attempt() -> None:
    quiz_id = _insert_quiz(QUIZ_ITEMS, topic=f"cert-reject-{RUN_TAG}")
    failed = _submit(quiz_id, TRAINEE_CERT, ANSWERS_ALL_WRONG)
    assert failed["passed"] is False

    with pytest.raises(ServiceError) as excinfo:
        _run_db(
            training_service.issue_certification(
                CertificationRequest(
                    trainee=TRAINEE_CERT,
                    device_model="Etcher-A",
                    level="L2",
                    attempt_id=failed["attempt_id"],
                    valid_days=365,
                ),
                trace_id="test-cert-reject",
            )
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "ATTEMPT_NOT_PASSED"
    assert "通过的考核" in excinfo.value.message

    # 不存在的考核记录 → 404（不是 400）
    with pytest.raises(ServiceError) as missing:
        _run_db(
            training_service.issue_certification(
                CertificationRequest(
                    trainee=TRAINEE_CERT,
                    device_model="Etcher-A",
                    attempt_id=MISSING_ATTEMPT_ID,
                ),
                trace_id="test-cert-missing",
            )
        )
    assert missing.value.status_code == 404
    assert missing.value.code == "ATTEMPT_NOT_FOUND"


def test_issue_certification_success_and_expiry() -> None:
    quiz_id = _insert_quiz(QUIZ_ITEMS, topic=f"cert-ok-{RUN_TAG}")
    passed = _submit(quiz_id, TRAINEE_CERT, ANSWERS_ALL_RIGHT)
    assert passed["passed"] is True

    cert = _run_db(
        training_service.issue_certification(
            CertificationRequest(
                trainee=TRAINEE_CERT,
                device_model="Etcher-A",
                level="L2",
                attempt_id=passed["attempt_id"],
                valid_days=365,
                note="年度复训",
            ),
            trace_id="test-cert-ok",
        )
    )

    assert cert["id"] > 0
    assert cert["trainee"] == TRAINEE_CERT
    assert cert["device_model"] == "Etcher-A"
    assert cert["level"] == "L2"
    assert cert["status"] == "valid"
    assert cert["issuer"] == "内部授权", "系统不代表原厂发证"
    assert "原厂" in cert["note"] and "年度复训" in cert["note"]
    assert cert["attempt_id"] == passed["attempt_id"]
    assert cert["quiz_id"] == quiz_id
    assert cert["score"] == passed["score"] == 100.0

    issued_at = datetime.fromisoformat(cert["issued_at"])
    expires_at = datetime.fromisoformat(cert["expires_at"])
    assert issued_at.tzinfo is not None and expires_at.tzinfo is not None
    assert expires_at - issued_at == timedelta(days=365)
    assert cert["days_to_expiry"] in (364, 365)
    CertificationResponse.model_validate(cert)

    listed = _run_db(
        training_service.list_certifications(
            trainee=TRAINEE_CERT,
            device_model="Etcher-A",
            status="valid",
            limit=20,
            offset=0,
            trace_id="test-cert-list",
        )
    )
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == cert["id"]
    CertificationListResponse.model_validate(listed)

    # 状态过滤生效
    none_valid = _run_db(
        training_service.list_certifications(
            trainee=TRAINEE_CERT,
            device_model=None,
            status="revoked",
            limit=20,
            offset=0,
            trace_id="test-cert-list",
        )
    )
    assert none_valid["total"] == 0


# --------------------------------------------------------------------------- #
# ⑥ 到期提醒
# --------------------------------------------------------------------------- #


def test_list_expiring_finds_soon_and_overdue(monkeypatch: pytest.MonkeyPatch) -> None:
    quiz_id = _insert_quiz(QUIZ_ITEMS, topic=f"expiring-{RUN_TAG}")
    passed = _submit(quiz_id, TRAINEE_EXP, ANSWERS_ALL_RIGHT)
    assert passed["passed"] is True

    # ① 即将到期：有效期只有 1 天
    soon = _run_db(
        training_service.issue_certification(
            CertificationRequest(
                trainee=TRAINEE_EXP,
                device_model="Etcher-A",
                level="L1",
                attempt_id=passed["attempt_id"],
                valid_days=1,
            ),
            trace_id="test-expiring",
        )
    )
    # ② 已过期 5 天（直接入库构造）
    overdue = _insert_certification(
        trainee=TRAINEE_EXP, device_model="Etcher-A", expires_in_days=-5
    )
    # ③ 已吊销且本月到期：提醒里不应出现
    revoked = _insert_certification(
        trainee=TRAINEE_EXP, device_model="Etcher-A", expires_in_days=10, status="revoked"
    )
    # ④ 还早（365 天后到期）：不在 30 天提醒窗口内
    far = _insert_certification(
        trainee=TRAINEE_EXP, device_model="Etcher-A", expires_in_days=365
    )

    result = _run_db(training_service.list_expiring(days=30, trace_id="test-expiring"))
    by_id = {item["id"]: item for item in result["items"]}
    ids = list(by_id)

    assert soon["id"] in ids, "即将到期的证书必须被提醒"
    assert overdue in ids, "已过期的证书必须被提醒（催复训）"
    assert revoked not in ids, "已吊销的证书不需要提醒"
    assert far not in ids, "365 天后到期的不在 30 天窗口内"

    # 距到期天数：过期的是负数，即将到期的是正数
    assert by_id[overdue]["days_to_expiry"] < 0
    assert by_id[soon["id"]]["days_to_expiry"] >= 1

    # 按 expires_at 升序
    stamps = [datetime.fromisoformat(item["expires_at"]) for item in result["items"]]
    assert stamps == sorted(stamps)
    assert result["total"] == len(result["items"])
    CertificationListResponse.model_validate(result)

    # 窗口放大后 365 天的那条会进来（确认不是被别的原因漏掉）
    wide = _run_db(training_service.list_expiring(days=400, trace_id="test-expiring-wide"))
    assert far in {item["id"] for item in wide["items"]}

    # 吊销记录仍能在认证列表里查到（只是不提醒）
    revoked_list = _run_db(
        training_service.list_certifications(
            trainee=TRAINEE_EXP,
            device_model=None,
            status="revoked",
            limit=20,
            offset=0,
            trace_id="test-expiring",
        )
    )
    assert revoked_list["total"] == 1


# --------------------------------------------------------------------------- #
# ⑦ 找不到 quiz → None
# --------------------------------------------------------------------------- #


def test_missing_quiz_returns_none() -> None:
    assert _run_db(training_service.get_quiz(MISSING_QUIZ_ID, trace_id="test-404")) is None
    assert (
        _run_db(
            training_service.submit_attempt(
                MISSING_QUIZ_ID,
                QuizSubmitRequest(trainee=TRAINEE_GRADE, answers=[0]),
                trace_id="test-404",
            )
        )
        is None
    )
