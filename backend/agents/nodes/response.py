"""响应组装节点：respond（开发文档 4.3，平台侧甲实现）。

产出字段
--------
    respond -> response（最终响应体，可直接落库 / 回放）

职责边界
--------
verify 已经完成引用校验、置信度与拒答判定，respond **不再修改答案与引用**，
只做两件事：

    1. 追加**安全前置提示**（开发文档 4.2 输出要求：安全前置提示）
    2. 把散落在状态里的字段组装成响应体

安全提示为什么走 uncertain
--------------------------
SSE 契约（接口文档 §6）已冻结为 meta / image / token / citations / done / error，
done 事件里唯一能承载额外文案的字段是 uncertain[]，而前端 MessageList 会渲染它。
因此在 respond 里追加到 uncertain 即可把安全提示送到用户面前，
**不需要改动已冻结的 SSE 契约**（协作规范要求契约变更需双方 approve）。
"""

from __future__ import annotations

import time
from typing import Any

from ...setting import get_settings
from ..state import AgentState
from . import NodeName, register_node

#: 命中这些词说明回答涉及设备操作，需要安全前置提示
_OPERATION_TERMS: tuple[str, ...] = (
    "拆卸",
    "拆装",
    "更换",
    "安装",
    "拧紧",
    "扭矩",
    "上锁",
    "挂牌",
    "泄压",
    "排气",
    "放气",
    "上电",
    "断电",
    "高压",
    "射频",
    "等离子",
    "真空",
    "气体",
    "化学品",
    "高温",
    "清洗",
    "检修",
    "维护",
)

_SAFETY_NOTICE = (
    "安全提示：本回答涉及设备操作，执行前必须按 LOTO（上锁挂牌）完成能源隔离，"
    "确认腔体已泄压至大气压、射频电源与工艺气体已切断，"
    "并由具备资质的人员按现场 SOP 作业。"
)


def _elapsed(started: float, name: str) -> dict[str, float]:
    return {name: round((time.perf_counter() - started) * 1000, 2)}


#: FR-02 失败处理（开发文档 §3.2）：识别不出来时必须明确提示改用文字，
#: 且**不允许猜测**。这是给用户「能照做」的下一步，不是技术报错。
IMAGE_UNRECOGNIZED_NOTICE = "图片未能识别，请用文字描述现象（例如报警码、参数名或设备型号）。"


def build_image_notice(state: AgentState) -> str | None:
    """图片识别失败时必须给出可见提示（FR-02）。

    判定口径与 ingest_image 节点保持一致：没有识别结果、image_type=unknown、
    或置信度低于阈值，都算「未能识别」。

    为什么放在 respond：ingest_image 只能把原因写进 errors，而 errors 不下发到前端；
    真正能到用户眼前的通道是 SSE done 事件的 uncertain[]。
    """

    if not (state.get("image_ids") or []):
        return None

    result = state.get("image_result")
    if result is None:
        return IMAGE_UNRECOGNIZED_NOTICE

    image_type = str(getattr(result, "image_type", "unknown") or "unknown")
    try:
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if image_type == "unknown" or confidence < get_settings().vision_min_confidence:
        return IMAGE_UNRECOGNIZED_NOTICE
    return None


def build_safety_notice(state: AgentState) -> str | None:
    """按回答内容判定是否需要安全前置提示；拒答时返回 None。"""

    if str(state.get("status") or "") != "OK":
        # 拒答本来就不给操作步骤，无需安全提示
        return None
    answer = str(state.get("answer") or "")
    if not answer:
        return None
    if not any(term in answer for term in _OPERATION_TERMS):
        return None
    return _SAFETY_NOTICE


async def respond(state: AgentState) -> dict[str, Any]:
    """组装最终响应体，并追加安全前置提示。"""

    started = time.perf_counter()
    citations = list(state.get("citations") or [])
    uncertain = list(state.get("uncertain") or [])
    errors = list(state.get("errors") or [])

    # 提示类文案统一走 uncertain：这是 SSE 契约里唯一能承载额外文案、
    # 且前端 MessageList 会渲染的字段（不需要改已冻结的事件结构）。
    # 顺序：先「图片未识别」（针对输入，用户需要立刻照做），后「安全提示」（针对输出）。
    notices = [
        item
        for item in (build_image_notice(state), build_safety_notice(state))
        if item and item not in uncertain
    ]
    if notices:
        uncertain = [*notices, *uncertain]

    response: dict[str, Any] = {
        "trace_id": state.get("trace_id") or "",
        "session_id": state.get("session_id") or "",
        "thread_id": state.get("thread_id") or "",
        "question": state.get("question") or "",
        "answer": state.get("answer") or "",
        "citations": [item.model_dump() for item in citations],
        "confidence": float(state.get("confidence") or 0.0),
        "confidence_label": state.get("confidence_label") or "low",
        "status": state.get("status") or "NOT_COVERED",
        "uncertain": uncertain,
        "answer_mode": state.get("answer_mode") or "qa",
        "image_ids": list(state.get("image_ids") or []),
        # 链路降级标记：出现过降级（LLM 未配置、精排降级、图片未识别等）即为 True
        "degraded": bool(errors),
        "errors": errors,
        "timings": dict(state.get("timings") or {}),
    }

    out: dict[str, Any] = {"response": response, "timings": _elapsed(started, "respond")}
    if notices:
        out["uncertain"] = uncertain
    return out


register_node(NodeName.RESPOND, respond)

__all__ = ["IMAGE_UNRECOGNIZED_NOTICE", "build_image_notice", "build_safety_notice", "respond"]
