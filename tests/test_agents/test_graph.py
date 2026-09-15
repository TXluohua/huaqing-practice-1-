"""LangGraph 链路骨架测试（开发文档 6.2 的 tests/test_agents/test_graph.py）。

覆盖范围（骨架阶段）：
    1. 零节点图可以编译
    2. 零节点时状态原样透传，且初始状态是 fail-closed
    3. 传入节点时按 NODE_SEQUENCE 线性串联（节点接入后的接线能力）
    4. 检查点按 thread_id 隔离会话
    5. session_id <-> thread_id 映射
    6. 异步调用与流式链路可用

这些用例在节点接入之后依然成立，不需要改写。

运行方式（当前环境未安装 pytest，两种都支持）：
    pytest tests/test_agents/test_graph.py
    python3 tests/test_agents/test_graph.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from backend.agents.graph import build_graph, compile_graph, node_names  # noqa: E402
from backend.agents.state import make_initial_state  # noqa: E402
from backend.memory import SessionRegistry, thread_config  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 零节点图可以编译
# --------------------------------------------------------------------------- #
def test_graph_compiles_without_nodes() -> None:
    # 零节点路径用显式空映射验证，与全局注册表解耦
    # （RAG 节点接入后注册表不再为空，见 test_rag_nodes_are_registered）
    compiled = compile_graph(nodes={})
    assert compiled is not None
    assert node_names(compiled) == [], "显式空注册表时不应有任何业务节点"


def test_rag_nodes_are_registered() -> None:
    """RAG 侧节点已接入：默认注册表构建的图上应出现检索与生成链路的节点。"""

    compiled = compile_graph()
    names = node_names(compiled)
    for expected in ("rewrite", "retrieve", "rerank", "build_context", "generate", "verify"):
        assert expected in names, f"缺少节点 {expected}（实际：{names}）"
    # 顺序必须遵循 NODE_SEQUENCE，而不是注册顺序或字母序
    assert names.index("retrieve") < names.index("rerank") < names.index("build_context")
    assert names.index("generate") < names.index("verify")


# --------------------------------------------------------------------------- #
# 2. 状态透传 + fail-closed
# --------------------------------------------------------------------------- #
def test_zero_node_graph_passes_state_through() -> None:
    compiled = compile_graph(nodes={})  # 显式空映射，与全局注册表解耦
    state = make_initial_state("刻蚀机真空度异常怎么排查？", session_id="s1")

    # 初始状态必须 fail-closed：verify 判定之前不得视为可信
    assert state["status"] == "NOT_COVERED"
    assert state["confidence"] == 0.0
    assert state["confidence_label"] == "low"

    result = compiled.invoke(state, thread_config("t-passthrough"))
    assert result["question"] == "刻蚀机真空度异常怎么排查？"
    assert result["trace_id"] == state["trace_id"]
    assert result["status"] == "NOT_COVERED"
    assert result["answer"] == ""


# --------------------------------------------------------------------------- #
# 3. 传入节点时的线性接线能力
# --------------------------------------------------------------------------- #
def test_linear_wiring_when_nodes_are_supplied() -> None:
    executed: list[str] = []

    # 全链路异步（开发文档 6.4）：节点写成 async def，用 ainvoke 调用
    async def first(state):  # type: ignore[no-untyped-def]
        executed.append("rewrite")
        return {"queries": ["q1"]}

    async def second(state):  # type: ignore[no-untyped-def]
        executed.append("retrieve")
        return {"timings": {"retrieve": 1.0}}

    graph = build_graph({"retrieve": second, "rewrite": first})
    compiled = graph.compile()

    # 顺序由 NODE_SEQUENCE 决定，与注册顺序无关
    assert node_names(compiled) == ["rewrite", "retrieve"]

    async def scenario() -> dict:
        return await compiled.ainvoke(
            make_initial_state("q"), thread_config("t-linear")
        )

    result = asyncio.run(scenario())
    assert executed == ["rewrite", "retrieve"]
    assert result["queries"] == ["q1"]
    assert result["timings"] == {"retrieve": 1.0}


def test_blank_graph_node_names_are_empty() -> None:
    assert node_names(build_graph({}).compile()) == []


# --------------------------------------------------------------------------- #
# 4. 检查点按 thread_id 隔离
# --------------------------------------------------------------------------- #
def test_checkpointer_isolates_threads() -> None:
    # 本用例只验证检查点隔离，故用零节点图（nodes={}）：不依赖知识库索引
    compiled = compile_graph(checkpointer=InMemorySaver(), nodes={})

    async def scenario() -> None:
        await compiled.ainvoke(
            make_initial_state("问题A", session_id="a", thread_id="thread-a"),
            thread_config("thread-a"),
        )
        await compiled.ainvoke(
            make_initial_state("问题B", session_id="b", thread_id="thread-b"),
            thread_config("thread-b"),
        )

        state_a = await compiled.aget_state(thread_config("thread-a"))
        state_b = await compiled.aget_state(thread_config("thread-b"))
        # 两个 thread 各自独立留存，互不覆盖
        assert state_a.values["question"] == "问题A"
        assert state_b.values["question"] == "问题B"
        assert state_a.values["thread_id"] == "thread-a"
        assert state_b.values["thread_id"] == "thread-b"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 5. session_id <-> thread_id 映射
# --------------------------------------------------------------------------- #
def test_session_registry_binding() -> None:
    registry = SessionRegistry()
    thread = registry.bind("sess-1", title="真空度排查")
    assert thread == "thread-sess-1"
    # 重复绑定不得更换 thread_id，否则会话记忆会丢
    assert registry.thread_id("sess-1") == thread
    assert registry.bind("sess-1", thread_id="ignored") == thread
    assert registry.session_id_for(thread) == "sess-1"
    assert registry.snapshot()["sess-1"]["title"] == "真空度排查"

    auto = registry.thread_id(registry.new_session_id())
    assert auto.startswith("thread-")

    registry.reset()
    assert registry.snapshot() == {}


# --------------------------------------------------------------------------- #
# 6. 工厂：异步调用与流式链路
# --------------------------------------------------------------------------- #
def test_factory_ainvoke_and_stream() -> None:
    from backend.agents.agent_factory import (
        ainvoke,
        astream,
        close_agent,
        graph_summary,
    )

    async def scenario() -> None:
        try:
            result = await ainvoke("O-ring 更换步骤", session_id="sess-factory")
            assert result["question"] == "O-ring 更换步骤"
            assert result["session_id"] == "sess-factory"
            assert result["thread_id"] == "thread-sess-factory"

            summary = graph_summary()
            assert summary["ready"] is True
            assert summary["name"] == "smka_qa_graph"
            assert isinstance(summary["nodes"], list)

            chunks = [c async for c in astream("参数下限是多少", session_id="sess-stream")]
            assert chunks, "values 模式下至少应产出一份状态"
            assert chunks[-1]["question"] == "参数下限是多少"
        finally:
            await close_agent()
        assert graph_summary()["ready"] is False

    asyncio.run(scenario())


if __name__ == "__main__":  # pragma: no cover - 无 pytest 时的兜底运行入口
    import traceback

    cases = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
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
