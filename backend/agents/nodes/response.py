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

    notice = build_safety_notice(state)
    if notice and notice not in uncertain:
        # 前置：安全提示放在 uncertain 最前面，前端按顺序渲染
        uncertain = [notice, *uncertain]

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
    if notice:
        out["uncertain"] = uncertain
    return out


register_node(NodeName.RESPOND, respond)

__all__ = ["build_safety_notice", "respond"]
