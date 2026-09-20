"""三块业务与问答链路的**独立性**测试。

为什么要有这个文件：维护计划 / 备件商城与采购 / 考核认证这三块功能是**独立业务**，
既不该被塞进问答（SSE、会话、`qa_id`）里，也不该反过来依赖编排层（LangGraph 图、
chat_service）。这条边界靠文档约定不够 —— 写歪一个 import 就悄悄绑上了，
所以用测试把它钉住，任何一边越界都会在 CI/本地立刻失败。

检查四项：
    ① 问答链路（services/chat_service.py、agents/**）不 import 三块业务；
    ② 三块业务（services/plan|parts|training_service.py、routers/plans|parts|training.py）
       不 import 问答链路（chat_service / agents.graph / agents.nodes / SSE / 会话依赖）；
    ③ 三块业务的路由不依赖会话或问答记录（没有 session_id / qa_id 之类的入参）；
    ④ 三块业务的接口各自挂在自己的路径前缀下（/plans、/parts、/training），
       与问答的 /chat 完全分开。

运行：
    .venv/bin/python -m pytest tests/test_api/test_module_independence.py -q
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BACKEND = ROOT / "backend"

#: 三块业务的模块（相对 backend/ 的路径）
BIZ_MODULES = (
    "services/plan_service.py",
    "services/parts_service.py",
    "services/training_service.py",
    "routers/plans.py",
    "routers/parts.py",
    "routers/training.py",
)

#: 问答链路的模块（相对 backend/ 的路径）
QA_MODULES = (
    "services/chat_service.py",
    "agents/graph.py",
    "agents/agent_factory.py",
    "routers/chat.py",
)

#: 业务模块里**禁止出现**的 import 目标（问答编排链路）
QA_IMPORT_PATTERNS = (
    "chat_service",
    "agents.graph",
    "agents.agent_factory",
    "agents.nodes",
    "utils.sse",
    "routers.chat",
)

#: 业务模块里禁止出现的问答概念（会话 / SSE / 问答记录）
QA_CONCEPT_PATTERNS = (
    re.compile(r"\bsession_id\b"),
    re.compile(r"\bqa_id\b"),
    re.compile(r"\bStreamingResponse\b"),
    re.compile(r"\bthread_id\b"),
    re.compile(r"text/event-stream"),
)


def _imports(path: Path) -> list[str]:
    """取一个 .py 文件里所有 import 的目标（含 `from X import Y` 的 X）。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * (node.level or 0)) + (node.module or "")
            found.append(base)
            found.extend(f"{base}.{alias.name}" for alias in node.names)
    return found


# --------------------------------------------------------------------------- #
# ① 问答链路不依赖三块业务
# --------------------------------------------------------------------------- #


def test_qa_chain_does_not_import_business_modules() -> None:
    """问答链路里不允许出现 plan/parts/training 三块业务的 import。"""

    hits: list[str] = []
    for rel in QA_MODULES:
        path = BACKEND / rel
        if not path.exists():  # 文件被重命名时给出明确信息，而不是静默跳过
            raise AssertionError(f"问答链路模块不存在，测试需要更新：{rel}")
        for name in _imports(path):
            if any(token in name for token in ("plan_service", "parts_service", "training_service")):
                hits.append(f"{rel} -> {name}")
    assert not hits, "问答链路引用了业务模块（应立即解耦）：\n  " + "\n  ".join(hits)


# --------------------------------------------------------------------------- #
# ② 三块业务不依赖问答链路
# --------------------------------------------------------------------------- #


def test_business_modules_do_not_import_qa_chain() -> None:
    """三块业务里不允许 import 问答链路（含编排图与 SSE）。"""

    hits: list[str] = []
    for rel in BIZ_MODULES:
        path = BACKEND / rel
        assert path.exists(), f"业务模块不存在：{rel}"
        for name in _imports(path):
            if any(token in name for token in QA_IMPORT_PATTERNS):
                hits.append(f"{rel} -> {name}")
    assert not hits, "业务模块引用了问答链路（应立即解耦）：\n  " + "\n  ".join(hits)


def test_business_modules_have_no_qa_concepts() -> None:
    """三块业务里不允许出现会话 / SSE / 问答记录等问答侧概念。

    例外：`agents.state` 里的 `Evidence`/`Citation` 是**共享数据契约**
    （题干依据、计划依据都用它），它不属于问答编排链路，允许引用。
    """

    hits: list[str] = []
    for rel in BIZ_MODULES:
        source = (BACKEND / rel).read_text(encoding="utf-8")
        # 去掉注释与字符串里的说明性提及：只检查真实代码行
        code_lines = [
            line
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for pattern in QA_CONCEPT_PATTERNS:
            for line in code_lines:
                if pattern.search(line):
                    hits.append(f"{rel}:{pattern.pattern} :: {line.strip()[:90]}")
    assert not hits, "业务模块里出现了问答侧概念：\n  " + "\n  ".join(hits)


def test_shared_contract_layer_is_allowed() -> None:
    """确认共享契约层（state / schemas / rag / tools）确实是这三块业务的合法依赖。

    这条是**正向**断言：独立性不是「什么都不能 import」，而是依赖方向正确 ——
    routers -> services -> rag/tools -> config，共享契约可以复用。
    """

    for rel in BIZ_MODULES:
        imports = " ".join(_imports(BACKEND / rel))
        assert "routers" not in imports.replace("routers.", "") or "backend/routers" in rel, (
            f"{rel} 反向引用了 routers（依赖方向错误）"
        )
    # services 层允许引用 rag / tools / schemas / db / setting
    plan = (BACKEND / "services/plan_service.py").read_text(encoding="utf-8")
    assert "from ..rag.retriever import" in plan
    assert "from ..tools.kb_tools import" in plan


# --------------------------------------------------------------------------- #
# ③④ 路由独立：自己的前缀、不依赖会话
# --------------------------------------------------------------------------- #


def test_business_routers_declare_only_their_own_paths() -> None:
    """三块业务的路由各自只在自己的前缀下，且入参里没有会话/问答记录。"""

    from backend.routers import parts, plans, training

    groups = {
        "plans": (plans.router, "/plans"),
        "parts": (parts.router, "/parts"),
        "training": (training.router, "/training"),
    }
    for name, (router, prefix) in groups.items():
        paths = [route.path for route in router.routes]
        assert paths, f"{name} 路由为空"
        for path in paths:
            assert path.startswith(prefix), f"{name} 出现了不属于自己前缀的路由：{path}"
            assert not path.startswith("/chat"), f"{name} 混入了问答路由：{path}"

        # 每个端点的入参里不允许出现会话 / 问答记录（依赖注入的 service 除外）
        for route in router.routes:
            params = set(getattr(route, "param_convertors", {}) or {})
            assert not ({"session_id", "qa_id"} & params), f"{name} 的 {route.path} 依赖了会话/问答记录"


def test_business_endpoints_are_separate_from_qa_endpoints() -> None:
    """在真实 app 的 OpenAPI 里，三块业务的路径与问答路径完全不相交。"""

    from backend.main import app

    paths = set(app.openapi()["paths"].keys())
    biz = {p for p in paths if p.startswith(("/api/plans", "/api/parts", "/api/training"))}
    qa = {p for p in paths if p.startswith(("/api/chat", "/api/feedback", "/api/upload"))}
    assert biz and qa
    assert not (biz & qa), f"业务与问答路径相交：{biz & qa}"
    # 业务接口不需要 SSE：OpenAPI 里它们的响应都必须是 application/json
    spec = app.openapi()
    for path in sorted(biz):
        for method, operation in spec["paths"][path].items():
            content = operation.get("responses", {}).get("200", {}).get("content", {})
            assert "application/json" in content, f"{path} [{method}] 不是 JSON 响应：{list(content)}"


def test_business_routers_do_not_require_sse_or_sessions() -> None:
    """业务路由文件里不出现 SSE / 会话相关符号（与②互为印证，盯住路由层）。"""

    for rel in ("routers/plans.py", "routers/parts.py", "routers/training.py"):
        source = (BACKEND / rel).read_text(encoding="utf-8")
        for banned in ("EventSourceResponse", "StreamingResponse", "session_id", "qa_id"):
            assert banned not in source, f"{rel} 出现了问答侧符号：{banned}"
