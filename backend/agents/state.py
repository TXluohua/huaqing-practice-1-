"""LangGraph 状态契约：AgentState / Evidence / Citation。

契约级文件（开发文档 6.3）：字段一旦变更，所有节点需同步修改。
因此本文件只定义「数据形状」，不含任何业务逻辑，也不依赖任何节点实现。

字段分组与开发文档 4.3 的节点一一对应，方便把节点实现直接挂到链路上：
    ingest_image  -> image_result
    rewrite       -> queries
    retrieve      -> candidates
    rerank        -> ranked / top_score
    build_context -> context / context_blocks
    augment       -> evidence_sufficient / augment_rounds / tool_calls
    generate      -> answer / answer_structured
    verify        -> citations / confidence / confidence_label / status / uncertain
    respond       -> response
    横切          -> trace_id / timings / errors
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 枚举取值（与开发文档 5.4 SSE 契约、5.2.3 零幻觉四道关保持一致）
# --------------------------------------------------------------------------- #

#: 引用来源类型：知识库文档 / 历史工单(MCP) / 图片依据
SourceType = Literal["kb_doc", "ticket", "image"]

#: 会话状态：OK 正常返回；NOT_COVERED 知识库未覆盖（拒答）；ERROR 链路异常
Status = Literal["OK", "NOT_COVERED", "ERROR"]

#: 置信度三级标注
ConfidenceLabel = Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- #
# 证据与引用
# --------------------------------------------------------------------------- #


class Citation(BaseModel):
    """回答中 [n] 对应的原始依据。

    字段与 SSE citations 事件的 items 元素一一对应（开发文档 5.4）：

        {id, source_type, doc, version, section, page, chunk_id, image_url}

    注意：编号 id 由代码生成，禁止由模型自行编造（开发文档 4.4.2）。
    """

    id: int
    source_type: SourceType = "kb_doc"
    doc: str | None = None
    version: str | None = None
    section: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    image_url: str | None = None
    #: 引用块原文片段，用于 SourceDrawer 展示与人工复核
    snippet: str | None = None


class Evidence(BaseModel):
    """候选切片：检索 -> 精排 -> 上下文构建 的统一载体。"""

    chunk_id: str
    text: str
    #: 融合检索得分（BM25 + 向量 RRF）
    score: float = 0.0
    #: Cross-Encoder 精排得分；未精排时为 None
    rerank_score: float | None = None
    #: 切片元数据：doc / version / section / page / device_model / category 等
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: 进入上下文后分配的引用编号与出处
    citation: Citation | None = None


class ImageExtraction(BaseModel):
    """ingest_image 节点的输出：只转写、不判断（开发文档 4.3 / 4.4.1）。

    confidence 低于阈值或 image_type == "unknown" 时，
    必须由链路明确提示「图片未能识别，请用文字描述现象」，不允许猜测。
    """

    image_id: str
    image_type: str = "unknown"
    #: 结构化识别结果，如 {"alarm_code": "VAC-2031", "table_md": "..."}
    extracted: dict[str, Any] = Field(default_factory=dict)
    #: 图内全部文字（原文转写）
    raw_text: str = ""
    confidence: float = 0.0


# --------------------------------------------------------------------------- #
# 状态 reducer
# --------------------------------------------------------------------------- #


def merge_timings(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
) -> dict[str, float]:
    """timings 字段的 reducer：各节点写入自己的耗时，合并而非互相覆盖。

    用于 NFR-06 可观测：每次问答记录节点耗时，便于定位性能瓶颈。
    """

    merged: dict[str, float] = dict(left or {})
    merged.update(right or {})
    return merged


# --------------------------------------------------------------------------- #
# 主状态
# --------------------------------------------------------------------------- #


class AgentState(TypedDict, total=False):
    """LangGraph 单图（A1 设备维护问答智能体）的共享状态。

    total=False：节点只需返回自己产出的字段（增量更新），
    未返回的字段由 LangGraph 沿用上一轮的值。

    被 Annotated 标注的两个字段使用 reducer 做累积合并，其余字段为覆盖语义。
    """

    # ---- 标识（贯穿日志与 SSE meta 事件）----
    trace_id: str
    session_id: str
    thread_id: str

    # ---- 输入 ----
    question: str
    image_ids: list[str]
    #: 多轮上下文，元素形如 {"role": "user" 或 "assistant", "content": str}
    history: list[dict[str, Any]]

    # ---- 输入补充（契约追加，2026-09：接入检索层过滤与培训模式所需）----
    #: 设备型号，用于检索层元数据过滤（开发文档 4.3 retrieve）
    device_model: str | None
    #: 分类过滤：设备维护 / 工艺 / 标准
    category: str | None
    #: 回答模式：qa 问答 / training 培训分层讲解（FR-09）
    answer_mode: str
    #: 提问者角色，检索即鉴权（开发文档 1.4）；由 HTTP 层 X-User-Role 透传
    user_role: str

    # ---- ingest_image（可选节点）----
    image_result: ImageExtraction | None

    # ---- rewrite ----
    #: 2~3 路检索式（术语归一后的多路 query）
    queries: list[str]

    # ---- retrieve ----
    #: 混合检索候选（BM25 + 向量 + RRF 融合后，Top 30~50）
    candidates: list[Evidence]

    # ---- rerank ----
    #: Cross-Encoder 精排后的 Top 5~8
    ranked: list[Evidence]
    #: 精排最高分，用于拒答阈值判定（开发文档 5.2.3 第 4 关）
    top_score: float

    # ---- build_context ----
    #: 带 [n] 编号的上下文文本（token 预算内）
    context: str
    #: 上下文中的证据块，顺序即 [n] 编号顺序
    context_blocks: list[Evidence]

    # ---- augment（受限 agent，硬边界见 4.3）----
    evidence_sufficient: bool
    #: 已用轮数（<=2）
    augment_rounds: int
    #: 已用工具调用次数（<=3）
    tool_calls: int

    # ---- generate ----
    answer: str
    #: with_structured_output 的原始结构化结果
    answer_structured: dict[str, Any] | None

    # ---- verify（确定性代码，禁止绕过）----
    citations: list[Citation]
    confidence: float
    confidence_label: ConfidenceLabel
    status: Status
    #: 未能溯源/存疑的点，随 SSE done 事件返回
    uncertain: list[str]

    # ---- respond ----
    #: 最终响应体（引用清单 + 置信度 + 未覆盖说明 + 安全前置提示）
    response: dict[str, Any] | None

    # ---- 横切 ----
    #: 节点名 -> 耗时(ms)，由各节点自行写入，reducer 合并
    timings: Annotated[dict[str, float], merge_timings]
    #: 链路异常/降级记录（MCP 加载失败、检索超时等），reducer 追加
    errors: Annotated[list[str], operator.add]


# --------------------------------------------------------------------------- #
# 构造函数
# --------------------------------------------------------------------------- #


def new_trace_id() -> str:
    """生成一次问答的 trace_id（NFR-06 可观测）。"""

    return uuid.uuid4().hex


def make_initial_state(
    question: str,
    *,
    session_id: str | None = None,
    thread_id: str | None = None,
    image_ids: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    trace_id: str | None = None,
    device_model: str | None = None,
    category: str | None = None,
    answer_mode: str = "qa",
    user_role: str = "engineer",
) -> AgentState:
    """构造一次问答的初始状态（图的输入）。

    初始 status 取 NOT_COVERED（fail-closed）：
    在 verify 节点明确判定之前，任何答案都不得被视为可信。
    这符合开发文档 1.4 的「零幻觉优先」原则 —— 忘记赋值时默认拒答，而不是默认放行。

    device_model / category / answer_mode / user_role 为 2026-09 追加的输入字段
    （接口文档 §8 缺口①②），均有默认值，不影响既有调用方。
    """

    return AgentState(
        trace_id=trace_id or new_trace_id(),
        session_id=session_id or "",
        thread_id=thread_id or "",
        question=question,
        image_ids=list(image_ids or []),
        history=list(history or []),
        device_model=device_model,
        category=category,
        answer_mode=answer_mode,
        user_role=user_role,
        image_result=None,
        queries=[],
        candidates=[],
        ranked=[],
        top_score=0.0,
        context="",
        context_blocks=[],
        evidence_sufficient=False,
        augment_rounds=0,
        tool_calls=0,
        answer="",
        answer_structured=None,
        citations=[],
        confidence=0.0,
        confidence_label="low",
        status="NOT_COVERED",
        uncertain=[],
        response=None,
        timings={},
        errors=[],
    )
