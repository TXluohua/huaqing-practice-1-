"""节点注册表：LangGraph 图的节点实现挂载点。

骨架阶段（当前状态）
--------------------
注册表为空。graph.build_graph() 检测到没有注册任何节点时，
直接把 START 连到 END，状态原样透传。
因此 compile / invoke / ainvoke / stream / 检查点 / 会话记忆
这整条链路现在就能跑通并被测试覆盖，而不需要任何节点与业务边。

接入一个节点
------------
1) 在 nodes/retrieval.py（或 generation.py / multimodal.py）里写实现，
   签名与 LangGraph 节点一致：接收状态，返回「增量字段」字典：

       async def rewrite(state: AgentState) -> dict:
           return {"queries": [...]}

2) 调用 register_node 注册：

       from ..nodes import NodeName, register_node
       register_node(NodeName.REWRITE, rewrite)

3) 顺序由 NODE_SEQUENCE（开发文档 4.3）决定，
   build_graph() 会自动把已注册的节点按该顺序线性串起来。

关于条件分支
------------
开发文档 4.3 的真实拓扑里有一处分支（「证据是否足够?」-> 是否走 augment），
线性串联无法表达。届时请在 graph.build_graph() 中用
add_conditional_edges 替换线性连线，本文件无需改动。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..state import AgentState

logger = logging.getLogger(__name__)


class NodeName:
    """开发文档 4.3 的 9 个节点名（契约常量，勿改名）。"""

    #: 图片结构化识别（可选，与检索并行）
    INGEST_IMAGE = "ingest_image"
    #: 问题改写为 2~3 路检索式
    REWRITE = "rewrite"
    #: 混合检索（BM25 + 向量 + RRF）
    RETRIEVE = "retrieve"
    #: Cross-Encoder 精排
    RERANK = "rerank"
    #: 上下文构建（去重、版本择优、[n] 编号、token 裁剪）
    BUILD_CONTEXT = "build_context"
    #: 证据不足时的补检索（受限 agent：<=2 轮 / <=3 次工具调用 / 15s 超时）
    AUGMENT = "augment"
    #: 生成答案（with_structured_output，temperature=0）
    GENERATE = "generate"
    #: 引用校验 + 置信度 + 拒答判定（确定性代码，禁止绕过）
    VERIFY = "verify"
    #: 组装最终响应
    RESPOND = "respond"


#: 节点在链路上的声明顺序（开发文档 4.3 图结构的主干）
NODE_SEQUENCE: tuple[str, ...] = (
    NodeName.INGEST_IMAGE,
    NodeName.REWRITE,
    NodeName.RETRIEVE,
    NodeName.RERANK,
    NodeName.BUILD_CONTEXT,
    NodeName.AUGMENT,
    NodeName.GENERATE,
    NodeName.VERIFY,
    NodeName.RESPOND,
)

#: 已注册的节点实现：名称 -> 可调用对象。骨架阶段为空。
NODE_REGISTRY: dict[str, Callable[[AgentState], Any]] = {}


def register_node(name: str, fn: Callable[[AgentState], Any]) -> None:
    """注册（或覆盖）一个节点实现。"""

    if not isinstance(name, str) or not name:
        raise ValueError("节点名必须是非空字符串")
    if not callable(fn):
        raise TypeError(f"节点 {name!r} 的实现必须是可调用对象")
    if name in NODE_REGISTRY:
        logger.debug("节点 %s 被重复注册，已覆盖", name)
    NODE_REGISTRY[name] = fn


def unregister_node(name: str) -> None:
    """移除一个节点实现（不影响已编译的图，仅影响下次 build）。"""

    NODE_REGISTRY.pop(name, None)


def registered_nodes() -> dict[str, Callable[[AgentState], Any]]:
    """返回当前注册表的快照，供 build_graph() 使用。"""

    return dict(NODE_REGISTRY)


def clear_registry() -> None:
    """清空注册表（测试用）。"""

    NODE_REGISTRY.clear()
