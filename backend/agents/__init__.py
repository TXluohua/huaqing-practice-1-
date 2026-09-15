"""A1 设备维护问答智能体（LangGraph 单图）。

模块职责（开发文档 6.2 / 6.3）
    state.py          状态与证据结构（契约级，变更需同步所有节点）
    graph.py          主图拓扑（契约级，影响全局需评审）
    agent_factory.py  图实例的异步单例 + 调用入口
    nodes/            节点实现与注册表（当前为空 —— 骨架阶段）
    prompts/          提示词（system / generate / rewrite / vision）

当前进度：链路已打通，节点与边待接入。
    build_graph() 在没有任何注册节点时直接连 START -> END，
    因此 compile / invoke / stream / 检查点 / 会话记忆已经可用且可测。

快速验证：
    python3 scripts/ask.py "刻蚀机真空度异常怎么排查？"
"""

from .state import (
    AgentState,
    Citation,
    ConfidenceLabel,
    Evidence,
    ImageExtraction,
    SourceType,
    Status,
    make_initial_state,
    new_trace_id,
)

__all__ = [
    "AgentState",
    "Citation",
    "ConfidenceLabel",
    "Evidence",
    "ImageExtraction",
    "SourceType",
    "Status",
    "make_initial_state",
    "new_trace_id",
]
