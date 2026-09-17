"""多模态节点测试（FR-02 失败处理 / 接口文档 §6 image 事件）。

覆盖：
    1. 无图片时 ingest_image **零开销**（不写任何字段）
    2. 识别失败（工具报错 / unknown / 低置信度）-> image_result=None 且给出明确原因
    3. **识别失败必须给用户「能照做」的提示**（开发文档 §3.2：不允许猜测）
    4. 识别成功 -> image_result 就位，且 image 事件先于 token

    .venv/bin/python -m pytest tests/test_multimodal/test_vision.py -q
    .venv/bin/python tests/test_multimodal/test_vision.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.nodes import multimodal  # noqa: E402
from backend.agents.nodes.response import IMAGE_UNRECOGNIZED_NOTICE, build_image_notice, respond  # noqa: E402
from backend.agents.state import ImageExtraction, make_initial_state  # noqa: E402
from backend.schemas import ChatRequest  # noqa: E402


@contextlib.contextmanager
def patched_vision_extract(fake: Any):  # noqa: ANN201
    """替换 multimodal 模块里的 vision_extract（节点通过模块属性调用）。"""

    saved = multimodal.vision_extract
    multimodal.vision_extract = fake
    try:
        yield
    finally:
        multimodal.vision_extract = saved


def run_ingest(state: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(multimodal.ingest_image(state))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 1. 无图片：零开销
# --------------------------------------------------------------------------- #


def test_ingest_image_is_noop_without_images() -> None:
    assert run_ingest(make_initial_state("纯文本问题")) == {}
    assert run_ingest({**make_initial_state("x"), "image_ids": []}) == {}


# --------------------------------------------------------------------------- #
# 2. 识别失败：必须给原因，且不产出识别结果
# --------------------------------------------------------------------------- #


def test_tool_failure_marks_unrecognized() -> None:
    async def boom(image_id: str) -> dict[str, Any]:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")

    state = {**make_initial_state("这个报警怎么处理？"), "image_ids": ["img_1"]}
    with patched_vision_extract(boom):
        out = run_ingest(state)

    assert out["image_result"] is None
    assert any("未能识别" in item for item in out["errors"]), out["errors"]
    assert any("DASHSCOPE" in item for item in out["errors"]), out["errors"]


def test_unknown_type_or_low_confidence_marks_unrecognized() -> None:
    async def unknown(image_id: str) -> dict[str, Any]:
        return {"image_type": "unknown", "extracted": {}, "raw_text": "", "confidence": 0.3}

    state = {**make_initial_state("这是什么？"), "image_ids": ["img_1"]}
    with patched_vision_extract(unknown):
        out = run_ingest(state)

    # 仍然写入识别结果（供前端展示低置信度），但必须带「未能识别」提示
    assert out["image_result"] is not None
    assert out["image_result"].image_type == "unknown"
    assert any("未能识别" in item for item in out["errors"]), out["errors"]


def test_confidence_threshold_boundary() -> None:
    """阈值来自 setting.vision_min_confidence（默认 0.5），低一分即判未识别。"""

    async def below(image_id: str) -> dict[str, Any]:
        return {"image_type": "alarm_screen", "extracted": {"alarm_codes": ["E-2041"]}, "raw_text": "", "confidence": 0.49}

    state = {**make_initial_state("这个报警怎么处理？"), "image_ids": ["img_1"]}
    with patched_vision_extract(below):
        out = run_ingest(state)
    assert any("未能识别" in item for item in out["errors"]), out["errors"]

    async def above(image_id: str) -> dict[str, Any]:
        return {"image_type": "alarm_screen", "extracted": {"alarm_codes": ["E-2041"]}, "raw_text": "", "confidence": 0.6}

    with patched_vision_extract(above):
        out2 = run_ingest(state)
    assert not any("未能识别" in item for item in (out2.get("errors") or [])), out2.get("errors")


# --------------------------------------------------------------------------- #
# 3. 识别成功
# --------------------------------------------------------------------------- #


def test_successful_extraction_populates_image_result() -> None:
    async def ok(image_id: str) -> dict[str, Any]:
        return {
            "image_type": "alarm_screen",
            "extracted": {"alarm_codes": ["E-2041"], "device_model": "Etcher-A"},
            "raw_text": "ALARM E-2041 腔体压力超限",
            "confidence": 0.92,
        }

    state = {**make_initial_state("这个报警怎么处理？"), "image_ids": ["img_1"]}
    with patched_vision_extract(ok):
        out = run_ingest(state)

    result = out["image_result"]
    assert isinstance(result, ImageExtraction)
    assert result.image_type == "alarm_screen"
    assert result.extracted["alarm_codes"] == ["E-2041"]
    assert result.confidence == 0.92
    assert "E-2041" in result.raw_text


def test_multiple_images_merge_extracted_fields() -> None:
    async def fake(image_id: str) -> dict[str, Any]:
        if image_id == "img_a":
            return {"image_type": "alarm_screen", "extracted": {"alarm_codes": ["E-2041"]}, "raw_text": "A", "confidence": 0.9}
        return {"image_type": "nameplate", "extracted": {"device_model": "Etcher-A"}, "raw_text": "B", "confidence": 0.7}

    state = {**make_initial_state("看图"), "image_ids": ["img_a", "img_b"]}
    with patched_vision_extract(fake):
        out = run_ingest(state)

    merged = out["image_result"]
    assert merged.image_id == "img_a"  # 取置信度最高者为主
    assert merged.extracted["alarm_codes"] == ["E-2041"]
    assert merged.extracted["device_model"] == "Etcher-A"


# --------------------------------------------------------------------------- #
# 4. 失败必须让用户看到（不允许猜测）
# --------------------------------------------------------------------------- #


def test_unrecognized_image_is_surfaced_to_the_user() -> None:
    """识别失败时 uncertain 必须带「图片未能识别，请用文字描述现象」。"""

    state = {**make_initial_state("这个报警怎么处理？", image_ids=["img_1"])}
    assert build_image_notice(state) == IMAGE_UNRECOGNIZED_NOTICE

    out = asyncio.run(respond({**state, "status": "NOT_COVERED", "answer": "知识库未覆盖该问题"}))
    assert IMAGE_UNRECOGNIZED_NOTICE in (out.get("uncertain") or [])
    # 也必须进入响应体（落库与历史回看同样要能看到）
    assert IMAGE_UNRECOGNIZED_NOTICE in ((out.get("response") or {}).get("uncertain") or [])


def test_recognized_image_does_not_warn() -> None:
    state = {
        **make_initial_state("这个报警怎么处理？", image_ids=["img_1"]),
        "image_result": ImageExtraction(image_id="img_1", image_type="alarm_screen", confidence=0.9),
    }
    assert build_image_notice(state) is None


def test_no_image_no_notice() -> None:
    assert build_image_notice(make_initial_state("纯文本问题")) is None


# --------------------------------------------------------------------------- #
# 5. image 事件顺序（走 chat_service 的真实映射）
# --------------------------------------------------------------------------- #


def test_image_event_precedes_token_end_to_end() -> None:
    from backend.services import chat_service

    async def fake_astream(question: str, **kwargs: Any):  # noqa: ANN201
        yield {"image_result": ImageExtraction(image_id="img_1", image_type="alarm_screen", confidence=0.9), "answer": ""}
        yield {"answer": "处理步骤 [1]", "status": "OK", "confidence": 0.8, "confidence_label": "medium"}

    async def fake_session(session_id: str | None, *, question: str):  # noqa: ANN201
        return chat_service.SessionRef("s1", "t1", "")

    async def fake_persist(*args: Any, **kwargs: Any) -> int:
        return 1

    async def fake_history(session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    saved = {name: getattr(chat_service, name) for name in ("astream", "_ensure_session", "_persist_qa", "_load_history")}
    chat_service.astream = fake_astream
    chat_service._ensure_session = fake_session
    chat_service._persist_qa = fake_persist
    chat_service._load_history = fake_history
    try:
        async def run() -> list[str]:
            names: list[str] = []
            async for chunk in chat_service.stream_answer(
                ChatRequest(question="这个报警怎么处理？", image_ids=["img_1"]),
                user_id="t",
                user_role="engineer",
                trace_id="tr",
            ):
                if chunk.startswith("event: "):
                    names.append(chunk.splitlines()[0][7:].strip())
            return names

        names = asyncio.run(run())
    finally:
        for name, value in saved.items():
            setattr(chat_service, name, value)

    assert names == ["meta", "image", "token", "citations", "done"], names


if __name__ == "__main__":  # pragma: no cover
    import traceback

    cases = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    failed = 0
    for name, case in cases:
        try:
            case()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    raise SystemExit(1 if failed else 0)
