"""LangGraph 主图（契约级文件，影响全局，需评审 —— 开发文档 6.3）。

当前阶段：骨架 —— 链路已打通，节点与边留空
==========================================

已经具备的能力（不依赖任何节点）：
    build_graph()   -> StateGraph(AgentState)
    compile_graph() -> 挂检查点的可执行图
    invoke / ainvoke / stream / astream / get_state / thread_id 会话记忆

原理：LangGraph 允许「零节点 + START -> END」的图编译并执行，状态原样透传。
所以整条链路（编译、调用、检查点、会话隔离）现在就能跑通、能被测试覆盖，
不必等节点写完。

尚未接入（留给你实现）：
    节点：开发文档 4.3 的 9 个节点，见 nodes/__init__.py 的 NodeName / NODE_SEQUENCE
    边：真实拓扑含一处条件分支（开发文档 4.3 图结构）

        ingest_image(可选) -> rewrite -> retrieve -> rerank -> build_context
                                                          |
                                            [证据是否足够?] --是--> generate -> verify -> respond
                                                          |否
                                                          v
                                                       augment ----^

    「证据是否足够?」由 build_graph() 中的 add_conditional_edges 实现，判定函数是
    nodes/retrieval.should_augment()（2026-09 已接入，`augment` 未注册时自动退回线性拓扑）。

接入节点后的唯一改动点：build_graph()。
节点实现写在 nodes/ 下，用 register_node() 注册即可被自动串进链路。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable

from langgraph.graph import END, START, StateGraph

from .nodes import NODE_SEQUENCE, NodeName, registered_nodes
from .state import AgentState

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

#: 图的名称，出现在 stream / 追踪信息中
GRAPH_NAME = "smka_qa_graph"

#: RAG 侧节点模块（缺任一即视为实现缺陷，直接报错而不是静默少节点）
_REQUIRED_NODE_MODULES: tuple[str, ...] = ("retrieval", "generation")

#: 平台侧节点模块（可能尚未实现；导入失败只警告，不影响检索/生成链路）
#: multimodal -> ingest_image / augment；response -> respond（甲侧，与 multimodal 分开
#: 是因为 respond 与多模态无关，不应塞进同一个模块）。
_OPTIONAL_NODE_MODULES: tuple[str, ...] = ("multimodal", "response")


def _load_node_modules() -> None:
    """导入节点实现模块，触发各模块底部的 register_node()。

    放在函数内而不是模块顶层，避免 nodes <-> graph 的循环 import。
    各节点模块自己注册，**不改 nodes/__init__.py 的注册表文件**（协作规范 §3.4）。
    """

    package = __package__ or "backend.agents"
    for module in _REQUIRED_NODE_MODULES:
        importlib.import_module(f"{package}.nodes.{module}")
    for module in _OPTIONAL_NODE_MODULES:
        name = f"{package}.nodes.{module}"
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("backend.agents.nodes"):
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - 平台侧节点未就绪时降级（R7 同一原则）
            logger.warning("节点模块 %s 导入失败（%s），该分支暂不参与拓扑", module, exc)



def _order(names: list[str]) -> list[str]:
    """按开发文档 4.3 的声明顺序排序；未在 NODE_SEQUENCE 中声明的排在最后。"""

    declared = [n for n in NODE_SEQUENCE if n in names]
    extra = [n for n in names if n not in NODE_SEQUENCE]
    return declared + extra


def build_graph(
    nodes: Mapping[str, Callable[[AgentState], Any]] | None = None,
) -> StateGraph:
    """构建主图（未编译）。

    参数
    ----
    nodes:
        节点名 -> 节点实现。默认使用全局注册表 NODE_REGISTRY 的快照。

        * 传入空映射 / 注册表为空（骨架阶段的默认情况）：
          图内没有任何节点，直接连 START -> END，状态原样透传。
        * 传入非空映射：
          按 NODE_SEQUENCE 的顺序把节点线性串联，
          即 START -> n1 -> n2 -> ... -> END。

    返回
    ----
    未编译的 StateGraph，交给 compile_graph() 挂检查点后使用。
    """

    _load_node_modules()
    registry = dict(registered_nodes() if nodes is None else nodes)
    graph: StateGraph = StateGraph(AgentState)

    if not registry:
        # ---- 骨架阶段：零节点。仅声明入口与出口，状态原样透传。 ----
        graph.add_edge(START, END)
        logger.debug("build_graph: 未注册任何节点，图以 START -> END 直连编译")
        return graph

    # ---- 真实拓扑（开发文档 4.3）----
    #   ... -> build_context -> [证据是否足够?] --是--> generate -> verify -> END
    #                                     |否
    #                                     v
    #                                  augment -------------------^
    #
    # 条件边只在 augment（平台侧 ingest_image/augment 节点）已注册时启用；
    # 未注册时退回线性拓扑，保证 RAG 侧的 retrieve/rerank/verify 现在就能跑。
    ordered = _order(list(registry))
    for name in ordered:
        graph.add_node(name, registry[name])
    graph.add_edge(START, ordered[0])

    branch_from = NodeName.BUILD_CONTEXT
    augment_name = NodeName.AUGMENT
    generate_name = NodeName.GENERATE
    has_branch = (
        augment_name in registry and branch_from in registry and generate_name in registry
    )

    #: 被条件边取代、不再需要画的线性边
    replaced: set[tuple[str, str]] = set()

    def _next_of(name: str) -> str | None:
        index = ordered.index(name) if name in ordered else -1
        return ordered[index + 1] if 0 <= index < len(ordered) - 1 else None

    if has_branch:
        from .nodes.retrieval import should_augment  # 判定属检索侧（证据是否充分）

        graph.add_conditional_edges(
            branch_from,
            should_augment,
            {augment_name: augment_name, generate_name: generate_name},
        )
        graph.add_edge(augment_name, generate_name)
        after_build = _next_of(branch_from)
        if after_build:
            replaced.add((branch_from, after_build))
        after_augment = _next_of(augment_name)
        if after_augment:
            replaced.add((augment_name, after_augment))
        logger.debug("build_graph: 已启用条件边 %s -> (%s | %s)", branch_from, augment_name, generate_name)

    for current_node, next_node in zip(ordered, ordered[1:]):
        if (current_node, next_node) in replaced:
            continue
        graph.add_edge(current_node, next_node)
    graph.add_edge(ordered[-1], END)
    logger.debug(
        "build_graph: 已接入 %d 个节点，拓扑 %s", len(ordered), " -> ".join(ordered)
    )
    return graph


def compile_graph(
    checkpointer: Any | None = None,
    *,
    nodes: Mapping[str, Callable[[AgentState], Any]] | None = None,
    name: str = GRAPH_NAME,
) -> "CompiledStateGraph":
    """构建并编译主图，返回可执行图。

    参数
    ----
    checkpointer:
        会话检查点。由 backend.memory.create_checkpointer() 提供；
        传 None 时不持久化会话（单次问答仍可正常工作）。
    nodes:
        同 build_graph()。
    name:
        图名称。
    """

    return build_graph(nodes).compile(checkpointer=checkpointer, name=name)


def node_names(compiled: "CompiledStateGraph") -> list[str]:
    """已编译图中的业务节点名（排除内建的 __start__ / __end__）。

    按 NODE_SEQUENCE 声明的顺序返回（未声明的排在最后），
    以便直接反映链路走向，而不是字母序。

    骨架阶段返回空列表 —— 可用作「当前是否已接入节点」的自检。
    """

    names = [n for n in compiled.get_graph().nodes if not n.startswith("__")]
    return _order(names)


def has_nodes(compiled: "CompiledStateGraph") -> bool:
    """图中是否已接入业务节点。"""

    return bool(node_names(compiled))
