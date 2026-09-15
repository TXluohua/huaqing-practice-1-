# 半导体设备维护知识库智能问答系统

面向半导体设备维护的知识库问答系统。核心命题：**查得准、引得实、不敢答的敢说不知道**。

完整设计见 `半导体设备维护知识库智能问答系统_开发文档（初稿）(2).md`。

---

## 当前进度

**LangGraph 链路骨架已打通，节点与边待接入。**

| 已完成 | 说明 |
| --- | --- |
| 状态契约 | `backend/agents/state.py` —— AgentState / Evidence / Citation / ImageExtraction |
| 图构建与编译 | `backend/agents/graph.py` —— 零节点时 START 直连 END |
| 异步单例 | `backend/agents/agent_factory.py` —— get_agent / ainvoke / astream |
| 会话记忆 | `backend/memory.py` —— sqlite 检查点 + session_id ↔ thread_id 映射 |
| 自检入口 | `scripts/ask.py`、`tests/test_agents/test_graph.py` |

节点注册表（`backend/agents/nodes/__init__.py`）当前为空，因此图中**没有节点、没有业务边**，
但 compile / invoke / ainvoke / stream / 检查点 / 会话隔离这整条链路已经可用。

---

## 快速开始

### 1. 准备虚拟环境

```bash
make install          # 等价于 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

或手动执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
source .venv/bin/activate      # 可选，激活后可直接用 python / pytest
```

> 项目所有命令都走 `.venv/bin/python`，不依赖系统 Python 环境。

### 2. 验证链路

```bash
make test                                  # 7 个链路测试
make ask Q="刻蚀机真空度异常怎么排查？"      # 命令行单问
make ask-stream Q="参数下限是多少"           # 流式链路
```

预期输出（骨架阶段暂无节点，答案为空、状态为 NOT_COVERED）：

```
检查点后端  : sqlite-async（落盘=True）
已接入节点  : 无（骨架阶段，START -> END 直连）
thread_id   : thread-venv-demo
状态        : NOT_COVERED  置信度=0.0
答案        : （空 —— 尚未接入 generate 节点）
```

### 3. 会话持久化

检查点落在 `backend/data/sqlite/checkpoints.sqlite`（该目录已在 .gitignore 中）。
同一 `session_id` 复用同一 `thread_id`，**进程重启后仍可读回历史状态**。
若卸载 `langgraph-checkpoint-sqlite`，`memory.py` 会自动降级为内存后端，主链路不中断。

---

## 接入节点

节点写在 `backend/agents/nodes/` 下并注册即可自动进链路：

```python
# backend/agents/nodes/retrieval.py
from ..state import AgentState

async def rewrite(state: AgentState) -> dict:
    """问题 -> 2~3 路检索式"""
    return {"queries": [state["question"]]}

# 注册
from backend.agents.nodes import NodeName, register_node
register_node(NodeName.REWRITE, rewrite)
```

顺序由 `NODE_SEQUENCE`（开发文档 4.3）决定。
真实拓扑含条件分支（证据是否足够 → 是否走 augment），
需在 `graph.build_graph()` 中改用 `add_conditional_edges`，该处已标 TODO。

---

## 目录结构

```
backend/
├── agents/          # 编排：state / graph / agent_factory / nodes / prompts
├── memory.py        # 检查点与会话映射
├── data/            # 运行时数据（不入 git）
scripts/ask.py       # 命令行自检
tests/test_agents/   # 链路测试
```

依赖方向（开发文档 6.1）：`routers → services → agents/tools/rag → config`，禁止反向 import。

---

## 尚未实现

- HTTP / SSE 接入层（`main.py`、`routers/`、`services/`）
- 9 个业务节点（rewrite / retrieve / rerank / build_context / augment / generate / verify / respond / ingest_image）
- RAG 链路（解析、切片、向量库、混合检索、精排）
- MCP 工单检索、前端 Vue3 页面
