"""路由层全部请求 / 响应模型（开发文档 6.2 的 schemas.py）。

设计依据：接口文档.md。按开发文档 6.4「接口先行」，
契约（本文档 + 接口文档）变更必须先于实现变更。

引用与置信度类型**直接复用 agents.state**，
保证「SSE citations 事件 = HTTP 响应 = AgentState.citations」三处同源，避免字段漂移。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agents.state import Citation, ConfidenceLabel, Status
from .setting import get_settings

# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #


class ErrorBody(BaseModel):
    """统一错误体（接口文档 §5）。"""

    code: str
    message: str
    detail: dict[str, Any] | None = None
    trace_id: str | None = None


class ServiceError(Exception):
    """service 层抛出的、带 HTTP 语义的业务错误。

    为什么定义在这里：错误契约与 ErrorBody 是一回事，放在契约模块里可以让
    routers 与 services **都** import 它，而不必让 services 反向 import routers
    （开发文档 6.1 禁止反向依赖）。routers.register_error_handlers 会把它
    渲染成与接口文档 §5 一致的错误体。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


class ComponentHealth(BaseModel):
    """单个依赖组件的健康状态。"""

    ok: bool
    detail: str | None = None
    latency_ms: float | None = None
    model_config = ConfigDict(extra="allow")


class HealthResponse(BaseModel):
    """GET /api/health"""

    status: Literal["ok", "degraded", "down"]
    version: str
    components: dict[str, Any]
    trace_id: str


# --------------------------------------------------------------------------- #
# 图片上传  POST /api/upload/image
# --------------------------------------------------------------------------- #


class ImageUploadResponse(BaseModel):
    image_id: str
    filename: str
    mime: str
    size_bytes: int
    width: int
    height: int
    url: str
    trace_id: str


# --------------------------------------------------------------------------- #
# 问答  POST /api/chat/stream
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    """提问请求。字段与接口文档 §4.3 一一对应。"""

    question: str = Field(min_length=1, description="用户问题")
    session_id: str | None = Field(default=None, description="缺省则新建会话")
    image_ids: list[str] = Field(default_factory=list, description="图片 ID，先经上传接口获得")
    mode: Literal["qa", "training"] = Field(default="qa", description="qa 问答 / training 培训讲解")
    device_model: str | None = Field(default=None, description="设备型号，用于元数据过滤")
    category: str | None = Field(default=None, description="分类过滤：设备维护 / 工艺 / 标准")

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空")
        limit = get_settings().question_max_len
        if len(value) > limit:
            raise ValueError(f"question 超过 {limit} 字上限")
        return value

    @field_validator("image_ids")
    @classmethod
    def _validate_images(cls, value: list[str]) -> list[str]:
        limit = get_settings().image_max_count
        if len(value) > limit:
            raise ValueError(f"image_ids 最多 {limit} 个")
        return value


# ---- SSE 事件载荷（接口文档 §6）----


class MetaEvent(BaseModel):
    session_id: str
    trace_id: str
    ts: str


class ImageEvent(BaseModel):
    image_id: str
    image_type: str
    extracted: dict[str, Any] = Field(default_factory=dict)
    confidence: float


class TokenEvent(BaseModel):
    delta: str


class CitationsEvent(BaseModel):
    items: list[Citation] = Field(default_factory=list)


class DoneEvent(BaseModel):
    status: Status
    confidence: float
    label: ConfidenceLabel
    uncertain: list[str] = Field(default_factory=list)


class ErrorEvent(BaseModel):
    code: str
    message: str


# --------------------------------------------------------------------------- #
# 会话与历史  GET /api/chat/sessions 等
# --------------------------------------------------------------------------- #


class SessionItem(BaseModel):
    session_id: str
    thread_id: str
    title: str
    message_count: int
    last_question: str | None = None
    updated_at: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
    total: int
    limit: int
    offset: int
    trace_id: str


class MessageItem(BaseModel):
    """一条历史消息。user 与 assistant 各一条，见接口文档 §4.5。"""

    qa_id: int
    role: Literal["user", "assistant"]
    content: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float | None = None
    confidence_label: ConfidenceLabel | None = None
    status: Status | None = None
    image_ids: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    created_at: str


class MessageListResponse(BaseModel):
    session_id: str
    thread_id: str
    items: list[MessageItem]
    total: int
    limit: int
    offset: int
    trace_id: str


# --------------------------------------------------------------------------- #
# 反馈  POST /api/feedback
# --------------------------------------------------------------------------- #

#: 四态反馈（FR-07）
FeedbackType = Literal["adopt", "partial", "reject", "correct"]


class FeedbackRequest(BaseModel):
    qa_id: int
    type: FeedbackType
    comment: str | None = Field(default=None, max_length=500)
    corrected_answer: str | None = None


class FeedbackResponse(BaseModel):
    feedback_id: int
    recorded: bool
    created_at: str
    trace_id: str


# --------------------------------------------------------------------------- #
# 管理  /api/admin/*
# --------------------------------------------------------------------------- #


class DocumentUploadResponse(BaseModel):
    doc_id: int
    job_id: str
    status: str
    trace_id: str


class IndexRebuildRequest(BaseModel):
    scope: Literal["all", "incremental"] = "all"
    force: bool = False


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "succeeded", "failed"]
    progress: float = 0.0
    message: str = ""
    result: dict[str, Any] | None = None
    trace_id: str


class ChunkItem(BaseModel):
    chunk_id: str
    doc: str | None = None
    version: str | None = None
    section: str | None = None
    page: int | None = None
    token_len: int | None = None
    text: str = ""
    image_url: str | None = None


class ChunkListResponse(BaseModel):
    items: list[ChunkItem]
    total: int
    limit: int
    offset: int
    trace_id: str
