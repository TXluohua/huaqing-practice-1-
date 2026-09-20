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
    #: 本轮问答记录 ID（2026-09 追加，接口文档 §8 缺口③）：
    #: 前端拿它才能对「刚拿到的答案」直接提交反馈，否则反馈按钮只能置灰、
    #: 要等刷新历史页才拿得到 qa_id。chat_service 一直在 payload 里给这个字段，
    #: 但模型未声明 —— 这里补齐，保证 OpenAPI 与前端类型能看到它。
    qa_id: int | None = None


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


# --------------------------------------------------------------------------- #
# 高频问题（FR-09）
# --------------------------------------------------------------------------- #


class FrequentQuestionItem(BaseModel):
    """一条高频问题统计。"""

    question: str
    question_key: str
    ask_count: int
    first_asked_at: str
    last_asked_at: str


class FrequentQuestionListResponse(BaseModel):
    items: list[FrequentQuestionItem]
    total: int
    limit: int
    trace_id: str

# --------------------------------------------------------------------------- #
# 新增业务契约（2026-09-20 追加，只追加不改既有模型）
#
# 三块新业务：① 维护计划生成  ② 备件商城与采购  ③ 考核认证
# 字段与 backend/db.py 的对应表、backend/services/*_service.py 的返回结构一致。
# --------------------------------------------------------------------------- #

# ---- ① 维护计划生成 POST /api/plans/generate ----


class PlanGenerateRequest(BaseModel):
    """按设备生成维护计划。"""

    device_model: str = Field(min_length=1, description="设备型号，如 Etcher-A")
    device_code: str = Field(default="", description="设备编号/位号")
    #: 设备当前计量值与上次维护基线（小时或片数），用于推算剩余量
    runtime_hours: float = Field(default=0.0, ge=0, description="累计运行小时")
    wafer_count: int = Field(default=0, ge=0, description="累计生产片数")
    hours_since_pm: float | None = Field(default=None, description="距上次 PM 的运行小时；缺省用 runtime_hours")
    wafers_since_pm: int | None = Field(default=None, description="距上次 PM 的片数；缺省用 wafer_count")
    top_k: int = Field(default=8, ge=1, le=20, description="最多生成多少条计划项")
    persist: bool = Field(default=True, description="是否落库（false 时只预览）")


class PlanItem(BaseModel):
    """一条维护计划项（依据必须可溯源）。"""

    id: int | None = None
    device_model: str
    device_code: str = ""
    item_name: str
    item_type: str = "PM 项"
    cycle_basis: str = "hours"
    cycle_value: float = 0.0
    baseline_value: float = 0.0
    current_value: float = 0.0
    #: 距离到期还差多少计量单位（负数=已超期）
    remaining: float = 0.0
    due_at: str | None = None
    status: str = "planned"
    #: 计划依据（引用手册章节），字段与 state.Citation 一致
    evidence: list[Citation] = Field(default_factory=list)
    chunk_id: str = ""
    note: str = ""
    #: 时间戳（UTC ISO 字符串）：created_at/updated_at 是行时间，completed_at 仅在
    #: 完成（done）或跳过（skipped）后才有值 —— 前端可直接展示「上次完成时间」
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class PlanGenerateResponse(BaseModel):
    device_model: str
    device_code: str = ""
    items: list[PlanItem] = Field(default_factory=list)
    #: 知识库里查不到 PM 周期的设备/项目，如实列出（不编造周期）
    uncovered: list[str] = Field(default_factory=list)
    trace_id: str


class PlanListResponse(BaseModel):
    items: list[PlanItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    trace_id: str


class PlanUpdateRequest(BaseModel):
    note: str | None = None
    #: 完成时回填的计量值（用于滚动到下一个周期）
    completed_value: float | None = None


# ---- ② 备件商城与采购 ----


class PartItem(BaseModel):
    code: str
    part: str
    device_model: str = ""
    spec: str = ""
    stock: int = 0
    unit: str = "个"
    location: str = ""
    lead_time_days: int = 0
    #: 商城价格（示例数据；接真实 ERP/采购系统时替换数据源即可）
    price_cny: float = 0.0
    supplier: str = ""
    stock_status: str = ""
    substitutes: list[dict[str, Any]] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class PartCatalogResponse(BaseModel):
    items: list[PartItem] = Field(default_factory=list)
    total: int = 0
    trace_id: str


class OrderItemRequest(BaseModel):
    code: str = Field(min_length=1, description="备件编码")
    qty: int = Field(default=1, ge=1, le=999)
    #: 是否作为替代件采购（true 时必须有依据，否则下单会被拒）
    is_substitute: bool = False
    #: 操作人（可选，只写进订单 note 的审计行，**不落进明细**）
    operator: str = ""


class OrderItemUpdateRequest(BaseModel):
    """购物车改数量（`PATCH /parts/orders/{order_id}/items/{code}`）。

    只允许改数量（1~999）：编码与单价由后端回台账取，**不接受前端传价格**。
    """

    qty: int = Field(ge=1, le=999, description="新的数量（1~999）")
    #: 操作人（可选，只写进订单 note 的审计行）
    operator: str = ""


class OrderCreateRequest(BaseModel):
    #: 初始明细；**允许为空** —— 空明细的草稿单就是「空购物车」（先建车再逐件加入），
    #: 但空车不能提交（提交时 400 EMPTY_ORDER）
    items: list[OrderItemRequest] = Field(default_factory=list)
    device_model: str = ""
    purpose: str = Field(default="", description="用途/关联工单")
    applicant: str = ""
    note: str = ""


class PartOrderItem(BaseModel):
    code: str
    part: str = ""
    qty: int = 1
    unit: str = "个"
    unit_price: float = 0.0
    amount: float = 0.0
    is_substitute: bool = False
    #: 替代件依据（无依据的替代件不允许下单）
    basis: str = ""
    stock: int = 0
    lead_time_days: int = 0


class PartOrderResponse(BaseModel):
    id: int
    order_no: str
    status: str
    device_model: str = ""
    purpose: str = ""
    applicant: str = ""
    items: list[PartOrderItem] = Field(default_factory=list)
    total_amount: float = 0.0
    currency: str = "CNY"
    note: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    #: 该状态允许的下一步动作，前端据此渲染按钮
    allowed_actions: list[str] = Field(default_factory=list)
    trace_id: str = ""


class OrderListResponse(BaseModel):
    items: list[PartOrderResponse] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    trace_id: str


class OrderActionRequest(BaseModel):
    operator: str = ""
    note: str = ""
    #: 驳回/取消时必填原因
    reason: str = ""


class SettlementRequest(BaseModel):
    amount: float | None = Field(default=None, ge=0, description="缺省用订单金额")
    method: str = "月结"
    invoice_no: str = ""
    operator: str = ""
    note: str = ""


class SettlementResponse(BaseModel):
    id: int
    order_id: int
    order_no: str
    amount: float
    currency: str = "CNY"
    method: str = "月结"
    invoice_no: str = ""
    operator: str = ""
    note: str = ""
    settled_at: str | None = None
    trace_id: str = ""


class SettlementListResponse(BaseModel):
    items: list[SettlementResponse] = Field(default_factory=list)
    total: int = 0
    #: 汇总金额，便于对账
    total_amount: float = 0.0
    trace_id: str


# ---- ③ 考核认证 ----


class QuizGenerateRequest(BaseModel):
    device_model: str = Field(default="", description="按设备型号出题；留空则全库")
    topic: str = Field(default="", description="主题/章节关键词，如「真空系统」")
    level: str = Field(default="basic", description="basic / advanced")
    n_items: int = Field(default=5, ge=1, le=10, description="题目数量")
    question_types: list[str] = Field(
        default_factory=lambda: ["single", "judgement"],
        description="题型：single 单选 / multiple 多选 / judgement 判断 / short 简答",
    )


class QuizItem(BaseModel):
    no: int
    question: str
    type: str = "single"
    options: list[str] = Field(default_factory=list)
    #: 单选题为选项下标；多选为下标列表；判断题为 true/false；简答为要点列表
    answer: Any = None
    explanation: str = ""
    #: 该题依据的知识库切片（必须非空，题目同样要可溯源）
    evidence: list[Citation] = Field(default_factory=list)


class QuizResponse(BaseModel):
    id: int
    device_model: str = ""
    topic: str = ""
    level: str = "basic"
    n_items: int = 0
    items: list[QuizItem] = Field(default_factory=list)
    generator: str = ""
    created_at: str | None = None
    trace_id: str = ""


class QuizSubmitRequest(BaseModel):
    trainee: str = Field(min_length=1)
    #: 与题目 no 对应：单选传下标、多选传下标列表、判断传 true/false、简答传文本
    answers: list[Any] = Field(default_factory=list)
    duration_s: float = Field(default=0.0, ge=0)


class QuizResultItem(BaseModel):
    no: int
    question: str = ""
    correct: bool = False
    expected: Any = None
    got: Any = None
    explanation: str = ""
    evidence: list[Citation] = Field(default_factory=list)


class QuizSubmitResponse(BaseModel):
    attempt_id: int
    quiz_id: int
    trainee: str
    score: float
    passed: bool
    pass_line: float
    detail: list[QuizResultItem] = Field(default_factory=list)
    #: 是否已据此发证（通过且达到发证线时）
    certification_id: int | None = None
    trace_id: str = ""


class CertificationRequest(BaseModel):
    trainee: str = Field(min_length=1)
    device_model: str = Field(min_length=1)
    level: str = Field(default="L1", description="L1 / L2 / L3")
    attempt_id: int = Field(description="必须基于一次通过的考核")
    issuer: str = "内部授权"
    valid_days: int = Field(default=365, ge=1, le=3650)
    note: str = ""


class CertificationResponse(BaseModel):
    id: int
    trainee: str
    device_model: str
    level: str = "L1"
    attempt_id: int = 0
    quiz_id: int = 0
    score: float = 0.0
    issuer: str = ""
    issued_at: str | None = None
    expires_at: str | None = None
    status: str = "valid"
    #: 距到期天数（负数=已过期）
    days_to_expiry: int | None = None
    note: str = ""
    trace_id: str = ""


class CertificationListResponse(BaseModel):
    items: list[CertificationResponse] = Field(default_factory=list)
    total: int = 0
    trace_id: str
