"""MCP 工单检索测试（FR-03 跨源 / 风险 R7 降级）。

覆盖：
    1. 工单检索纯函数：现象匹配、同型号优先、不相关返回空
    2. format_ticket 产出可进上下文的文本
    3. MCP server 可**独立调用**（stdio 拉起，经 MultiServerMCPClient）
    4. **工具报错 / server 起不来 -> 降级为空工具列表，主链路不受影响**

    .venv/bin/python -m pytest tests/test_mcp/test_ticket_server.py -q
    .venv/bin/python tests/test_mcp/test_ticket_server.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import mcp_client  # noqa: E402
from backend.mcp_servers.ticket_server import format_ticket, load_tickets, search_tickets  # noqa: E402
from backend.tools import call_tool, load_all_tools  # noqa: E402


def test_ticket_dataset_loads() -> None:
    tickets = load_tickets()
    assert tickets, "工单数据源为空：backend/config/tickets.yaml"
    assert all(item.get("ticket_no") for item in tickets)


def test_search_matches_symptom() -> None:
    rows = search_tickets("腔体真空度异常波动，本底压力上漂")
    assert rows, "应当命中真空度相关工单"
    assert rows[0]["ticket_no"] == "TK-2026-0431"
    assert rows[0]["score"] > 0


def test_search_prefers_same_device_model() -> None:
    rows = search_tickets("清洗后颗粒残留超标", device_model="Cleaner-C")
    assert rows
    assert rows[0]["device_model"] == "Cleaner-C"


def test_search_returns_empty_for_unrelated_symptom() -> None:
    """查不到必须返回空列表 —— 不能因为空就编造案例。"""

    assert search_tickets("今天午饭吃什么") == []


def test_format_ticket_contains_evidence_fields() -> None:
    row = search_tickets("腔体真空度异常波动")[0]
    text = format_ticket(row)
    for field in ("ticket_no", "root_cause", "action"):
        assert row[field] and str(row[field])[:8] in text
    assert "现象" in text and "经验" in text


# --------------------------------------------------------------------------- #
# MCP 集成（需要 fastmcp + langchain-mcp-adapters）
# --------------------------------------------------------------------------- #


def test_mcp_tools_load_and_callable() -> None:
    """MCP server 可独立调用：加载工具并真的跑一次工单检索。"""

    try:
        tools = asyncio.run(load_all_tools())
    except Exception as exc:  # pragma: no cover - 环境缺失时给出可读原因
        raise AssertionError(f"MCP 加载异常：{exc}") from exc

    if not tools:
        # 允许「未安装 MCP 依赖」的环境：此时必须已降级并给出原因，而不是静默为空
        status = mcp_client.status()
        assert status["degraded"] is True, status
        assert status["detail"], status
        return

    assert "ticket_search" in tools

    rows = asyncio.run(
        call_tool("ticket_search", symptom="腔体真空度异常波动", device_model="Etcher-A", limit=2)
    )
    assert isinstance(rows, list) and rows, f"MCP 返回未解包或为空：{rows!r}"
    assert rows[0].get("ticket_no"), rows[0]


def test_mcp_degrades_when_server_cannot_start(monkeypatch: Any = None) -> None:
    """故意让 server 起不来：必须降级为空工具并记录原因（R7）。"""

    saved = mcp_client.server_config
    mcp_client.server_config = lambda: {  # type: ignore[assignment]
        "ticket": {
            "command": sys.executable,
            "args": ["-c", "import sys; sys.exit(3)"],  # 立刻退出，模拟 server 启动失败
            "transport": "stdio",
            "env": {"PYTHONUNBUFFERED": "1"},
        }
    }
    try:
        tools = asyncio.run(mcp_client.load_tools())
    finally:
        mcp_client.server_config = saved  # type: ignore[assignment]

    assert tools == [], f"server 起不来时必须降级为空工具，实际：{tools!r}"
    status = mcp_client.status()
    assert status["degraded"] is True
    assert status["ok"] is False
    assert status["detail"], "降级时必须说明原因"


def test_mcp_disabled_by_config_is_graceful() -> None:
    """配置里关掉 MCP 时，同样走降级而不是报错。"""

    from backend.setting import get_settings

    settings = get_settings()
    saved = settings.mcp_enabled
    settings.mcp_enabled = False
    try:
        tools = asyncio.run(mcp_client.load_tools())
    finally:
        settings.mcp_enabled = saved

    assert tools == []
    assert "关闭" in mcp_client.status()["detail"]


def test_call_tool_rejects_unregistered_name() -> None:
    try:
        asyncio.run(call_tool("no_such_tool"))
    except KeyError as exc:
        assert "no_such_tool" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未注册工具必须抛 KeyError")


if __name__ == "__main__":  # pragma: no cover
    import traceback

    cases = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    failed = 0
    for name, case in cases:
        try:
            case()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    raise SystemExit(1 if failed else 0)
