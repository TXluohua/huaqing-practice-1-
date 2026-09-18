"""SSE 契约测试（开发文档 §5.4 / 接口文档 §6）。

覆盖：
    1. 路由层：SSE 响应头、流式 content-type、关闭 nginx 缓冲
    2. 事件完整性：meta -> image? -> token* -> citations -> done
    3. **流已开始后出错 -> 只发 error 事件**（不再改 HTTP 状态码）
    4. done 事件带 qa_id（前端反馈闭环依赖它）
    5. 路由层与 service 层的契约一致性（缺方法即 503 SERVICE_NOT_READY）

运行（与仓库其它测试一致，不依赖 pytest 也能跑）：
    .venv/bin/python -m pytest tests/test_api/test_sse.py -q
    .venv/bin/python tests/test_api/test_sse.py
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.agents.state import Citation, ImageExtraction  # noqa: E402
from backend.routers import ApiError, api_router, get_chat_service, register_error_handlers  # noqa: E402
from backend.schemas import ChatRequest, ServiceError  # noqa: E402


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析成 [(event, data), ...]（事件之间以空行分隔）。"""

    events: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current = line[7:].strip()
            events.append((current, {}))
        elif line.startswith("data: ") and current is not None:
            events[-1] = (current, json.loads(line[6:]))
    return events


@contextlib.contextmanager
def patched(**replacements: Any):  # noqa: ANN201
    """临时替换 chat_service 的模块级依赖（不引入 pytest 依赖）。"""

    from backend.services import chat_service

    saved = {name: getattr(chat_service, name) for name in replacements}
    for name, value in replacements.items():
        setattr(chat_service, name, value)
    try:
        yield chat_service
    finally:
        for name, value in saved.items():
            setattr(chat_service, name, value)


def collect_events(payload: ChatRequest, *, trace_id: str = "trace-test") -> list[tuple[str, dict[str, Any]]]:
    """跑一次 stream_answer 并把产出的 SSE 文本块解析成事件列表。"""

    from backend.services import chat_service

    async def run() -> str:
        chunks: list[str] = []
        async for chunk in chat_service.stream_answer(
            payload, user_id="tester", user_role="engineer", trace_id=trace_id
        ):
            chunks.append(chunk)
        return "".join(chunks)

    return parse_sse(asyncio.run(run()))


class FakeChatService:
    """路由层测试用的最小实现（只验证 HTTP 契约）。"""

    async def stream_answer(self, payload: ChatRequest, *, user_id: str, user_role: str, trace_id: str):  # noqa: ANN201
        def frame(event: str, data: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield frame("meta", {"session_id": "s1", "trace_id": trace_id, "ts": "2026-09-17T00:00:00+08:00"})
        yield frame("token", {"delta": "结论 [1]"})
        yield frame("citations", {"items": []})
        yield frame("done", {"status": "OK", "confidence": 0.9, "label": "high", "uncertain": []})


def build_client(service: Any | None = None) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(api_router, prefix="/api")
    if service is not None:
        app.dependency_overrides[get_chat_service] = lambda: service
    return TestClient(app)


def citation() -> Citation:
    return Citation(
        id=1,
        source_type="kb_doc",
        doc="刻蚀设备维护手册",
        version="V3.2",
        section="3.4",
        page=3,
        chunk_id="c_0001",
        snippet="检查腔体密封 O-ring",
    )


def session_ref():  # noqa: ANN201
    from backend.services.chat_service import SessionRef

    return SessionRef("s1", "thread-s1", "真空度排查")


# --------------------------------------------------------------------------- #
# 1. 路由层契约
# --------------------------------------------------------------------------- #


def test_router_returns_event_stream_with_anti_buffering_headers() -> None:
    client = build_client(FakeChatService())
    response = client.post("/api/chat/stream", json={"question": "腔体真空度异常怎么排查？"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 接口文档 §8 缺口⑧：nginx 不关缓冲会把 SSE 攒包，首 Token 退化
    assert response.headers.get("x-accel-buffering") == "no"
    assert response.headers.get("cache-control") == "no-cache"

    names = [event for event, _ in parse_sse(response.text)]
    assert names == ["meta", "token", "citations", "done"], names


def test_router_rejects_blank_question() -> None:
    client = build_client(FakeChatService())
    response = client.post("/api/chat/stream", json={"question": "   "})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_load_service_reports_503_for_unimplemented_service() -> None:
    """service 缺方法时路由层必须给出明确 503，而不是启动失败或 500。"""

    from backend.routers import _load_service

    try:
        _load_service("chat_service", "会话与问答服务", ("no_such_method",))
    except ApiError as exc:
        assert exc.status_code == 503
        assert exc.code == "SERVICE_NOT_READY"
    else:  # pragma: no cover
        raise AssertionError("缺少约定方法时必须抛 ApiError(503)")


def test_implemented_services_satisfy_router_contract() -> None:
    """真实 service 必须实现路由层依赖的全部方法（契约一致性回归）。"""

    from backend.routers import _CHAT_SERVICE_API, _KB_SERVICE_API, _load_service

    assert _load_service("chat_service", "会话与问答服务", _CHAT_SERVICE_API) is not None
    assert _load_service("kb_service", "知识库服务", _KB_SERVICE_API) is not None


# --------------------------------------------------------------------------- #
# 2. 事件映射（service 层）
# --------------------------------------------------------------------------- #


def test_event_sequence_and_done_payload() -> None:
    async def fake_astream(question: str, **kwargs: Any):  # noqa: ANN201
        yield {"answer": ""}
        yield {
            "answer": "先检查腔体密封 O-ring [1]",
            "citations": [citation()],
            "status": "OK",
            "confidence": 0.86,
            "confidence_label": "high",
            "uncertain": [],
        }

    async def fake_session(session_id: str | None, *, question: str):  # noqa: ANN201
        return session_ref()

    async def fake_persist(*args: Any, **kwargs: Any) -> int:
        return 42

    async def fake_history(session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    with patched(
        astream=fake_astream,
        _ensure_session=fake_session,
        _persist_qa=fake_persist,
        _load_history=fake_history,
    ):
        events = collect_events(ChatRequest(question="腔体真空度异常怎么排查？"))

    assert [name for name, _ in events] == ["meta", "token", "citations", "done"]

    meta = events[0][1]
    assert meta["session_id"] == "s1"
    assert meta["trace_id"] == "trace-test"

    assert "O-ring" in events[1][1]["delta"]
    assert events[2][1]["items"][0]["page"] == 3
    assert events[2][1]["items"][0]["doc"] == "刻蚀设备维护手册"

    done = events[3][1]
    assert done["status"] == "OK"
    assert done["label"] == "high"
    # 契约追加字段：前端反馈按钮依赖它
    assert done["qa_id"] == 42


def test_image_event_precedes_token() -> None:
    """图片识别结果必须先于正文（接口文档 §6 的事件顺序）。"""

    async def fake_astream(question: str, **kwargs: Any):  # noqa: ANN201
        yield {
            "image_result": ImageExtraction(
                image_id="img_1",
                image_type="alarm_screen",
                extracted={"alarm_codes": ["E-2041"]},
                raw_text="E-2041",
                confidence=0.92,
            ),
            "answer": "",
        }
        yield {
            "answer": "报警 E-2041 的处理步骤 [1]",
            "citations": [citation()],
            "status": "OK",
            "confidence": 0.8,
            "confidence_label": "medium",
        }

    async def fake_session(session_id: str | None, *, question: str):  # noqa: ANN201
        return session_ref()

    async def fake_persist(*args: Any, **kwargs: Any) -> int:
        return 1

    async def fake_history(session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    with patched(
        astream=fake_astream,
        _ensure_session=fake_session,
        _persist_qa=fake_persist,
        _load_history=fake_history,
    ):
        events = collect_events(ChatRequest(question="这个报警怎么处理？", image_ids=["img_1"]))

    names = [name for name, _ in events]
    assert names == ["meta", "image", "token", "citations", "done"], names
    image = dict(events)["image"]
    assert image["image_type"] == "alarm_screen"
    assert image["extracted"]["alarm_codes"] == ["E-2041"]


# --------------------------------------------------------------------------- #
# 3. 出错路径
# --------------------------------------------------------------------------- #


def test_stream_failure_emits_error_event_only() -> None:
    """流已开始后出错：发 error 事件，**不再改 HTTP 状态码**，也不再补 done。"""

    async def boom(question: str, **kwargs: Any):  # noqa: ANN201
        yield {"answer": ""}
        raise RuntimeError("检索阶段炸了")

    async def fake_session(session_id: str | None, *, question: str):  # noqa: ANN201
        return session_ref()

    async def fake_history(session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    with patched(astream=boom, _ensure_session=fake_session, _load_history=fake_history):
        events = collect_events(ChatRequest(question="随便问问"))

    assert [name for name, _ in events] == ["meta", "error"], [n for n, _ in events]
    assert events[1][1]["code"] == "INTERNAL_ERROR"
    assert "检索阶段炸了" in events[1][1]["message"]


def test_unknown_session_emits_error_event() -> None:
    async def missing_session(session_id: str | None, *, question: str):  # noqa: ANN201
        raise ServiceError(404, "SESSION_NOT_FOUND", f"会话不存在：{session_id}")

    with patched(_ensure_session=missing_session):
        events = collect_events(ChatRequest(question="你好", session_id="not-exist"))

    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["code"] == "SESSION_NOT_FOUND"


def test_error_body_shape_is_documented_form() -> None:
    """统一错误体：{code, message, detail?, trace_id}（接口文档 §5）。"""

    client = build_client(FakeChatService())
    body = client.post("/api/feedback", json={"qa_id": 1, "type": "correct"}).json()
    assert body["code"] == "CORRECTED_ANSWER_REQUIRED"
    assert isinstance(body["message"], str)
    assert body["trace_id"]


if __name__ == "__main__":  # pragma: no cover - 无 pytest 时的兜底入口
    import traceback

    cases = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
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
