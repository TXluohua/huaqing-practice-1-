"""问答路由（开发文档 6.2：SSE 问答 + 会话 + 图片上传 + 反馈 + 健康检查）。

本模块只做 HTTP 翻译：取参 -> 调 service -> 返回。业务逻辑一律在 services/ 层。
每个接口的完整契约见 接口文档.md，此处 docstring 只补充实现侧约定。

service 契约（backend/services/chat_service.py 需实现，全部 async）：

    stream_answer(payload, *, user_id, user_role, trace_id) -> AsyncIterator[str]
        产出**已格式化好的 SSE 文本块**，事件顺序 meta -> image? -> token* -> citations -> done，
        失败时产出 error 事件（流已开始则不再改 HTTP 状态码）。

    list_sessions(*, limit, offset, keyword) -> tuple[list[SessionItem], int]

    list_messages(session_id, *, limit, offset, with_citations)
        -> tuple[SessionRef, list[MessageItem], int] | None      # None 表示会话不存在

    add_feedback(*, qa_id, type, comment, corrected_answer)
        -> FeedbackRecord | None                                  # None 表示 qa_id 不存在
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import StreamingResponse

from ..agents.agent_factory import get_agent, graph_summary
from ..schemas import (
    ChatRequest,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    ImageUploadResponse,
    MessageListResponse,
    SessionListResponse,
)
from ..setting import get_settings
from . import ApiError, get_chat_service, get_kb_service, get_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["问答"])


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(verbose: bool = Query(default=False, description="返回组件耗时明细")) -> HealthResponse:
    """向量库 / 模型 / VLM / MCP / 图 的健康状态。

    说明：健康检查是**基础设施探测**，不承载业务逻辑，因此直接读取 agent 单例
    与配置，不经 service 层（本层唯一允许的例外）。尚未实现的组件标记为未接入。
    """

    settings = get_settings()
    started = time.perf_counter()
    components: dict[str, Any] = {}

    # ---- LangGraph 图 ----
    # 图是懒加载的，直接读 graph_summary() 会得到 ready=False 并被误判为 down。
    # 因此这里主动 await get_agent()：健康检查同时验证「图能否成功构建」。
    try:
        await get_agent()
        summary = graph_summary()
        components["graph"] = {
            "ok": bool(summary["ready"]),
            "detail": summary["name"],
            "nodes": summary["nodes"],
            "checkpointer": summary["checkpointer"],
            "persistent": summary["persistent"],
        }
    except Exception as exc:  # noqa: BLE001 - 健康检查本身不得抛出
        logger.exception("图初始化失败")
        components["graph"] = {
            "ok": False,
            "detail": f"图初始化失败：{exc}",
            "nodes": [],
            "checkpointer": None,
            "persistent": False,
        }

    # ---- 文本模型 / 图片模型（配置检查，无密钥即视为未就绪）----
    components["llm"] = {
        "ok": bool(settings.deepseek_api_key),
        "detail": settings.deepseek_model if settings.deepseek_api_key else "DEEPSEEK_API_KEY 未配置",
    }
    components["vlm"] = {
        "ok": bool(settings.dashscope_api_key),
        "detail": settings.vlm_model if settings.dashscope_api_key else "DASHSCOPE_API_KEY 未配置",
    }

    # ---- 依赖 service 层的组件：未实现时明确标记，不伪装成健康 ----
    for name, module, purpose in (
        ("vector_store", "kb_service", "向量库"),
        ("mcp", "kb_service", "MCP 工单检索"),
    ):
        try:
            from . import _load_service

            _load_service(module, purpose)
            components[name] = {"ok": False, "detail": "service 已实现但未提供健康探测"}
        except ApiError:
            components[name] = {"ok": False, "detail": f"尚未接入（{module} 未实现）"}

    all_ok = all(bool(item.get("ok")) for item in components.values())
    status = "down" if not components["graph"]["ok"] else ("ok" if all_ok else "degraded")

    if verbose:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        for item in components.values():
            item.setdefault("latency_ms", elapsed_ms)

    return HealthResponse(status=status, version=settings.app_version, components=components, trace_id=get_trace_id())


# --------------------------------------------------------------------------- #
# 图片上传
# --------------------------------------------------------------------------- #


@router.post("/upload/image", response_model=ImageUploadResponse, summary="上传图片")
async def upload_image(
    file: UploadFile = File(..., description="图片文件，jpg / png / webp"),
    session_id: str | None = Form(default=None, description="关联会话，便于历史回看"),
    service: Any = Depends(get_kb_service),
    trace_id: str = Depends(get_trace_id),
) -> ImageUploadResponse:
    """上传图片并返回 image_id。**本接口只做预处理，不做识别**。

    识别在问答链路的 ingest_image 节点执行（开发文档 §5.4：SSE 端点不处理文件上传）。
    """

    result = await service.save_image_upload(file, session_id=session_id, trace_id=trace_id)
    return ImageUploadResponse.model_validate(result)


# --------------------------------------------------------------------------- #
# 问答（SSE）
# --------------------------------------------------------------------------- #


@router.post("/chat/stream", summary="提问（SSE 流式）")
async def chat_stream(
    payload: ChatRequest,
    user_id: str = Header(default="anonymous", alias="X-User-Id"),
    user_role: str = Header(default="engineer", alias="X-User-Role"),
    service: Any = Depends(get_chat_service),
    trace_id: str = Depends(get_trace_id),
) -> StreamingResponse:
    """提问并流式返回答案，事件契约见接口文档 §6。

    X-User-Id / X-User-Role 仅做**透传**：权限过滤发生在检索层（检索即鉴权，
    开发文档 §1.4），本层不得裁剪答案内容。
    """

    stream = service.stream_answer(payload, user_id=user_id, user_role=user_role, trace_id=trace_id)
    # 兼容两种 service 写法：async generator 直接返回，或 coroutine 返回迭代器
    if inspect.isawaitable(stream):
        stream = await stream

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关闭 nginx 缓冲，否则 SSE 会被攒包，首 Token 退化（NFR-01 <= 2s）
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 会话与历史
# --------------------------------------------------------------------------- #


@router.get("/chat/sessions", response_model=SessionListResponse, summary="会话列表")
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, description="按标题模糊搜索"),
    service: Any = Depends(get_chat_service),
    trace_id: str = Depends(get_trace_id),
) -> SessionListResponse:
    """会话列表（FR-06）。"""

    items, total = await service.list_sessions(limit=limit, offset=offset, keyword=keyword)
    return SessionListResponse(items=items, total=total, limit=limit, offset=offset, trace_id=trace_id)


@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    summary="历史消息",
)
async def list_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    with_citations: bool = Query(default=True),
    service: Any = Depends(get_chat_service),
    trace_id: str = Depends(get_trace_id),
) -> MessageListResponse:
    """历史消息回看，含引用（FR-04 / FR-06）。

    分页作用于**问答记录**（一轮 user + assistant 视为一条记录），
    因此 total 为记录数的两倍。
    """

    result = await service.list_messages(
        session_id, limit=limit, offset=offset, with_citations=with_citations
    )
    if result is None:
        raise ApiError(404, "SESSION_NOT_FOUND", f"会话不存在：{session_id}")

    ref, items, total = result
    return MessageListResponse(
        session_id=session_id,
        thread_id=getattr(ref, "thread_id", ""),
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------- #
# 反馈
# --------------------------------------------------------------------------- #


@router.post("/feedback", response_model=FeedbackResponse, summary="提交反馈")
async def submit_feedback(
    payload: FeedbackRequest,
    service: Any = Depends(get_chat_service),
    trace_id: str = Depends(get_trace_id),
) -> FeedbackResponse:
    """四态反馈落库（FR-07）。驳回案例由 service 记入 badcases.md。"""

    if payload.type == "correct" and not (payload.corrected_answer or "").strip():
        raise ApiError(400, "CORRECTED_ANSWER_REQUIRED", "反馈类型为 correct 时必须提供 corrected_answer")

    record = await service.add_feedback(
        qa_id=payload.qa_id,
        type=payload.type,
        comment=payload.comment,
        corrected_answer=payload.corrected_answer,
        trace_id=trace_id,
    )
    if record is None:
        raise ApiError(404, "QA_NOT_FOUND", f"问答记录不存在：{payload.qa_id}")

    return FeedbackResponse(
        feedback_id=record.feedback_id,
        recorded=True,
        created_at=record.created_at,
        trace_id=trace_id,
    )
